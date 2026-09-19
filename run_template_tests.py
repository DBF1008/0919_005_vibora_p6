"""Standalone runner for the template engine unit tests.

The full `vibora` package requires compiled C extensions (see build.py and
a working Cython toolchain). This runner stubs the top-level package so the
pure-Python template subsystem can be tested without them.

Note: tests/templates/extensions.py is intentionally skipped because it
needs the full Vibora application (compiled extensions).
"""
import os
import sys
import types
from unittest import TestLoader, TextTestRunner

ROOT = os.path.dirname(os.path.abspath(__file__))

# Stub the top-level package to avoid importing compiled extensions.
stub = types.ModuleType('vibora')
stub.__path__ = [os.path.join(ROOT, 'vibora')]
sys.modules.setdefault('vibora', stub)
sys.path.insert(0, ROOT)

TEST_MODULES = [
    'tests.templates.render',
    'tests.templates.exceptions',
    'tests.templates.nodes',
    'tests.templates.transactions',
    'tests.templates.cython_source_map',
    'tests.templates.loader',
]

if __name__ == '__main__':
    loader = TestLoader()
    suite = loader.loadTestsFromNames(TEST_MODULES)
    result = TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(f'{len(result.failures) + len(result.errors)} tests failed.')
