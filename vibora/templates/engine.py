from contextlib import contextmanager
from typing import Dict, List, Tuple
from .ast import merge, raise_nodes, resolve_include_nodes
from .exceptions import TemplateNotFound, ConflictingNames
from .nodes import ExtendsNode, MacroNode
from .template import Template, TemplateParser, ParsedTemplate, CompiledTemplate
from .cache import InMemoryCache
from .compilers.python import PythonTemplateCompiler


class TemplateEngine:

    def __init__(self, cache_engine=None, compiler=None, extensions: list=None,
                 parser: TemplateParser=None):

        # Loaded templates list, caching purposes.
        # {Name: ParsedTemplate}
        self.templates: Dict[str, ParsedTemplate] = {}

        # This is a cache holding the functions responsible for actually rendering the template.
        # {TemplateHash: CompiledTemplate}
        self.compiled_templates: Dict[str, CompiledTemplate] = {}

        # Templates compiler, translate a template AST to a Python callable.
        self.compiler = compiler or PythonTemplateCompiler()

        # Extensions can modify template nodes before compilation.
        self.extensions = extensions if extensions else []

        # Template compilation is an expensive process so we try to cache as much as possible,
        # especially when using Cython compiler.
        self.cache = cache_engine or InMemoryCache()

        # Template parser.
        self.template_parser = parser or TemplateParser()

    def remove_template(self, template: ParsedTemplate):
        """

        :param template:
        :return:
        """
        keys_to_remove = []
        for name, t in self.templates.items():
            if t.hash == template.hash:
                keys_to_remove.append(name)
        for key in keys_to_remove:
            try:
                del self.templates[key]
            except KeyError:
                pass
            try:
                del self.compiled_templates[template.hash]
            except KeyError:
                pass
        self.cache.remove(template.hash)

    @contextmanager
    def transaction(self):
        """
        Atomic context for template loading. Any exception raised inside the
        block restores every piece of engine state (registered templates,
        compiled templates and cache entries) to its pre-transaction snapshot,
        so a failed batch never leaves partial side effects behind.

        :return:
        """
        templates_snapshot = dict(self.templates)
        compiled_snapshot = dict(self.compiled_templates)
        cached_templates_snapshot = dict(self.cache.loaded_templates)
        cached_metas_snapshot = dict(self.cache.loaded_metas)
        try:
            yield self
        except Exception:
            self.templates.clear()
            self.templates.update(templates_snapshot)
            self.compiled_templates.clear()
            self.compiled_templates.update(compiled_snapshot)
            self.cache.loaded_templates.clear()
            self.cache.loaded_templates.update(cached_templates_snapshot)
            self.cache.loaded_metas.clear()
            self.cache.loaded_metas.update(cached_metas_snapshot)
            raise

    def add_templates(self, templates: List[Tuple[Template, list]]) -> List[ParsedTemplate]:
        """
        Atomically adds a batch of (template, names) tuples. Either every
        template is registered or, on any failure (conflicting names, invalid
        tags, etc), the engine is rolled back to its previous state.

        :param templates:
        :return:
        """
        with self.transaction():
            parsed_templates = []
            for template, names in templates:
                parsed_templates.append(self.add_template(template, names=names))
            return parsed_templates

    def add_template(self, template: Template, names: list) -> ParsedTemplate:
        """

        :param template:
        :param names:
        :return:
        """
        template = self.template_parser.parse(template)
        # Two-phase registration: conflicts are detected before any mutation
        # so a failed call never leaves the engine in a partial state.
        available_names = [name for name in names if name not in self.templates]
        if not available_names:
            raise ConflictingNames('This template needs a unique name because imports are name based.')
        for name in available_names:
            self.templates[name] = template
        if not hasattr(template, 'origin'):
            template.origin = available_names[0]
        return template

    async def render(self, name: str, streaming: bool=False, **template_vars):
        """

        :param streaming:
        :param name:
        :param template_vars:
        :return:
        """
        try:
            template = self.templates[name]
            try:
                compiled_template = self.compiled_templates[template.hash]
                template_generator = compiled_template.render({**template_vars})
                if streaming:
                    return template_generator
                try:
                    content = ''
                    async for chunk in template_generator:
                        content += chunk
                    return content
                except Exception as error:
                    raise compiled_template.render_exception(error, name=name)
            except KeyError:
                raise Exception('You need to compile your templates first.')
        except KeyError:
            raise TemplateNotFound(name)

    def get_template(self, name: str):
        """

        :param name:
        :return:
        """
        try:
            return self.templates[name]
        except KeyError:
            raise TemplateNotFound(name)

    def get_compiled_template(self, template: Template):
        """

        :param template:
        :return:
        """
        try:
            return self.compiled_templates[template.hash]
        except KeyError:
            raise TemplateNotFound(f'You need to compile this template first.')

    def prepare_template(self, template: ParsedTemplate):
        """

        :param template:
        :return:
        """

        # Checking if this template is already prepared.
        if template.prepared:
            return

        # Calling extensions so they have a chance to modify the template include/extends node.
        for extension in self.extensions:
            extension.before_prepare(self, template)

        # Resolving "include" nodes.
        relationships = resolve_include_nodes(self, template.ast.children)
        for t in relationships:
            template.dependencies.add(t.hash)

        # Resolving "extends" nodes.
        for index, node in enumerate(template.ast.children):
            if isinstance(node, ExtendsNode):
                parent = self.get_template(node.parent)
                template.dependencies.add(parent.hash)
                self.prepare_template(parent)
                template.ast = merge(parent, template)

        # Macro nodes needs to be compiled first.
        raise_nodes(lambda x: isinstance(x, MacroNode), template.ast)

        template.prepared = True

    def sync_cache(self):
        """

        :return:
        """
        updated_hashes = [t.hash for t in self.templates.values()]
        for template_hash, meta in self.cache.loaded_metas.items():
            if any([x for x in meta.dependencies if x not in updated_hashes]):
                self.cache.remove(template_hash)

    def compile_templates(self, verbose=False, templates: list=None):
        """

        :param verbose:
        :param templates: Optional subset of templates to (re)compile. When
        omitted every registered template is considered. Passing only the
        changed templates allows cheap incremental recompiles.
        :return:
        """
        targets = templates if templates is not None else self.templates.values()
        # Checking if all dependencies are met
        for template in targets:

            # Trying to load the compiled version from cache,
            # if not possible then let's call the compiler to build this template.
            compiled_template = self.cache.get(template.hash)
            if compiled_template is None:

                # Optimizing/Replacing nodes so we compile it with the final AST.
                if not template.prepared:
                    self.prepare_template(template)

                compiled_template = self.compiler.compile(template, verbose=verbose)
                self.cache.store(compiled_template)

            # Caching the render function for fast access.
            self.compiled_templates[template.hash] = compiled_template

        # Cleaning old template cache files.
        self.cache.clean(set([t.hash for t in self.templates.values()]))
