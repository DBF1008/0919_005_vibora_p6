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
        # Loader indexes are derived state: snapshot them so a failed reload
        # leaves the loader consistent with the (rolled back) engine.
        path_index_snapshot = dict(self.path_index)
        hash_index_snapshot = dict(self.hash_index)
        try:
            with self.engine.transaction():
                reloaded = []
                for root, path in paths:
                    if path in self.path_index:
                        template = self.path_index[path]

                        # Searching for templates who depends on this one.
                        relationships = []
                        for meta in self.engine.cache.loaded_metas.values():
                            if template.hash in meta.dependencies:
                                relationships.append(meta.template_hash)

                        # In case we found dependencies we need to reload them too.
                        for template_hash in relationships:
                            if template_hash in self.hash_index:
                                values = self.hash_index[template_hash]
                                self.engine.remove_template(values[2])
                                reloaded.append(self.add_to_engine(values[0], values[1]))

                        # Removing the actual template.
                        self.engine.remove_template(template)

                    reloaded.append(self.add_to_engine(root, path))
                self.engine.sync_cache()
                # Incremental recompile: only the changed templates and their
                # dependents are compiled again instead of the whole project.
                self.engine.compile_templates(templates=reloaded)
        except Exception:
            self.path_index.clear()
            self.path_index.update(path_index_snapshot)
            self.hash_index.clear()
            self.hash_index.update(hash_index_snapshot)
            raise

    def check_for_modified_templates(self):
        to_be_notified = []
        mtimes = {}
        for path in self.directories:
            for root, dirs, files in os.walk(path):
                for file in [f for f in files if f.endswith(self.supported_files)]:
                    path = os.path.join(root, file)
                    try:
                        last_modified = os.path.getmtime(path)
                    except FileNotFoundError:
                        continue
                    mtimes[path] = last_modified
                    if self.cache.get(path) != last_modified:
                        to_be_notified.append((root, path))
        if to_be_notified:
            # Only mark files as processed after a successful reload, so a
            # failed (rolled back) reload is retried on the next tick.
            self.reload_templates(to_be_notified)
        self.cache.update(mtimes)

    def add_to_engine(self, root: str, path: str):
        with open(path, 'r') as f:
            template = Template(f.read())
            names = get_import_names(root, path)
            template = self.engine.add_template(template, names=names)
            template.origin = path
            self.path_index[path] = template
            self.hash_index[template.hash] = (root, path, template)
            return template

    def load(self):
        with self.engine.transaction():
            for directory in self.directories:
                for root, dirs, files in os.walk(directory):
                    for file in files:
                        if file.endswith(self.supported_files):
                            path = os.path.join(root, file)
                            self.add_to_engine(root, path)
                            try:
                                # Remembering the current mtime so the watcher
                                # does not reload everything on its first tick.
                                self.cache[path] = os.path.getmtime(path)
                            except FileNotFoundError:
                                continue

    def run(self):
        while self.has_to_run:
            try:
                self.check_for_modified_templates()
            except Exception as error:
                # A broken template must not kill the debug watcher: the
                # engine was rolled back and the reload will be retried.
                print(f'TemplateLoader: reload failed, keeping previous state. ({error})')
            time.sleep(self.interval)
