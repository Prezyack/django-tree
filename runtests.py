#!/usr/bin/env python
# coding: utf-8

from __future__ import unicode_literals
import os
import sys
import django


if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
    django.setup()
    from django.test.runner import DiscoverRunner
    test_runner = DiscoverRunner(verbosity=2, keepdb=True)
    failures = test_runner.run_tests(['tree.tests'])
    if failures:
        sys.exit(failures)
