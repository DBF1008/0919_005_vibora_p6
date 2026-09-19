import time
import os
import threading
from .engine import TemplateEngine
from .template import Template
from .utils import get_import_names


class TemplateLoader(threading.Thread):
    def __init__(self, directories: list, engine: TemplateEngine, supported_files: list=None, interval: int=0.5):
        super().__init__()
        self.directories = directories
        self.engine = engine
        self.supported_files = supported_files or ('.html', '.vib')
        self.cache = {}
        self.path_index = {}
        self.hash_index = {}
        self.interval = interval
        self.has_to_run = True

    def reload_templates(self, paths: list):
        # Collecting every affected template: the modified ones plus
        # every template that depends on them (include/extends).
        to_reload = {}
        removed = {}
        for root, path in paths:
            if path in self.path_index:
                template = self.path_index[path]

                # Searching for templates who depends on this one.
                for meta in self.engine.cache.loaded_metas.values():
                    if template.hash in meta.dependencies and meta.template_hash in self.hash_index:
                        dep_root, dep_path, dep_template = self.hash_index[meta.template_hash]
                        removed[dep_template.hash] = (dep_root, dep_path, dep_template)
                        to_reload[dep_path] = (dep_root, dep_path)

                removed[template.hash] = (root, path, template)
            to_reload[path] = (root, path)

        # Removing outdated templates from the engine.
        for root, path, template in removed.values():
            self.engine.remove_template(template)
            self.path_index.pop(path, None)
            self.hash_index.pop(template.hash, None)

        # Reading the new versions from disk.
        batch = []
        for root, path in to_reload.values():
            try:
                with open(path, 'r') as f:
                    batch.append((root, path, Template(f.read())))
            except FileNotFoundError:
                continue

        # Transactional registration: either every template is
        # reloaded or the engine is restored to its previous state.
        try:
            parsed_templates = self.engine.add_templates(
                [(template, get_import_names(root, path)) for root, path, template in batch]
            )
        except Exception:
            rollback = self.engine.add_templates(
                [(template, get_import_names(root, path)) for root, path, template in removed.values()]
            )
            for (root, path, template), parsed in zip(removed.values(), rollback):
                self.path_index[path] = parsed
                self.hash_index[parsed.hash] = (root, path, parsed)
            # Restoring the compiled state so the engine keeps working
            # exactly as it did before the failed reload.
            self.engine.sync_cache()
            self.engine.compile_templates(templates=rollback)
            raise

        # Updating indexes.
        for (root, path, template), parsed in zip(batch, parsed_templates):
            self.path_index[path] = parsed
            self.hash_index[parsed.hash] = (root, path, parsed)

        # Incremental recompilation: only the affected templates are
        # recompiled instead of the entire template set.
        self.engine.sync_cache()
        self.engine.compile_templates(templates=parsed_templates)

    def check_for_modified_templates(self):
        to_be_notified = []
        for path in self.directories:
            for root, dirs, files in os.walk(path):
                for file in [f for f in files if f.endswith(self.supported_files)]:
                    path = os.path.join(root, file)
                    try:
                        last_modified = os.path.getmtime(path)
                        if path in self.cache:
                            if self.cache[path] != last_modified:
                                to_be_notified.append((root, path))
                        else:
                            to_be_notified.append((root, path))
                        self.cache[path] = last_modified
                    except FileNotFoundError:
                        continue
        if to_be_notified:
            self.reload_templates(to_be_notified)

    def load(self):
        batch = []
        for directory in self.directories:
            for root, dirs, files in os.walk(directory):
                for file in files:
                    if file.endswith(self.supported_files):
                        path = os.path.join(root, file)
                        with open(path, 'r') as f:
                            batch.append((root, path, Template(f.read())))
                        # Keeping the mtime cache in sync so the first polling
                        # cycle doesn't mistake every file for a new one.
                        self.cache[path] = os.path.getmtime(path)
        # Transactional loading: a single conflicting template
        # aborts the whole load instead of leaving partial state.
        parsed_templates = self.engine.add_templates(
            [(template, get_import_names(root, path)) for root, path, template in batch]
        )
        for (root, path, template), parsed in zip(batch, parsed_templates):
            self.path_index[path] = parsed
            self.hash_index[parsed.hash] = (root, path, parsed)

    def run(self):
        while self.has_to_run:
            try:
                self.check_for_modified_templates()
            except Exception as error:
                # A broken template on disk must not kill the watcher.
                # The transactional reload guarantees the engine state
                # is left untouched, so we can safely keep polling.
                print(f'Failed to reload templates: {error}')
            time.sleep(self.interval)
