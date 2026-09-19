"""
Unit tests for the source map injected by the Cython template compiler.

These tests exercise the source map generation and the runtime traceback
translation without requiring a C compiler: the "compiled module" is
simulated by exec'ing Python code with the same filename a real .pyx
module would carry in its tracebacks.

Run manually with: python3 tests/templates/cython_source_map.py
"""
import os
import sys
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, REPO_ROOT)
if 'vibora' not in sys.modules:
    package = types.ModuleType('vibora')
    package.__path__ = [os.path.join(REPO_ROOT, 'vibora')]
    sys.modules['vibora'] = package
if 'setuptools' not in sys.modules:
    # The cython compiler imports setuptools at module level; it is only
    # needed for the actual binary build which these tests never reach.
    setuptools_stub = types.ModuleType('setuptools')
    setuptools_stub.Extension = type('Extension', (), {'__init__': lambda self, *a, **k: None})
    setuptools_stub.setup = lambda *a, **k: None
    sys.modules['setuptools'] = setuptools_stub

from vibora.templates import Template
from vibora.templates.template import TemplateParser
from vibora.templates.compilers.cython import CythonTemplateCompiler, TemplateSourceError


def make_fake_compiled_module(source, source_map, origin='index.html', module_name='vt_fake.pyx'):
    namespace = {}
    exec(compile(source, module_name, 'exec'), namespace)
    module = types.ModuleType('compiled_templates')
    module.render = namespace['render']
    module.__template_source_map__ = source_map
    module.__template_origin__ = origin
    module.__template_module__ = module_name
    return module


class SourceMapGenerationSuite(unittest.TestCase):

    def parse(self, content, origin='index.html'):
        template = TemplateParser().parse(Template(content))
        template.origin = origin
        return template

    def test_module_source_embeds_map_and_origin(self):
        compiler = CythonTemplateCompiler()
        source, source_map, origin, module_name, template_hash = compiler.build_module(
            self.parse('hello {{ who }}'))
        self.assertIn('__template_source_map__', source)
        self.assertIn('__template_origin__', source)
        self.assertIn('__template_module__', source)
        self.assertEqual(origin, 'index.html')
        self.assertTrue(module_name.endswith('.pyx'))
        self.assertTrue(module_name.startswith('vt_'))

    def test_original_tags_are_emitted_as_comments(self):
        compiler = CythonTemplateCompiler()
        source, _, _, _, _ = compiler.build_module(self.parse('hello {{ who }}'))
        self.assertIn('# {{ who }}', source)

    def test_source_map_points_to_original_template_lines(self):
        content = 'line one\nline two\n{{ missing_value }}\nline four'
        compiler = CythonTemplateCompiler()
        _, source_map, _, _, _ = compiler.build_module(self.parse(content))
        mapped = [info for info in source_map.values() if info[1] == '{{ missing_value }}']
        self.assertTrue(mapped, 'expected generated lines mapped to the eval tag')
        for template_line, _ in mapped:
            self.assertEqual(template_line, 3)

    def test_origin_falls_back_to_template_hash(self):
        template = self.parse('hello')
        del template.origin
        compiler = CythonTemplateCompiler()
        _, _, origin, _, _ = compiler.build_module(template)
        self.assertEqual(origin, template.hash)


class TracebackTranslationSuite(unittest.TestCase):

    def test_exception_is_translated_to_template_position(self):
        module_source = (
            'def render(context):\n'
            '    value = context["x"]\n'
            '    raise ValueError("boom")\n'
        )
        module = make_fake_compiled_module(
            module_source, source_map={2: (5, '{{ x }}'), 3: (7, '{{ y }}')})
        render = CythonTemplateCompiler.wrap_module_render(module)
        with self.assertRaises(TemplateSourceError) as ctx:
            render({'x': 1})
        error = ctx.exception
        self.assertEqual(error.template_origin, 'index.html')
        self.assertEqual(error.template_line_number, 7)
        self.assertEqual(error.template_source, '{{ y }}')
        self.assertIsInstance(error.original_exception, ValueError)
        self.assertIsInstance(error.__cause__, ValueError)

    def test_unmapped_line_falls_back_to_closest_mapped_line(self):
        module_source = (
            'def render(context):\n'
            '    value = context["x"]\n'
            '    raise ValueError("boom")\n'
        )
        module = make_fake_compiled_module(module_source, source_map={2: (5, '{{ x }}')})
        render = CythonTemplateCompiler.wrap_module_render(module)
        with self.assertRaises(TemplateSourceError) as ctx:
            render({'x': 1})
        self.assertEqual(ctx.exception.template_line_number, 5)
        self.assertEqual(ctx.exception.template_source, '{{ x }}')

    def test_module_without_source_map_is_returned_untouched(self):
        module = types.ModuleType('compiled_templates')
        module.render = lambda context: 'ok'
        self.assertIs(CythonTemplateCompiler.wrap_module_render(module), module.render)

    def test_foreign_traceback_reraises_original_exception(self):
        module_source = (
            'def render(context):\n'
            '    raise ValueError("boom")\n'
        )
        module = make_fake_compiled_module(
            module_source, source_map={2: (5, '{{ x }}')}, module_name='vt_other.pyx')
        # Pretending the traceback comes from a different compiled module.
        module.__template_module__ = 'vt_expected.pyx'
        render = CythonTemplateCompiler.wrap_module_render(module)
        with self.assertRaises(ValueError):
            render({})

    def test_successful_render_passes_through(self):
        module_source = 'def render(context):\n    return "done"\n'
        module = make_fake_compiled_module(module_source, source_map={2: (1, '{{ x }}')})
        render = CythonTemplateCompiler.wrap_module_render(module)
        self.assertEqual(render({}), 'done')


if __name__ == '__main__':
    unittest.main(verbosity=2)
