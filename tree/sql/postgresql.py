from collections import OrderedDict
from string import Formatter

from django.db import DEFAULT_DB_ALIAS, connections

from .base import ALPHANUM, ALPHANUM_LEN


class Arg:
    pattern = '%{position}${format_type}'
    format_types = ('I', 'L', 's')

    def __init__(self, position):
        self.position = position

    def __format__(self, format_spec):
        if format_spec and format_spec not in self.format_types:
            raise ValueError("Unknown format_spec '%s'" % format_spec)
        if not format_spec and self.format_types:
            format_spec = self.format_types[0]
        return self.pattern.format(position=self.position,
                                   format_type=format_spec)


class UsingArg(Arg):
    pattern = '${position}'
    format_types = ()


class AnyArg(OrderedDict):
    arg_class = Arg

    def __init__(self, *args, **kwargs):
        super(AnyArg, self).__init__(*args, **kwargs)
        self.position = 1

    def __getitem__(self, item):
        if item not in self:
            self[item] = self.arg_class(self.position)
            self.position += 1
        return super(AnyArg, self).__getitem__(item)


class AnyUsingArg(AnyArg):
    arg_class = UsingArg


def format_sql_in_function(sql, into=None):
    kwargs = AnyArg({'USING': AnyUsingArg()})
    # TODO: Replace Formatter with sql.format(**kwargs) when dropping Python 2.
    sql = Formatter().vformat(sql, (), kwargs).replace("'", "''")
    using = kwargs.pop('USING')
    args = ', '.join([k for k in kwargs])

    extra = ''
    if into is not None:
        extra += ' INTO ' + ', '.join(into)
    if using:
        extra += ' USING ' + ', '.join([a for a in using])

    return "EXECUTE format('%s', %s)%s;" % (sql, args, extra)


