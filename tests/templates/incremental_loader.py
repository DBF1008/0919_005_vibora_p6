"""
Unit tests for the incremental (per-change) template recompilation done by
TemplateLoader in debug mode.

Run manually with: python3 tests/templates/incremental_loader.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, REPO_ROOT)
if 'vibora' not in sys.modules:
    package = types.ModuleType('vibora')
    package.__path__ = [os.path.join(REPO_ROOT, 'vibora')]
    sys.modules['vibora'] = package

from vibora.templates import TemplateEngine
from vibora.templates.loader import TemplateLoader
from vibora.templates.exceptions import InvalidTag


def render(engine, name, **context):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(engine.render(name, **context))
    finally:
        loop.close()


class IncrementalLoaderSuite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = TemplateEngine()
        self.loader = TemplateLoader([self.tmp.name], self.engine, interval=999)
        self.compile_calls = []
        original_compile = self.engine.compiler.compile

        def counting_compile(template, verbose=False):
            self.compile_calls.append(template.hash)
            return original_compile(template, verbose=verbose)

        self.engine.compiler.compile = counting_compile

    def tearDown(self):
        self.loader.has_to_run = False
        self.tmp.cleanup()

    def write_template(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w') as f:
            f.write(content)
        # Bumping the mtime so the change is always detected regardless of
        # the filesystem timestamp resolution.
        future = time.time() + 10
        os.utime(path, (future, future))
        return path

    def load_and_compile(self):
        self.loader.load()
        self.engine.compile_templates()
        self.compile_calls.clear()

    def test_initial_load_records_mtimes_and_first_tick_is_quiet(self):
        self.write_template('a.html', 'A')
        self.write_template('b.html', 'B')
        self.load_and_compile()
        self.assertEqual(len(self.loader.cache), 2)
        self.loader.check_for_modified_templates()
        self.assertEqual(self.compile_calls, [])

    def test_only_the_changed_template_is_recompiled(self):
        self.write_template('a.html', 'A')
        self.write_template('b.html', 'B')
        self.write_template('c.html', 'C')
        self.load_and_compile()
        self.write_template('b.html', 'B2')
        self.loader.check_for_modified_templates()
        self.assertEqual(len(self.compile_calls), 1)
        self.assertEqual(render(self.engine, 'b.html'), 'B2')
        self.assertEqual(render(self.engine, 'a.html'), 'A')
        self.assertEqual(render(self.engine, 'c.html'), 'C')

    def test_dependents_are_recompiled_when_parent_changes(self):
        self.write_template('parent.html', 'P1 {{ who }}')
        self.write_template('child.html', "{% include 'parent.html' %}!")
        self.load_and_compile()
        self.assertEqual(render(self.engine, 'child.html', who='x'), 'P1 x!')
        self.write_template('parent.html', 'P2 {{ who }}')
        self.loader.check_for_modified_templates()
        # The parent and its dependent child must be recompiled, nothing else.
        self.assertEqual(len(self.compile_calls), 2)
        self.assertEqual(render(self.engine, 'child.html', who='x'), 'P2 x!')

    def test_failed_reload_rolls_back_and_is_retried(self):
        path = self.write_template('a.html', 'A1')
        self.load_and_compile()
        self.assertEqual(render(self.engine, 'a.html'), 'A1')
        self.write_template('a.html', '{% for %}')
        with self.assertRaises(InvalidTag):
            self.loader.check_for_modified_templates()
        # The engine kept the previous, working version of the template.
        self.assertEqual(render(self.engine, 'a.html'), 'A1')
        # The failed change was not marked as processed: fixing the file and
        # waiting for the next tick picks it up.
        self.assertNotEqual(self.loader.cache.get(path), os.path.getmtime(path))
        self.write_template('a.html', 'A2')
        self.loader.check_for_modified_templates()
        self.assertEqual(render(self.engine, 'a.html'), 'A2')

    def test_new_file_is_picked_up_incrementally(self):
        self.write_template('a.html', 'A')
        self.load_and_compile()
        self.write_template('new.html', 'N')
        self.loader.check_for_modified_templates()
        self.assertEqual(len(self.compile_calls), 1)
        self.assertEqual(render(self.engine, 'new.html'), 'N')


if __name__ == '__main__':
    unittest.main(verbosity=2)
