import json
import logging
import pathlib
import platform
import traceback
import typing
from functools import partial
from multiprocessing import Process
from multiprocessing import Queue
from typing import IO
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple

import pygls.uris as Uri
from docutils.parsers.rst import Directive
from pygls.lsp.types import DeleteFilesParams
from pygls.lsp.types import Diagnostic
from pygls.lsp.types import DiagnosticSeverity
from pygls.lsp.types import DidSaveTextDocumentParams
from pygls.lsp.types import InitializedParams
from pygls.lsp.types import InitializeParams
from pygls.lsp.types import MessageType
from pygls.lsp.types import Position
from pygls.lsp.types import Range
from pygls.lsp.types import ShowDocumentParams
from sphinx import __version__ as __sphinx_version__
from sphinx.application import Sphinx
from sphinx.domains import Domain
from sphinx.errors import ConfigError
from sphinx.util import console
from sphinx.util import logging as sphinx_logging_module
from sphinx.util.logging import NAMESPACE as SPHINX_LOG_NAMESPACE
from sphinx.util.logging import VERBOSITY_MAP

from esbonio.cli import setup_cli
from esbonio.lsp.rst import RstLanguageServer
from esbonio.lsp.sphinx.config import InitializationOptions
from esbonio.lsp.sphinx.config import MissingConfigError
from esbonio.lsp.sphinx.config import SphinxConfig
from esbonio.lsp.sphinx.config import SphinxLogHandler
from esbonio.lsp.sphinx.config import SphinxServerConfig
from esbonio.lsp.sphinx.preview import make_preview_server
from esbonio.lsp.sphinx.preview import start_preview_server

from .line_number_transform import LineNumberTransform

__all__ = [
    "InitializationOptions",
    "MissingConfigError",
    "SphinxConfig",
    "SphinxServerConfig",
    "SphinxLanguageServer",
]

IS_LINUX = platform.system() == "Linux"

# fmt: off
# Order matters!
DEFAULT_MODULES = [
    "esbonio.lsp.directives",         # Generic directive support
    "esbonio.lsp.roles",              # Generic roles support
    "esbonio.lsp.rst.directives",     # Specialised support for docutils directives
    "esbonio.lsp.rst.roles",          # Specialised support for docutils roles
    "esbonio.lsp.sphinx.codeblocks",  # Support for code-block, highlight, etc.
    "esbonio.lsp.sphinx.domains",     # Support for Sphinx domains
    "esbonio.lsp.sphinx.directives",  # Specialised support for Sphinx directives
    "esbonio.lsp.sphinx.images",      # Support for image, figure etc
    "esbonio.lsp.sphinx.includes",    # Support for include, literal-include etc.
    "esbonio.lsp.sphinx.roles",       # Support for misc roles added by Sphinx e.g. :download:
]
"""The modules to load in the default configuration of the server."""
# fmt: on


