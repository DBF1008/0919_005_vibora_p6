import hashlib
import importlib.util
import os
import tempfile
import time
import datetime
from ..compilers.base import TemplateCompiler
from ..exceptions import TemplateRenderError
from ..utils import find_template_binary, CompilerFlavor, TemplateMeta, get_architecture_signature, CompilationResult


# TODO: Remove 'render' hardcoded name.


class CythonTemplateCompiler(TemplateCompiler):

    NAME = 'cython'
    VERSION = '0.0.1'
    EXTENSION_NAME = 'compiled_templates'

    def __init__(self, flavor=CompilerFlavor.TEMPLATE, temporary_dir: str=None):
        super().__init__()
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.content_var = '__content__'
        self.context_var = '__context__'
        self.functions = []
        self.flavor = flavor
        self.temporary_dir = temporary_dir or tempfile.gettempdir()
        # Source mapping state: maps generated .pyx lines back to template lines.
        self.source_map = {}
        self.template_line = 1
        self.template_content = ''
        self.macro_compilers = []
        self._content_line = 0
        self._root = None
        self._search_offset = 0

    def clean(self):
        self._indentation = 0
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.functions = []
        self.flavor = CompilerFlavor.TEMPLATE
        self.source_map = {}
        self.template_line = 1
        self.template_content = ''
        self.macro_compilers = []
        self._content_line = 0
        self._root = None
        self._search_offset = 0

    @property
    def root_compiler(self):
        """Macros are compiled by child compilers but share the same template."""
        return self._root or self

    def add_comment(self, content: str):
        """
        Called by nodes before emitting their statements. We use the raw tag
        to locate ourselves in the original template, tracking line numbers
        so runtime errors can be mapped back to the template source.
        """
        raw = content.strip()
        root = self.root_compiler
        if root.template_content and raw:
            position = root.template_content.find(raw, root._search_offset)
            if position == -1:
                # Nodes from parent templates (extends/merge) may not be
                # found ahead of the cursor, so we search from the start.
                position = root.template_content.find(raw)
            if position != -1:
                self.template_line = root.template_content.count('\n', 0, position) + 1
                root._search_offset = position + len(raw)
        self.add_statement('# ' + raw, map_to_template=False)

    def add_text(self, content: str):
        content = content.replace("\n", "\\n")
        content = content.replace(r'"', r'\"')
        self.accumulated_text += content

    def flush_text(self):
        text = self.accumulated_text
        self.accumulated_text = ''
        stm = f'{self.content_var}.append("{text}")'
        self.add_statement(stm)

    def add_eval(self, statement: str):
        self.add_statement(f'{self.content_var}.append({statement})')

    def add_statement(self, content: str, map_to_template: bool=True):
        if self.accumulated_text:
            self.flush_text()
        self._content_line += 1
        if map_to_template:
            self.source_map[self._content_line] = self.template_line
        new_content = (' ' * self._indentation) + content.strip() + '\n'
        self.content += new_content

    def consume(self, template):
        self.template_content = template.content or ''
        self._search_offset = 0
        self.add_statement('from vibora.templates.compilers.helpers import *', map_to_template=False)
        self.add_statement(f'cpdef str render(dict {self.context_var}):')
        self._indentation += 4
        self.add_statement(f"cdef list {self.content_var} = []")
        template.ast.compile(self)
        self.add_statement(f'return "".join({self.content_var})')
        self._indentation -= 4

    def create_new_macro(self, definition: str):
        new_compiler = self.__class__(flavor=CompilerFlavor.MACRO)
        new_compiler._root = self.root_compiler
        new_compiler.template_content = self.template_content
        new_compiler.template_line = self.template_line
        new_compiler.add_statement('def ' + definition + ':')
        new_compiler.indent()
        new_compiler.add_statement(f"{self.content_var} = []")
        self.root_compiler.macro_compilers.append(new_compiler)
        return new_compiler

    def build_source_map(self) -> dict:
        """
        Assembles the final source map ({pyx_line: template_line}) accounting
        for the macro/helper functions placed before the main render function.
        """
        final_map = {}
        offset = 0
        for index, helper_function in enumerate(self.functions):
            if index < len(self.macro_compilers):
                for line, template_line in self.macro_compilers[index].source_map.items():
                    final_map[offset + line] = template_line
            offset += helper_function.count('\n') + 2
        for line, template_line in self.source_map.items():
            final_map[offset + line] = template_line
        return final_map

    @staticmethod
    def lookup_template_line(source_map: dict, pyx_line: int):
        """Exact match or closest statement above the failing line."""
        normalized = {int(line): template_line for line, template_line in source_map.items()}
        if pyx_line in normalized:
            return normalized[pyx_line]
        candidates = [line for line in normalized if line <= pyx_line]
        if candidates:
            return normalized[max(candidates)]
        return None

    @classmethod
    def find_template_location(cls, error: Exception, source_map: dict):
        """Walks the traceback looking for frames generated from a template .pyx file."""
        template_line = None
        traceback = error.__traceback__
        while traceback is not None:
            filename = os.path.basename(traceback.tb_frame.f_code.co_filename)
            if filename.startswith('vt_') and filename.endswith('.pyx'):
                mapped = cls.lookup_template_line(source_map, traceback.tb_lineno)
                if mapped is not None:
                    template_line = mapped
            traceback = traceback.tb_next
        return template_line

    @classmethod
    def wrap_render(cls, render_function, source_map: dict, template_name: str, template_content: str=''):
        """
        Wraps the compiled render function so runtime exceptions are
        re-raised as TemplateRenderError pointing back to the original
        template file and line number.
        """
        def render(context):
            try:
                return render_function(context)
            except Exception as error:
                line_number = cls.find_template_location(error, source_map)
                source_line = ''
                if line_number is not None and template_content:
                    lines = template_content.splitlines()
                    if 1 <= line_number <= len(lines):
                        source_line = lines[line_number - 1].strip()
                wrapped = TemplateRenderError(template=None, template_line=source_line,
                                              exception=error, template_name=template_name)
                wrapped.template_line_number = line_number
                wrapped.template_file = template_name
                raise wrapped
        return render

    @classmethod
    def load_compiled_template(cls, meta: TemplateMeta, content: bytes):
        f = tempfile.NamedTemporaryFile(mode='wb', suffix='.so')
        f.file.write(content)
        f.file.flush()
        spec = importlib.util.spec_from_file_location(cls.EXTENSION_NAME, f.name)
        compiled_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_module)
        render_function = getattr(compiled_module, meta.entry_point)
        source_map = getattr(meta, 'source_map', None) or {}
        return cls.wrap_render(render_function, source_map, template_name=meta.template_hash)

    def compile(self, template, verbose: bool=False) -> CompilationResult:
        """

        :param verbose:
        :param template:
        :return:
        """
        # Imported lazily so this module stays importable (and cached
        # templates loadable) on machines without a build toolchain.
        from setuptools import Extension, setup

        # Tracking compile times
        started_at = time.time()

        # Temporary directory for this compilation.
        working_dir = tempfile.TemporaryDirectory(dir=self.temporary_dir)

        # Generating .pyx files.
        self.consume(template)
        template_hash = hashlib.md5(self.content.encode()).hexdigest()
        temp_path = os.path.join(working_dir.name, 'vt_' + template_hash + '.pyx')
        source_map = self.build_source_map()
        with open(temp_path, 'w') as f:
            for helper_function in self.functions:
                f.write(helper_function + '\n\n')
            f.write(self.content)

        # Building optimized binaries.
        ext = Extension(self.EXTENSION_NAME, [temp_path], extra_compile_args=['-O3'], include_dirs=['.'])
        build_path = os.path.join(working_dir.name, template_hash)
        trash_dir = os.path.join(working_dir.name, 'trash')
        args = ['build_ext', '-b', build_path, '-t', trash_dir]
        if not verbose:
            args = ['-q'] + args
        setup(ext_modules=[ext], script_args=args)

        # Loading modules.
        compiled_path = find_template_binary(build_path)
        spec = importlib.util.spec_from_file_location(self.EXTENSION_NAME, compiled_path)
        compiled_template = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_template)

        # Generating meta data about this compilation so we can correctly
        # cache and load these templates later.
        template_name = getattr(template, 'name', None) or template.hash
        meta = TemplateMeta(
            entry_point='render',
            version=self.VERSION,
            compiler=self.NAME,
            template_hash=template.hash,
            created_at=datetime.datetime.now().isoformat(),
            architecture=get_architecture_signature(),
            compilation_time=round(time.time() - started_at, 2),
            dependencies=template.dependencies,
            source_map=source_map
        )

        # Compilation result contains the meta data and the render function loaded at runtime.
        compilation = CompilationResult(
            template=template,
            meta=meta, code=open(compiled_path, 'rb').read(),
            render_function=self.wrap_render(compiled_template.render, source_map,
                                             template_name=template_name,
                                             template_content=template.content)
        )

        # Clearing state
        self.clean()

        # Binding the render function
        return compilation

    @classmethod
    def generate_template_name(cls, hash_: str):
        return hash_ + '.so'