UPDATE_PATHS_FUNCTION = """
    CREATE OR REPLACE FUNCTION update_paths() RETURNS trigger AS $$
    DECLARE
        table_name text := TG_TABLE_NAME;
        pk text := TG_ARGV[0];
        parent text := TG_ARGV[1];
        path text := TG_ARGV[2];
        order_by text[] := TG_ARGV[3];
        max_siblings int := TG_ARGV[4];
        label_size int := TG_ARGV[5];
        order_by_cols text := array_to_string(order_by, ',');
        order_by_cols2 text := 't2.' || array_to_string(order_by, ',t2.');
        old_path ltree := NULL;
        new_path ltree;
        old_parent_path ltree := NULL;
        new_parent_path ltree;
        parent_changed boolean;
        n_siblings integer;
    BEGIN
        IF TG_OP != 'INSERT' THEN
            {get_old_paths}
        END IF;
        IF old_parent_path IS NULL THEN
            old_parent_path := ''::ltree;
        END IF;

        IF TG_OP = 'DELETE' THEN
            {update_old_siblings}
            RETURN OLD;
        END IF;

        {get_new_path}
        IF TG_OP = 'UPDATE' THEN
            IF new_path = '__rebuild__' THEN
                {rebuild}
                {set_new_path}
                RETURN NEW;
            END IF;
        END IF;


        {get_new_parent_path}
        IF new_parent_path IS NULL THEN
            new_parent_path := ''::ltree;
        END IF;
        IF TG_OP = 'UPDATE' THEN
            -- TODO: Add this behaviour to the model validation.
            IF new_parent_path <@ old_path THEN
                RAISE 'Cannot set itself or a descendant as parent.';
            END IF;
        END IF;

        IF TG_OP = 'INSERT' THEN
            parent_changed := TRUE;
        ELSE
            {get_parent_changed}
        END IF;

        IF parent_changed THEN
            {get_n_siblings}
            IF n_siblings = max_siblings THEN
                RAISE '`max_siblings` (%) has been reached.\n'
                    'You should increase it then rebuild.', max_siblings;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                RAISE LOG 'UPDATE %', OLD;
                {update_old_siblings}
            END IF;
        END IF;

        {get_new_parent_path}
        IF new_parent_path IS NULL THEN
            new_parent_path := ''::ltree;
        END IF;

        {update_new_siblings}
        {set_new_path}
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """.format(
    get_old_paths=format_sql_in_function("""
        SELECT {USING[OLD]}.{path}, subpath({USING[OLD]}.{path}, 0, -1)
    """, into=['old_path', 'old_parent_path']),
    get_new_path=format_sql_in_function(
        'SELECT {USING[NEW]}.{path}', into=['new_path']),
    rebuild=format_sql_in_function("""
        -- TODO: Handle concurrent writes during this query (using FOR UPDATE).
        WITH RECURSIVE generate_paths(pk, path) AS ((
                -- FIXME: This doesn't handle non-integer primary keys.
                SELECT NULL::int, ''::ltree
            ) UNION ALL (
                SELECT
                    t2.{pk},
                    t1.path || to_alphanum(
                        row_number() OVER (PARTITION BY t1.pk
                                           ORDER BY {order_by_cols2:s}) - 1,
                        {label_size:L})
                FROM generate_paths AS t1
                INNER JOIN {table_name} AS t2 ON (CASE
                    WHEN t1.pk IS NULL
                        THEN t2.{parent} IS NULL
                    ELSE t2.{parent} = t1.pk END)
            )
        ), updated AS (
            UPDATE {table_name} AS t2 SET {path} = t1.path
            FROM generate_paths AS t1
            WHERE t2.{pk} = t1.pk AND t2.{pk} != {USING[OLD]}.{pk}
                AND (t2.{path} IS NULL OR t2.{path} != t1.path)
        )
        SELECT path FROM generate_paths
        WHERE pk = {USING[OLD]}.{pk}
    """, into=['new_path']),
    update_old_siblings=format_sql_in_function("""
        -- TODO: Handle concurrent writes during this query (using FOR UPDATE).
        WITH RECURSIVE generate_paths(pk, old_path, new_path) AS ((
                SELECT
                    {pk},
                    {path},
                    {USING[old_parent_path]} || to_alphanum(
                        row_number() OVER (ORDER BY {order_by_cols:s}) - 1,
                        {label_size:L})
                FROM {table_name}
                WHERE (CASE
                        WHEN {USING[OLD]}.{parent} IS NULL
                            THEN {parent} IS NULL
                        ELSE {parent} = {USING[OLD]}.{parent} END)
                    AND {pk} != {USING[OLD]}.{pk}
            ) UNION ALL (
                SELECT
                    t2.{pk},
                    t2.{path},
                    t1.new_path || subpath(t2.{path}, nlevel(t1.new_path))
                FROM generate_paths AS t1
                INNER JOIN {table_name} AS t2
                ON t2.{path} ~ (ltree2text(t1.old_path) || '.*{{1,}}')::lquery
                WHERE t1.old_path != t1.new_path
                    AND t1.old_path > {USING[old_path]}
            ))
        UPDATE {table_name} AS t2 SET {path} = t1.new_path
        FROM generate_paths AS t1
        WHERE t2.{pk} = t1.pk
            AND (t2.{path} IS NULL OR t2.{path} != t1.new_path)
    """),
    get_parent_changed=format_sql_in_function("""
        SELECT
            ({USING[OLD]}.{parent} IS NULL AND {USING[NEW]}.{parent} IS NOT NULL)
            OR ({USING[OLD]}.{parent} IS NOT NULL AND {USING[NEW]}.{parent} IS NULL)
            OR ({USING[OLD]}.{parent} != {USING[NEW]}.{parent})
    """, into=['parent_changed']),
    get_n_siblings=format_sql_in_function("""
        SELECT COUNT(*) FROM {table_name}
        WHERE (CASE
            WHEN {USING[NEW]}.{parent} IS NULL
                THEN {parent} IS NULL
            ELSE {parent} = {USING[NEW]}.{parent} END)
    """, into=['n_siblings']),
    get_new_parent_path=format_sql_in_function("""
        SELECT {path} FROM {table_name} WHERE {pk} = {USING[NEW]}.{parent}
    """, into=['new_parent_path']),
    update_new_siblings=format_sql_in_function("""
        -- TODO: Handle concurrent writes during this query (using FOR UPDATE).
        WITH RECURSIVE generate_paths(pk, path) AS ((
                SELECT
                    {pk},
                    {USING[new_parent_path]} || to_alphanum(
                        row_number() OVER (ORDER BY {order_by_cols:s}) - 1,
                        {label_size:L})
                FROM ((
                        SELECT *
                        FROM {table_name}
                        WHERE
                            (CASE
                                WHEN {USING[NEW]}.{parent} IS NULL
                                    THEN {parent} IS NULL
                                ELSE {parent} = {USING[NEW]}.{parent} END)
                            AND (CASE
                                WHEN {USING[NEW]}.{pk} IS NULL
                                    THEN TRUE
                                ELSE {pk} != {USING[NEW]}.{pk} END)
                    ) UNION ALL (
                        SELECT {USING[NEW]}.*
                    )
                ) AS t
            ) UNION ALL (
                SELECT
                    t2.{pk},
                    t1.path || to_alphanum(
                        row_number() OVER (PARTITION BY t1.pk
                                           ORDER BY {order_by_cols2:s}) - 1,
                        {label_size:L})
                FROM generate_paths AS t1
                INNER JOIN {table_name} AS t2 ON t2.{parent} = t1.pk
            )
        ), updated AS (
            UPDATE {table_name} AS t2 SET {path} = t1.path
            FROM generate_paths AS t1
            WHERE t2.{pk} = t1.pk AND t2.{pk} != {USING[NEW]}.{pk}
                AND (t2.{path} IS NULL OR t2.{path} != t1.path)
        )
        SELECT path FROM generate_paths
        WHERE (CASE
            WHEN {USING[NEW]}.{pk}
                IS NULL THEN pk IS NULL
            ELSE pk = {USING[NEW]}.{pk} END)
    """, into=['new_path']),
    set_new_path=format_sql_in_function("""
        -- FIXME: `json_populate_record` is not available in PostgreSQL < 9.3.
        SELECT *
        FROM json_populate_record({USING[NEW]},
                                  '{{"{path:s}": "{new_path:s}"}}'::json)
    """, into=['NEW']),
)


