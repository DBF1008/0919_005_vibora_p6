import hashlib
import importlib.util
import os
import tempfile
import time
import datetime
from setuptools import Extension, setup
from ..compilers.base import TemplateCompiler
from ..utils import find_template_binary, CompilerFlavor, TemplateMeta, get_architecture_signature, CompilationResult


# TODO: Remove 'render' hardcoded name.


class TemplateSourceError(Exception):
    """
    Wraps a runtime error raised inside a Cython compiled template, enriched
    with the original template location (file/name, line and source code).
    """

    def __init__(self, origin: str, line_number, source: str, original: Exception):
        self.template_origin = origin
        self.template_line_number = line_number
        self.template_source = source
        self.original_exception = original
        super().__init__(
            f'Template "{origin}" raised {original.__class__.__name__} at line '
            f'{line_number}: {source} ({original})'
        )


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
        self.pending_comment = None
        self.source_map = {}
        self.current_template_source = None
        self.template_content = ''
        self.template_line_cache = {}
        self.current_line = 0

    def clean(self):
        self._indentation = 0
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.functions = []
        self.flavor = CompilerFlavor.TEMPLATE
        self.pending_comment = None
        self.source_map = {}
        self.current_template_source = None
        self.template_content = ''
        self.template_line_cache = {}
        self.current_line = 0

    def add_comment(self, content: str):
        """

        :param content:
        :return:
        """
        self.pending_comment = ' '.join(content.split())

    def add_text(self, content: str):
        content = content.replace("\n", "\\n")
        content = content.replace(r'"', r'\"')
        self.accumulated_text += content

    def add_eval(self, statement: str):
        """

        :param statement:
        :return:
        """
        self.add_statement(f'{self.content_var}.append(str({statement}))')

    def flush_text(self):
        text = self.accumulated_text
        self.accumulated_text = ''
        stm = f'{self.content_var}.append("{text}")'
        self.add_statement(stm)

    def add_statement(self, content: str):
        if self.accumulated_text:
            self.flush_text()
        if self.pending_comment is not None:
            # Emitting the original template tag as a comment and remembering
            # it as the source of the statements that follow it.
            self.content += (' ' * self._indentation) + '# ' + self.pending_comment + '\n'
            self.current_line += 1
            self.current_template_source = self.pending_comment
            self.pending_comment = None
        self.current_line += 1
        if self.current_template_source is not None:
            self.source_map[self.current_line] = self.current_template_source
        new_content = (' ' * self._indentation) + content.strip() + '\n'
        self.content += new_content

    def consume(self, template):
        self.template_content = getattr(template, 'content', '') or ''
        self.add_statement(f'cpdef str render(dict {self.context_var}):')
        self._indentation += 4
        self.add_statement(f"cdef list {self.content_var} = []")
        template.ast.compile(self)
        self.add_statement(f'return "".join({self.content_var})')
        self._indentation -= 4

    def template_line_for(self, source: str):
        """
        Maps a raw template tag back to its line number in the original
        template source (None when it cannot be located, e.g. nodes merged
        from parent templates).

        :param source:
        :return:
        """
        if source not in self.template_line_cache:
            index = self.template_content.find(source)
            line = self.template_content.count('\n', 0, index) + 1 if index >= 0 else None
            self.template_line_cache[source] = line
        return self.template_line_cache[source]

    def build_module(self, template):
        """
        Generates the .pyx source for a template together with its source map
        ({generated line: (template line, template source)}). The map plus the
        template origin are embedded in the module itself as variables so the
        information survives binary caching and runtime exceptions can always
        be traced back to the original template position.

        :param template:
        :return:
        """
        self.consume(template)
        template_hash = hashlib.md5(self.content.encode()).hexdigest()
        module_name = 'vt_' + template_hash + '.pyx'
        origin = getattr(template, 'origin', None) or template.hash
        header = 'from vibora.templates.compilers.helpers import *\n\n'
        for helper_function in self.functions:
            header += helper_function + '\n\n'
        line_offset = header.count('\n')
        source_map = {
            line + line_offset: (self.template_line_for(source), source)
            for line, source in self.source_map.items()
        }
        source = header + self.content
        source += f'\n__template_source_map__ = {source_map!r}\n'
        source += f'__template_origin__ = {origin!r}\n'
        source += f'__template_module__ = {module_name!r}\n'
        return source, source_map, origin, module_name, template_hash

    @staticmethod
    def locate_source(traceback, module_name: str, source_map: dict):
        """
        Finds the deepest traceback frame belonging to the compiled template
        module and translates its line number through the source map.

        :param traceback:
        :param module_name:
        :param source_map:
        :return:
        """
        line_number = None
        current = traceback
        while current is not None:
            if os.path.basename(current.tb_frame.f_code.co_filename) == module_name:
                line_number = current.tb_lineno
            current = current.tb_next
        if line_number is None:
            return None
        mapped = source_map.get(line_number)
        if mapped is None:
            # Falling back to the closest mapped line above the failure point.
            candidates = [line for line in source_map if line <= line_number]
            if not candidates:
                return None
            mapped = source_map[max(candidates)]
        return mapped

    @classmethod
    def wrap_module_render(cls, compiled_module, entry_point: str='render'):
        """
        Wraps the module render function so runtime exceptions are re-raised
        as TemplateSourceError carrying the original template location.
        Modules without an embedded source map are returned untouched.

        :param compiled_module:
        :param entry_point:
        :return:
        """
        render_function = getattr(compiled_module, entry_point)
        source_map = getattr(compiled_module, '__template_source_map__', None)
        if not source_map:
            return render_function
        origin = getattr(compiled_module, '__template_origin__', None)
        module_name = getattr(compiled_module, '__template_module__', '')

        def render(context):
            try:
                return render_function(context)
            except Exception as error:
                location = cls.locate_source(error.__traceback__, module_name, source_map)
                if location is None:
                    raise
                line_number, source = location
                raise TemplateSourceError(origin, line_number, source, error) from error

        return render

    def create_new_macro(self, definition: str):
        new_compiler = self.__class__(flavor=CompilerFlavor.MACRO)
        new_compiler.add_statement('def ' + definition + ':')
        new_compiler.indent()
        new_compiler.add_statement(f"{self.content_var} = []")
        return new_compiler

    @classmethod
    def load_compiled_template(cls, meta: TemplateMeta, content: bytes):
        f = tempfile.NamedTemporaryFile(mode='wb', suffix='.so')
        f.file.write(content)
        f.file.flush()
        spec = importlib.util.spec_from_file_location(cls.EXTENSION_NAME, f.name)
        compiled_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_module)
        return cls.wrap_module_render(compiled_module, meta.entry_point)

    def compile(self, template, verbose: bool=False) -> CompilationResult:
        """

        :param verbose:
        :param template:
        :return:
        """
        # Tracking compile times
        started_at = time.time()

        # Temporary directory for this compilation.
        working_dir = tempfile.TemporaryDirectory(dir=self.temporary_dir)

        # Generating .pyx files (with the embedded source map).
        source, source_map, origin, module_name, template_hash = self.build_module(template)
        temp_path = os.path.join(working_dir.name, module_name)
        with open(temp_path, 'w') as f:
            f.write(source)

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
        meta = TemplateMeta(
            entry_point='render',
            version=self.VERSION,
            compiler=self.NAME,
            template_hash=template.hash,
            created_at=datetime.datetime.now().isoformat(),
            architecture=get_architecture_signature(),
            compilation_time=round(time.time() - started_at, 2),
            dependencies=template.dependencies
        )

        # Compilation result contains the meta data and the render function loaded at runtime.
        compilation = CompilationResult(
            template=template,
            meta=meta, render_function=self.wrap_module_render(compiled_template),
            code=open(compiled_path, 'rb').read()
        )

        # Clearing state
        self.clean()

        # Binding the render function
        return compilation

    @classmethod
    def generate_template_name(cls, hash_: str):
        return hash_ + '.so'
