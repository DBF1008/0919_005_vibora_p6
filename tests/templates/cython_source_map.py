import os
import tempfile

from vibora.templates import TemplateEngine, Template
from vibora.templates.compilers.cython import CythonTemplateCompiler
from vibora.templates.exceptions import TemplateRenderError
from vibora.templates.utils import TemplateMeta
from vibora.tests import TestSuite


class CythonSourceMapSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    def _generate(self, content):
        template = self.engine.add_template(Template(content), ['test'])
        self.engine.prepare_template(template)
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        return compiler

    def test_statements_map_back_to_template_lines(self):
        compiler = self._generate('line1\n{% for x in items %}\n{{ x }}\n{% endfor %}\n')
        source_map = compiler.build_source_map()
        lines = compiler.content.splitlines()
        for_line = next(i + 1 for i, line in enumerate(lines) if line.strip().startswith('async for'))
        eval_line = next(i + 1 for i, line in enumerate(lines) if '__temp__ = x' in line)
        self.assertEqual(source_map[for_line], 2)
        self.assertEqual(source_map[eval_line], 3)

    def test_raw_tags_are_emitted_as_comments(self):
        compiler = self._generate('{% for x in items %}{{ x }}{% endfor %}')
        self.assertIn('# {% for x in items %}', compiler.content)
        self.assertIn('# {{ x }}', compiler.content)

    def test_macro_functions_offset_the_source_map(self):
        compiler = self._generate(
            '{% macro hello(name) %}Hello {{ name }}{% endmacro %}\n{{ something }}\n'
        )
        self.assertEqual(len(compiler.functions), 1)
        offset = compiler.functions[0].count('\n') + 2
        source_map = compiler.build_source_map()
        for line, template_line in compiler.source_map.items():
            self.assertEqual(source_map[line + offset], template_line)

    def test_wrapped_render_translates_traceback_to_template_location(self):
        def fake_render(context):
            exec(compile('raise ValueError("boom")', '/tmp/vt_abc123.pyx', 'exec'))

        content = 'one\ntwo\nthree\nfour\nfive\nsix\nseven\n'
        render = CythonTemplateCompiler.wrap_render(fake_render, {1: 7}, 'page.html', content)
        with self.assertRaises(TemplateRenderError) as ctx:
            render({})
        error = ctx.exception
        self.assertEqual(error.template_line_number, 7)
        self.assertEqual(error.template_line, 'seven')
        self.assertEqual(error.template_file, 'page.html')
        self.assertIsInstance(error.original_exception, ValueError)

    def test_wrapped_render_ignores_non_template_frames(self):
        def fake_render(context):
            raise KeyError('nope')

        render = CythonTemplateCompiler.wrap_render(fake_render, {1: 1}, 'page.html', 'content')
        with self.assertRaises(TemplateRenderError) as ctx:
            render({})
        self.assertIsNone(ctx.exception.template_line_number)

    def test_lookup_falls_back_to_closest_statement_above(self):
        self.assertEqual(CythonTemplateCompiler.lookup_template_line({5: 2, 10: 4}, 8), 2)
        self.assertEqual(CythonTemplateCompiler.lookup_template_line({5: 2}, 5), 2)
        self.assertIsNone(CythonTemplateCompiler.lookup_template_line({}, 3))

    def test_lookup_accepts_json_serialized_keys(self):
        self.assertEqual(CythonTemplateCompiler.lookup_template_line({'5': 2}, 5), 2)

    def test_source_map_survives_meta_serialization(self):
        meta = TemplateMeta(
            entry_point='render', version='0.0.1', template_hash='hash', created_at='now',
            compiler='cython', architecture='arch', compilation_time=0.1, source_map={3: 5}
        )
        path = os.path.join(tempfile.gettempdir(), 'vibora_meta_test.json')
        try:
            meta.store(path)
            loaded = TemplateMeta.load_from_path(path)
            self.assertEqual(CythonTemplateCompiler.lookup_template_line(loaded.source_map, 3), 5)
        finally:
            os.remove(path)
