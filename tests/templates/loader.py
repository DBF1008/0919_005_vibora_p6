import os
import shutil
import tempfile
import time

from vibora.templates import TemplateEngine
from vibora.templates.exceptions import ConflictingNames
from vibora.templates.loader import TemplateLoader
from vibora.tests import TestSuite


class TemplateLoaderSuite(TestSuite):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.engine = TemplateEngine()
        self.loader = TemplateLoader([self.directory], self.engine)

    def tearDown(self):
        self.loader.has_to_run = False
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, name, content):
        path = os.path.join(self.directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def _spy_on_compiler(self):
        compiled = []
        original_compile = self.engine.compiler.compile

        def spy(template, verbose=False):
            compiled.append(template.content)
            return original_compile(template, verbose=verbose)

        self.engine.compiler.compile = spy
        return compiled

    def test_load_registers_all_templates(self):
        self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.assertIn('a.html', self.engine.templates)
        self.assertIn('b.html', self.engine.templates)

    def test_load_is_transactional(self):
        self._write('a.html', 'A')
        # Loading the same directory twice forces every name to conflict.
        loader = TemplateLoader([self.directory, self.directory], TemplateEngine())
        with self.assertRaises(ConflictingNames):
            loader.load()
        self.assertEqual(len(loader.engine.templates), 0)

    async def test_reload_recompiles_only_the_modified_template(self):
        self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()

        compiled = self._spy_on_compiler()
        time.sleep(0.02)
        self._write('a.html', 'A2')
        self.loader.check_for_modified_templates()

        self.assertEqual(compiled, ['A2'])
        self.assertEqual(await self.engine.render('a.html'), 'A2')
        self.assertEqual(await self.engine.render('b.html'), 'B')

    async def test_reload_recompiles_dependents_but_nothing_else(self):
        self._write('base.html', 'base {% block content %}nothing{% endblock %}')
        self._write('child.html', '{% extends "base.html" %}{% block content %}child{% endblock %}')
        self._write('plain.html', 'plain')
        self.loader.load()
        self.engine.compile_templates()

        compiled = self._spy_on_compiler()
        time.sleep(0.02)
        self._write('base.html', 'base2 {% block content %}nothing{% endblock %}')
        self.loader.check_for_modified_templates()

        # Only the modified template and its dependent are recompiled.
        self.assertEqual(len(compiled), 2)
        self.assertNotIn('plain', compiled)
        self.assertEqual(await self.engine.render('plain.html'), 'plain')
        rendered = await self.engine.render('child.html')
        self.assertIn('base2', rendered)
        self.assertIn('child', rendered)

    async def test_first_polling_cycle_after_load_does_not_recompile(self):
        self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()

        compiled = self._spy_on_compiler()
        self.loader.check_for_modified_templates()
        self.assertEqual(compiled, [])
        self.assertEqual(await self.engine.render('a.html'), 'A')

    async def test_failed_reload_restores_previous_state(self):
        from vibora.templates.exceptions import InvalidTag
        self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()

        time.sleep(0.02)
        self._write('a.html', '{% invalidtag %}')
        with self.assertRaises(InvalidTag):
            self.loader.check_for_modified_templates()

        # The broken template is rejected and the previous version restored.
        self.assertEqual(await self.engine.render('a.html'), 'A')
        self.assertEqual(await self.engine.render('b.html'), 'B')