REBUILD_PATHS_FUNCTION = """
    CREATE OR REPLACE FUNCTION rebuild_paths(
        table_name text, pk text, parent text, path text) RETURNS void AS $$
    BEGIN
        {}
    END;
    $$ LANGUAGE plpgsql;
    """.format(
    format_sql_in_function("""
        UPDATE {table_name} SET {path} = '__rebuild__' FROM (
            SELECT * FROM {table_name}
            WHERE {parent} IS NULL
            LIMIT 1
            FOR UPDATE
        ) AS t
        WHERE {table_name}.{pk} = t.{pk}
    """),
)


CREATE_FUNCTIONS_QUERIES = (
    'CREATE EXTENSION IF NOT EXISTS ltree;',
    """
    CREATE OR REPLACE FUNCTION to_alphanum(i bigint,
                                           size smallint) RETURNS text AS $$
    DECLARE
        ALPHANUM text := '{}';
        ALPHANUM_LEN int := {};
        out text := '';
        remainder int := 0;
    BEGIN
        LOOP
            remainder := i % ALPHANUM_LEN;
            i := i / ALPHANUM_LEN;
            out := substring(ALPHANUM from remainder+1 for 1) || out;
            IF i = 0 THEN
                RETURN lpad(out, size, '0');
            END IF;
        END LOOP;
    END;
    $$ LANGUAGE plpgsql;
    """.format(ALPHANUM, ALPHANUM_LEN),
    UPDATE_PATHS_FUNCTION,
    REBUILD_PATHS_FUNCTION,
)
# We escape the modulo operator '%' otherwise Django considers it
# as a placeholder for a parameter.
CREATE_FUNCTIONS_QUERIES = [s.replace('%', '%%')
                            for s in CREATE_FUNCTIONS_QUERIES]