class SphinxLanguageServer(RstLanguageServer):
    """A language server dedicated to working with Sphinx projects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.app: Optional[Sphinx] = None
        """The Sphinx application instance."""

        self.sphinx_args: Dict[str, Any] = {}
        """The current Sphinx configuration will all variables expanded."""

        self.sphinx_log: Optional[SphinxLogHandler] = None
        """Logging handler for sphinx messages."""

        self.preview_process: Optional[Process] = None
        """The process hosting the preview server."""

        self.preview_port: Optional[int] = None
        """The port the preview server is running on."""

        self._role_target_types: Optional[Dict[str, List[str]]] = None
        """Cache for role target types."""

        self.is_preview: bool = False
        """Enable callbacks, that limit and inject files to only the current unsaved file."""

        self.preview_docname: str = ""
        """Name of the current (unsaved) file."""

        self.preview_content: str = ""
        """Content of the current (unsaved) file."""

        self.enableLivePreview: bool = False
        """Enable necessary feature for scroll sync of the rst previewer."""

        self.buildSphinxOnChange: bool = False
        """Enable Sphinx build for unsaved file content for onChange event"""

    @property
    def configuration(self) -> Dict[str, Any]:
        """Return the server's actual configuration."""
        config = super().configuration
        sphinx_config = SphinxConfig.from_arguments(sphinx_args=self.sphinx_args)

        if sphinx_config is None:
            self.logger.error("Unable to determine SphinxConfig!")
            return config

        if self.user_config is None:
            self.logger.error("Unable to determine user config!")
            return config

        # We always run Sphinx in "'-Q' mode", so we need to go back to the user's
        # config to get those values.
        sphinx_config.silent = self.user_config.sphinx.silent  # type: ignore
        sphinx_config.quiet = self.user_config.sphinx.quiet  # type: ignore

        # 'Make mode' isn't something that can be inferred from Sphinx args either.
        sphinx_config.make_mode = self.user_config.sphinx.make_mode  # type: ignore

        config["sphinx"] = dict(**sphinx_config.dict(by_alias=True))
        config["sphinx"]["command"] = ["sphinx-build"] + sphinx_config.to_cli_args()
        config["sphinx"]["version"] = __sphinx_version__

        config["server"] = self.user_config.server.dict(by_alias=True)  # type: ignore

        self.enableLivePreview = self.user_config.sphinx.enableLivePreview
        self.buildSphinxOnChange = self.user_config.sphinx.buildSphinxOnChange

        return config

    def initialize(self, params: InitializeParams):
        super().initialize(params)
        self.user_config = InitializationOptions(
            **typing.cast(Dict, params.initialization_options)
        )

    def initialized(self, params: InitializedParams):
        self.app = self._initialize_sphinx()
        self.build()

    def _initialize_sphinx(self):

        try:
            return self.create_sphinx_app(self.user_config)
        except MissingConfigError:
            self.show_message(
                message="Unable to find your 'conf.py', features that depend on Sphinx will be unavailable",
                msg_type=MessageType.Warning,
            )
            self.send_notification(
                "esbonio/buildComplete",
                {
                    "config": self.configuration,
                    "error": True,
                    "warnings": 0,
                },
            )
        except Exception as exc:
            self.logger.error(traceback.format_exc())
            uri, diagnostic = exception_to_diagnostic(exc)
            self.set_diagnostics("conf.py", uri, [diagnostic])

            self.sync_diagnostics()
            self.send_notification(
                "esbonio/buildComplete",
                {"config": self.configuration, "error": True, "warnings": 0},
            )

    def on_shutdown(self, *args):

        if self.preview_process:

            if not hasattr(self.preview_process, "kill"):
                self.preview_process.terminate()
            else:
                self.preview_process.kill()

    def save(self, params: DidSaveTextDocumentParams):
        super().save(params)

        filepath = Uri.to_fs_path(params.text_document.uri)
        if filepath.endswith("conf.py"):
            if self.app:
                conf_dir = pathlib.Path(self.app.confdir)
            else:
                # The user's config is currently broken... where should their conf.py be?
                if self.user_config is not None:
                    config = typing.cast(InitializationOptions, self.user_config).sphinx
                else:
                    config = SphinxConfig()

                conf_dir = config.resolve_conf_dir(
                    self.workspace.root_uri
                ) or pathlib.Path(".")

            if str(conf_dir / "conf.py") == filepath:
                self.clear_diagnostics("conf.py")
                self.sync_diagnostics()
                self.app = self._initialize_sphinx()

        else:
            self.clear_diagnostics("sphinx", params.text_document.uri)

        self.build()

    def delete_files(self, params: DeleteFilesParams):
        self.logger.debug("Deleted files: %s", params.files)

        # Files don't exist anymore, so diagnostics must be cleared.
        for file in params.files:
            self.clear_diagnostics("sphinx", file.uri)

        self.build()

    def remove_prefix(self, text, prefix):
        return text[len(prefix) :] if text.startswith(prefix) else text

    """
    Trigger sphinx build for an unsaved file. So the build would be done on workspace content rather than from file itself
    """

    def trigger_sphinx_build_for_usaved_file(self, params: str):
        if not self.buildSphinxOnChange:
            return
        file_path = pathlib.Path(self.remove_prefix(params[0], "file://"))
        content = self.workspace.get_document("file://" + str(file_path)).source
        rel_path_without_extension = str(
            file_path.relative_to(self.sphinx_args["srcdir"]).with_suffix("")
        )
        self.build_preview_from_content(rel_path_without_extension, content)

    def build_preview_from_content(self, preview_docname: str, preview_content: str):
        """Handle build with preview from content."""
        self.is_preview = True
        self.preview_content = preview_content
        self.preview_docname = preview_docname
        self.build()

    def build(self):

        if not self.app:
            return

        self.logger.debug("Building...")
        self.send_notification("esbonio/buildStart", {})
        self.clear_diagnostics("sphinx-build")
        self.sync_diagnostics()

        # Reset the warnings counter
        self.app._warncount = 0
        error = False

        if self.sphinx_log is not None:
            self.sphinx_log.diagnostics = {}

        try:
            self.app.build()
        except Exception as exc:
            error = True
            self.logger.error(traceback.format_exc())
            uri, diagnostic = exception_to_diagnostic(exc)
            self.set_diagnostics("sphinx-build", uri, [diagnostic])

        if self.sphinx_log is not None:
            for doc, diagnostics in self.sphinx_log.diagnostics.items():
                self.logger.debug("Found %d problems for %s", len(diagnostics), doc)
                self.set_diagnostics("sphinx", doc, diagnostics)

        self.sync_diagnostics()
        self.send_notification(
            "esbonio/buildComplete",
            {
                "config": self.configuration,
                "error": error,
                "warnings": self.app._warncount,
            },
        )

    def cb_env_before_read_docs(self, app, env, docnames: List[str]):
        """Callback handling env-before-read-docs event."""
        app.builder.get_translator_class().docnames = docnames
        app.builder.get_translator_class().doccount = len(docnames)
        # comm.log(f"cb_env_before_read_docs {docnames}")
        # add our edited file to inject content in source-read, even if not physically changed
        if self.is_preview & (self.preview_docname not in docnames):
            # don't care about any other docs
            docnames.clear()
            docnames.append(self.preview_docname)

    def cb_source_read(self, app, docname, source):
        """Callback handling source_read event."""
        if self.is_preview & (docname == self.preview_docname):
            # log(f"Injecting live-preview content for {docname}")
            source[0] = self.preview_content

    def create_sphinx_app(self, options: InitializationOptions) -> Optional[Sphinx]:
        """Create a Sphinx application instance with the given config."""
        sphinx = options.sphinx
        server = options.server

        self.logger.debug("Workspace root '%s'", self.workspace.root_uri)
        self.logger.debug("User Config %s", json.dumps(sphinx.dict(), indent=2))

        sphinx_config = sphinx.resolve(self.workspace.root_uri)
        self.sphinx_args = sphinx_config.to_application_args()
        self.logger.debug("Sphinx Args %s", json.dumps(self.sphinx_args, indent=2))

        # Override Sphinx's logging setup with our own.
        sphinx_logging_module.setup = partial(self._logging_setup, server, sphinx)
        app = Sphinx(**self.sphinx_args)

        self._load_sphinx_extensions(app)
        self._load_sphinx_config(app)

        # TODO This creates a problem, if other extensions already use a custom builder
        # or translator, because either this or the other one is overwritten.
        if self.user_config.sphinx.enableLivePreview:
            # translator_class = CustomHTMLTranslatorWrapper(app)
            # app.set_translator("html", translator_class, True)

            app.add_transform(LineNumberTransform)

        if self.user_config.sphinx.buildSphinxOnChange:
            app.connect("env-before-read-docs", self.cb_env_before_read_docs)
            app.connect("source-read", self.cb_source_read)

        return app

    def _logging_setup(
        self,
        server: SphinxServerConfig,
        sphinx: SphinxConfig,
        app: Sphinx,
        status: IO,
        warning: IO,
    ):

        # Disable color escape codes in Sphinx's log messages
        console.nocolor()

        if not server.hide_sphinx_output and not sphinx.silent:
            sphinx_logger = logging.getLogger(SPHINX_LOG_NAMESPACE)
            self.sphinx_log = SphinxLogHandler(app, self)
            sphinx_logger.addHandler(self.sphinx_log)

            if sphinx.quiet:
                level = logging.WARNING
            else:
                level = VERBOSITY_MAP[app.verbosity]

            sphinx_logger.setLevel(level)
            self.sphinx_log.setLevel(level)

            formatter = logging.Formatter("%(message)s")
            self.sphinx_log.setFormatter(formatter)

    def _load_sphinx_extensions(self, app: Sphinx):
        """Loop through each of Sphinx's extensions and see if any contain server
        functionality.
        """

        for name, ext in app.extensions.items():
            mod = ext.module

            setup = getattr(mod, "esbonio_setup", None)
            if setup is None:
                self.logger.debug(
                    "Skipping extension '%s', missing 'esbonio_setup' fuction", name
                )
                continue

            self.load_extension(name, setup)

    def _load_sphinx_config(self, app: Sphinx):
        """Try and load the config as an server extension."""

        name = "<sphinx-config>"

        setup = app.config._raw_config.get("esbonio_setup", None)
        if not setup or not callable(setup):
            return

        self.load_extension(name, setup)

    def preview(self, options: Dict[str, Any]) -> Dict[str, Any]:

        if not self.app or not self.app.builder:
            return {}

        builder_name = self.app.builder.name
        if builder_name not in {"html"}:
            self.show_message(
                f"Previews are not currently supported for the '{builder_name}' builder."
            )

            return {}

        if not self.preview_process and IS_LINUX:
            self.logger.debug("Starting preview server.")
            server = make_preview_server(self.app.outdir)
            self.preview_port = server.server_port

            self.preview_process = Process(target=server.serve_forever, daemon=True)
            self.preview_process.start()

        if not self.preview_process and not IS_LINUX:
            self.logger.debug("Starting preview server")

            q: Queue = Queue()
            self.preview_process = Process(
                target=start_preview_server, args=(q, self.app.outdir), daemon=True
            )
            self.preview_process.start()
            self.preview_port = q.get()

        if options.get("show", True):
            self.show_document(
                ShowDocumentParams(
                    uri=f"http://localhost:{self.preview_port}", external=True
                )
            )

        return {"port": self.preview_port}

    def get_doctree(
        self, *, docname: Optional[str] = None, uri: Optional[str] = None
    ) -> Optional[Any]:
        """Return the initial doctree corresponding to the specified document.

        The ``docname`` of a document is its path relative to the project's ``srcdir``
        minus the extension e.g. the docname of the file ``docs/lsp/features.rst``
        would be ``lsp/features``.

        Parameters
        ----------
        docname:
           Returns the doctree that corresponds with the given docname
        uri:
           Returns the doctree that corresponds with the given uri.
        """

        if self.app is None or self.app.env is None or self.app.builder is None:
            return None

        if uri is not None:
            fspath = Uri.to_fs_path(uri)
            docname = self.app.env.path2doc(fspath)

        if docname is None:
            return None

        try:
            return self.app.env.get_and_resolve_doctree(docname, self.app.builder)
        except FileNotFoundError:
            self.logger.debug("Could not find doctree for '%s'", docname)
            # self.logger.debug(traceback.format_exc())
            return None

    def get_domain(self, name: str) -> Optional[Domain]:
        """Return the domain with the given name.

        If a domain with the given name cannot be found, this method will return None.

        Parameters
        ----------
        name:
           The name of the domain
        """

        if self.app is None or self.app.env is None:
            return None

        domains = self.app.env.domains
        return domains.get(name, None)

    def get_domains(self) -> Iterator[Tuple[str, Domain]]:
        """Get all the domains registered with an applications.

        Returns a generator that iterates through all of an application's domains,
        taking into account configuration variables such as ``primary_domain``.
        Yielded values will be a tuple of the form ``(prefix, domain)`` where

        - ``prefix`` is the namespace that should be used when referencing items
          in the domain
        - ``domain`` is the domain object itself.

        """

        if self.app is None or self.app.env is None:
            return []

        domains = self.app.env.domains
        primary_domain = self.app.config.primary_domain

        for name, domain in domains.items():
            prefix = name

            # Items from the standard and primary domains don't require the namespace prefix
            if name == "std" or name == primary_domain:
                prefix = ""

            yield prefix, domain

    def get_directives(self) -> Dict[str, Directive]:
        """Return a dictionary of the known directives"""

        if self._directives is not None:
            return self._directives

        self._directives = super().get_directives()

        for prefix, domain in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for name, directive in domain.directives.items():
                key = fmt.format(name=name, prefix=prefix)
                self._directives[key] = directive

        return self._directives

    def get_directive_options(self, name: str) -> Dict[str, Any]:
        """Return the options specification for the given directive."""

        directive = self.get_directives().get(name, None)
        if directive is None:
            return {}

        options = directive.option_spec

        if name.startswith("auto") and self.app:
            self.logger.debug("Processing options for '%s' directive", name)
            name = name.replace("auto", "")

            self.logger.debug("Documenter name is '%s'", name)
            documenter = self.app.registry.documenters.get(name, None)

            if documenter is not None:
                options = documenter.option_spec

        return options or {}

    def get_roles(self) -> Dict[str, Any]:
        """Return a dictionary of known roles."""

        if self._roles is not None:
            return self._roles

        self._roles = super().get_roles()

        for prefix, domain in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for name, role in domain.roles.items():
                key = fmt.format(name=name, prefix=prefix)
                self._roles[key] = role

        return self._roles

    def get_default_role(self) -> Tuple[Optional[str], Optional[str]]:
        """Return the project's default role"""

        if not self.app:
            return None, None

        role = self.app.config.default_role
        if not role:
            return None, None

        if ":" in role:
            domain, name = role.split(":")

            if domain == self.app.config.primary_domain:
                domain = ""

            return domain, name

        return None, role

    def get_role_target_types(self, name: str, domain_name: str = "") -> List[str]:
        """Return a map indicating which object types a role is capable of linking
        with.

        For example

        .. code-block:: python

           {
               "func": ["function"],
               "class": ["class", "exception"]
           }
        """

        key = f"{domain_name}:{name}" if domain_name else name

        if self._role_target_types is not None:
            return self._role_target_types.get(key, [])

        self._role_target_types = {}

        for prefix, domain in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for name, item_type in domain.object_types.items():
                for role in item_type.roles:
                    role_key = fmt.format(name=role, prefix=prefix)
                    target_types = self._role_target_types.get(role_key, list())
                    target_types.append(name)

                    self._role_target_types[role_key] = target_types

        types = self._role_target_types.get(key, [])
        self.logger.debug("Role '%s' targets object types '%s'", key, types)

        return types

    def get_role_targets(self, name: str, domain: str = "") -> List[tuple]:
        """Return a list of objects targeted by the given role.

        Parameters
        ----------
        name:
           The name of the role
        domain:
           The domain the role is a part of, if applicable.
        """

        targets: List[tuple] = []
        domain_obj: Optional[Domain] = None

        if domain:
            domain_obj = self.get_domain(domain)
        else:
            std = self.get_domain("std")
            if std and name in std.roles:
                domain_obj = std

            elif self.app and self.app.config.primary_domain:
                domain_obj = self.get_domain(self.app.config.primary_domain)

        target_types = set(self.get_role_target_types(name, domain))

        if not domain_obj:
            self.logger.debug("Unable to find domain for role '%s:%s'", domain, name)
            return []

        for obj in domain_obj.get_objects():
            if obj[2] in target_types:
                targets.append(obj)

        return targets

    def get_intersphinx_projects(self) -> List[str]:
        """Return the list of configured intersphinx project names."""

        if self.app is None:
            return []

        inv = getattr(self.app.env, "intersphinx_named_inventory", {})
        return list(inv.keys())

    def has_intersphinx_targets(
        self, project: str, name: str, domain: str = ""
    ) -> bool:
        """Return ``True`` if the given intersphinx project has targets targeted by the
        given role.

        Parameters
        ----------
        project:
           The project to check
        name:
           The name of the role
        domain:
           The domain the role is a part of, if applicable.
        """

        targets = self.get_intersphinx_targets(project, name, domain)

        if len(targets) == 0:
            return False

        return any([len(items) > 0 for items in targets.values()])

    def get_intersphinx_targets(
        self, project: str, name: str, domain: str = ""
    ) -> Dict[str, Dict[str, tuple]]:
        """Return the intersphinx objects targeted by the given role.

        Parameters
        ----------
        project:
           The project to return targets from
        name:
           The name of the role
        domain:
           The domain the role is a part of, if applicable.
        """

        if self.app is None:
            return {}

        inv = getattr(self.app.env, "intersphinx_named_inventory", {})
        if project not in inv:
            return {}

        targets = {}
        inv = inv[project]

        for target_type in self.get_role_target_types(name, domain):

            explicit_domain = f"{domain}:{target_type}"
            if explicit_domain in inv:
                targets[target_type] = inv[explicit_domain]
                continue

            primary_domain = f'{self.app.config.primary_domain or ""}:{target_type}'
            if primary_domain in inv:
                targets[target_type] = inv[primary_domain]
                continue

            std_domain = f"std:{target_type}"
            if std_domain in inv:
                targets[target_type] = inv[std_domain]

        return targets


def exception_to_diagnostic(exc: BaseException):
    """Convert an exception into a diagnostic we can send to the client."""

    # Config errors sometimes wrap the true cause of the problem
    if isinstance(exc, ConfigError) and exc.__cause__ is not None:
        exc = exc.__cause__

    if isinstance(exc, SyntaxError):
        path = pathlib.Path(exc.filename or "")
        line = (exc.lineno or 1) - 1
    else:
        tb = exc.__traceback__
        frame = traceback.extract_tb(tb)[-1]
        path = pathlib.Path(frame.filename)
        line = (frame.lineno or 1) - 1

    message = type(exc).__name__ if exc.args.count == 0 else exc.args[0]

    diagnostic = Diagnostic(
        range=Range(
            start=Position(line=line, character=0),
            end=Position(line=line + 1, character=0),
        ),
        message=message,
        severity=DiagnosticSeverity.Error,
    )

    return Uri.from_fs_path(str(path)), diagnostic


cli = setup_cli("esbonio.lsp.sphinx", "Esbonio's Sphinx language server.")
cli.set_defaults(modules=DEFAULT_MODULES)
cli.set_defaults(server_cls=SphinxLanguageServer)
