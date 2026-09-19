"""
Unit tests for the transactional (all-or-nothing) template loading.

Run manually with: python3 tests/templates/transactional_loading.py
"""
import os
import sys
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, REPO_ROOT)
if 'vibora' not in sys.modules:
    # Loading the templates subpackage without triggering vibora/__init__.py
    # (which requires compiled C extensions not needed for these tests).
    package = types.ModuleType('vibora')
    package.__path__ = [os.path.join(REPO_ROOT, 'vibora')]
    sys.modules['vibora'] = package

from vibora.templates import TemplateEngine, Template
from vibora.templates.exceptions import ConflictingNames, InvalidTag


class TransactionalLoadingSuite(unittest.TestCase):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_add_template_with_all_names_conflicting_registers_nothing(self):
        self.engine.add_template(Template('old'), ['taken'])
        before = dict(self.engine.templates)
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('new'), ['taken'])
        self.assertEqual(self.engine.templates, before)
        self.assertEqual(self.engine.templates['taken'].content, 'old')

    def test_add_template_registers_only_available_names(self):
        self.engine.add_template(Template('old'), ['taken'])
        parsed = self.engine.add_template(Template('new'), ['taken', 'free'])
        self.assertIs(self.engine.templates['free'], parsed)
        self.assertEqual(self.engine.templates['taken'].content, 'old')

    def test_batch_rollback_on_conflict(self):
        # Pre-registering the name that the 49th template will try to use.
        self.engine.add_template(Template('pre-existing'), ['t49'])
        batch = [(Template('content %d' % index), ['t%d' % index]) for index in range(1, 51)]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        # The 48 successfully parsed templates must have been rolled back.
        self.assertEqual(list(self.engine.templates.keys()), ['t49'])
        self.assertEqual(self.engine.templates['t49'].content, 'pre-existing')

    def test_batch_rollback_on_parse_error(self):
        batch = [(Template('valid'), ['a']), (Template('{% for %}'), ['b'])]
        with self.assertRaises(InvalidTag):
            self.engine.add_templates(batch)
        self.assertEqual(len(self.engine.templates), 0)

    def test_batch_success_registers_everything(self):
        batch = [(Template('content %d' % index), ['t%d' % index]) for index in range(1, 51)]
        parsed = self.engine.add_templates(batch)
        self.assertEqual(len(parsed), 50)
        self.assertEqual(len(self.engine.templates), 50)
        self.assertEqual(self.engine.templates['t50'].content, 'content 50')

    def test_transaction_restores_compiled_templates_and_cache(self):
        self.engine.add_template(Template('hello'), ['a'])
        self.engine.compile_templates()
        compiled_before = dict(self.engine.compiled_templates)
        cache_before = dict(self.engine.cache.loaded_templates)
        metas_before = dict(self.engine.cache.loaded_metas)
        with self.assertRaises(RuntimeError):
            with self.engine.transaction():
                self.engine.add_template(Template('bye'), ['b'])
                self.engine.compile_templates()
                self.engine.remove_template(self.engine.templates['a'])
                raise RuntimeError('boom')
        self.assertEqual(self.engine.compiled_templates, compiled_before)
        self.assertEqual(self.engine.cache.loaded_templates, cache_before)
        self.assertEqual(self.engine.cache.loaded_metas, metas_before)
        self.assertIn('a', self.engine.templates)
        self.assertNotIn('b', self.engine.templates)

    def test_transaction_success_keeps_changes(self):
        with self.engine.transaction():
            self.engine.add_template(Template('hello'), ['a'])
        self.assertIn('a', self.engine.templates)


if __name__ == '__main__':
    unittest.main(verbosity=2)