DROP_FUNCTIONS_QUERIES = (
    """
    DROP FUNCTION IF EXISTS rebuild_paths(table_name text, pk text,
                                          parent text, path text);
    """,
    'DROP FUNCTION IF EXISTS update_paths();',
    'DROP FUNCTION IF EXISTS to_alphanum(i bigint);',
    'DROP EXTENSION IF EXISTS ltree;',
)

CREATE_TRIGGER_QUERIES = (
    """
    CREATE TRIGGER "update_{path}_before"
    BEFORE INSERT OR UPDATE OF {update_columns}
    ON "{table}"
    FOR EACH ROW
    WHEN (pg_trigger_depth() = 0)
    EXECUTE PROCEDURE update_paths(
        '{pk}', '{parent}', '{path}', '{{{order_by}}}',
        {max_siblings}, {label_size});
    """,
    """
    CREATE TRIGGER "update_{path}_after"
    AFTER DELETE
    ON "{table}"
    FOR EACH ROW
    WHEN (pg_trigger_depth() = 0)
    EXECUTE PROCEDURE update_paths(
        '{pk}', '{parent}', '{path}', '{{{order_by}}}',
        {max_siblings}, {label_size});
    """,
    """
    CREATE OR REPLACE FUNCTION rebuild_{table}_{path}() RETURNS void AS $$
    BEGIN
        PERFORM rebuild_paths('{table}', '{pk}', '{parent}', '{path}');
    END;
    $$ LANGUAGE plpgsql;
    """,
    # TODO: Find a way to create this unique constraint
    #       somewhere else.
    """
    ALTER TABLE "{table}"
    ADD CONSTRAINT "{table}_{path}_unique" UNIQUE ("{path}")
    -- FIXME: Remove this `INITIALLY DEFERRED` whenever possible.
    INITIALLY DEFERRED;
    """,
)

DROP_TRIGGER_QUERIES = (
    # TODO: Find a way to delete this unique constraint
    #       somewhere else.
    'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{table}_{path}_unique";'
    'DROP TRIGGER IF EXISTS "update_{path}_after" ON "{table}";',
    'DROP TRIGGER IF EXISTS "update_{path}_before" ON "{table}";',
    'DROP FUNCTION IF EXISTS rebuild_{table}_{path}();',
)


CREATE_INDEX_QUERIES = (
    'CREATE INDEX "{table}_{path}" ON "{table}" USING gist("{path}");',
)

DROP_INDEX_QUERIES = (
    'DROP INDEX "{table}_{path}";',
)


def rebuild(table, path_field, db_alias=DEFAULT_DB_ALIAS):
    with connections[db_alias].cursor() as cursor:
        cursor.execute('SELECT rebuild_{}_{}();'.format(table, path_field))


def disable_trigger(table, path_field, db_alias=DEFAULT_DB_ALIAS):
    with connections[db_alias].cursor() as cursor:
        cursor.execute('ALTER TABLE "{}" DISABLE TRIGGER "update_{}_after";'
                       .format(table, path_field))
        cursor.execute('ALTER TABLE "{}" DISABLE TRIGGER "update_{}_before";'
                       .format(table, path_field))


def enable_trigger(table, path_field, db_alias=DEFAULT_DB_ALIAS):
    with connections[db_alias].cursor() as cursor:
        cursor.execute('ALTER TABLE "{}" ENABLE TRIGGER "update_{}_before";'
                       .format(table, path_field))
        cursor.execute('ALTER TABLE "{}" ENABLE TRIGGER "update_{}_after";'
                       .format(table, path_field))
