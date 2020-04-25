import importlib
import inspect
import os
from pathlib import Path
import pkgutil
import sys
import warnings
from contextlib import contextmanager
from logging import getLogger
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from . import _tracing
from .callers import HookResult
from .exceptions import (
    PluginError,
    PluginImportError,
    PluginRegistrationError,
    PluginValidationError,
)
from .hooks import HookCaller, HookExecFunc
from .implementation import HookImpl
from .markers import HookimplMarker, HookspecMarker
from .plugin import Plugin, module_to_dist

if sys.version_info >= (3, 8):
    from importlib import metadata as importlib_metadata
else:
    import importlib_metadata


logger = getLogger(__name__)


def ensure_namespace(obj: Any, name: str = 'orphan') -> Type:
    """Convert a ``dict`` to an object that provides ``getattr``.

    Parameters
    ----------
    obj : Any
        An object, may be a ``dict``, or a regular namespace object.
    name : str, optional
        A name to use for the new namespace, if created.  by default 'orphan'

    Returns
    -------
    type
        A namespace object. If ``obj`` is a ``dict``, creates a new ``type``
        named ``name``, prepopulated with the key:value pairs from ``obj``.
        Otherwise, if ``obj`` is not a ``dict``, will return the original
        ``obj``.

    Raises
    ------
    ValueError
        If ``obj`` is a ``dict`` that contains keys that are not valid
        `identifiers
        <https://docs.python.org/3.3/reference/lexical_analysis.html#identifiers>`_.
    """
    if isinstance(obj, dict):
        bad_keys = [str(k) for k in obj.keys() if not str(k).isidentifier()]
        if bad_keys:
            raise ValueError(
                f"dict contained invalid identifiers: {', '.join(bad_keys)}"
            )
        return type(name, (), obj)
    return obj


@contextmanager
def temp_path_additions(path: Optional[Union[str, List[str]]]) -> Generator:
    """A context manager that temporarily adds ``path`` to sys.path.

    Parameters
    ----------
    path : str or list of str
        A path or list of paths to add to sys.path

    Yields
    -------
    sys_path : list of str
        The current sys.path for the context.
    """
    if isinstance(path, (str, Path)):
        path = [path]
    path = [os.fspath(p) for p in path] if path else []
    to_add = [p for p in path if p not in sys.path]
    for p in to_add:
        sys.path.insert(0, p)
    try:
        yield sys.path
    finally:
        for p in to_add:
            sys.path.remove(p)


class PluginManager:
    """ Core class which manages registration of plugin objects and hook calls.

    You can register new hooks by calling :meth:`add_hookspecs(namespace)
    <.PluginManager.add_hookspecs>`. You can register plugin objects (which
    contain hooks) by calling :meth:`register(namespace)
    <.PluginManager.register>`.  The ``PluginManager`` is initialized
    with a ``project_name`` that is used when discovering `hook specifications`
    and `hook implementations`.

    For debugging purposes you may call :meth:`.PluginManager.enable_tracing`
    which will subsequently send debug information to the trace helper.
    """

    def __init__(
        self,
        project_name: str,
        *,
        discover_entry_point: str = '',
        discover_prefix: str = '',
    ):
        self.project_name = project_name
        self.discover_entry_point = discover_entry_point
        self.discover_prefix = discover_prefix
        # mapping of name -> Plugin object
        self.plugins: Dict[str, Plugin] = {}
        self._blocked: Set[str] = set()

        self.trace = _tracing.TagTracer().get("pluginmanage")
        self.hook = _HookRelay(self)
        self._inner_hookexec: HookExecFunc = lambda c, m, k: c.multicall(
            m, k, firstresult=c.is_firstresult
        )

    @property
    def hooks(self) -> '_HookRelay':
        """An alias for PluginManager.hook"""
        return self.hook

    def _hookexec(
        self, caller: HookCaller, methods: List[HookImpl], kwargs: dict
    ) -> HookResult:
        """Returns a function that will call a set of hookipmls with a caller.

        This function will be passed to ``HookCaller`` instances that are
        created during hookspec and plugin registration.

        If :meth:`~.PluginManager.enable_tracing` is used, it will set it's own
        wrapper function at self._inner_hookexec to enable tracing of hook
        calls.

        Parameters
        ----------
        caller : HookCaller
            The HookCaller instance that will call the HookImpls.
        methods : List[HookImpl]
            A list of :class:`~naplugi.HookImpl` objects whos functions will
            be called during the hook call loop.
        kwargs : dict
            Keyword arguments to pass when calling the ``HookImpl``.

        Returns
        -------
        :class:`~naplugi.HookResult`
            The result object produced by the multicall loop.
        """
        return self._inner_hookexec(caller, methods, kwargs)

    def discover(
        self,
        path: Optional[str] = None,
        entry_point: str = None,
        prefix: str = None,
        ignore_errors: bool = True,
    ) -> Tuple[int, List[PluginError]]:
        """Discover modules by both naming convention and entry_points

        1) Using naming convention:
            plugins installed in the environment that follow a naming
            convention (e.g. "napari_plugin"), can be discovered using
            `pkgutil`. This also enables easy discovery on pypi

        2) Using package metadata:
            plugins that declare a special key (self.PLUGIN_ENTRYPOINT) in
            their setup.py `entry_points`.  discovered using `pkg_resources`.

        https://packaging.python.org/guides/creating-and-discovering-plugins/

        Parameters
        ----------
        path : str, optional
            If a string is provided, it is added to sys.path before importing,
            and removed at the end. by default True
        entry_point : str, optional
            An entry_point group to search for, by default None
        prefix : str, optional
            If ``provided``, modules in the environment starting with
            ``prefix`` will be imported and searched for hook implementations
            by default None.
        ignore_errors : bool, optional
            If ``True``, errors will be gathered and returned at the end.
            Otherwise, they will be raised immediately. by default True

        Returns
        -------
        (count, errs) : Tuple[int, List[PluginError]]
            The number of succefully loaded modules, and a list of errors that
            occurred (if ``ignore_errors`` was ``True``)
        """
        entry_point = entry_point or self.discover_entry_point
        prefix = prefix or self.discover_prefix

        self.hook._needs_discovery = False
        # allow debugging escape hatch
        if os.environ.get("NAPLUGI_DISABLE_PLUGINS"):
            warnings.warn(
                'Plugin discovery disabled due to '
                'environmental variable "NAPLUGI_DISABLE_PLUGINS"'
            )
            return 0, []

        errs: List[PluginError] = []
        with temp_path_additions(path):
            count = 0
            count, errs = self.load_entrypoints(entry_point, '', ignore_errors)
            n, err = self.load_modules_by_prefix(prefix, ignore_errors)
            count += n
            errs += err
            if count:
                msg = f'loaded {count} plugins:\n  '
                msg += "\n  ".join([str(p) for p in self.plugins.values()])
                logger.info(msg)

        return count, errs

    @contextmanager
    def discovery_blocked(self) -> Generator:
        """A context manager that temporarily blocks discovery of new plugins.
        """
        current = self.hook._needs_discovery
        self.hook._needs_discovery = False
        try:
            yield
        finally:
            self.hook._needs_discovery = current

    def load_entrypoints(
        self, group: str, name: str = '', ignore_errors=True
    ) -> Tuple[int, List[PluginError]]:
        """Load plugins from distributions with an entry point named ``group``.

        https://packaging.python.org/guides/creating-and-discovering-plugins/#using-package-metadata

        For background on entry points, see the Entry Point specification at
        https://packaging.python.org/specifications/entry-points/

        Parameters
        ----------
        group : str
            The entry_point group name to search for
        name : str, optional
            If provided, loads only plugins named ``name``, by default None.
        ignore_errors : bool, optional
            If ``False``, any errors raised during registration will be
            immediately raised, by default True

        Returns
        -------
        Tuple[int, List[PluginError]]
            A tuple of `(count, errors)` with the number of new modules
            registered and a list of any errors encountered (assuming
            ``ignore_errors`` was ``False``, otherwise they are raised.)

        Raises
        ------
        PluginError
            If ``ignore_errors`` is ``True`` and any errors are raised during
            registration.
        """
        if (not group) or os.environ.get("NAPLUGI_DISABLE_ENTRYPOINT_PLUGINS"):
            return 0, []
        count = 0
        errors: List[PluginError] = []
        for dist in importlib_metadata.distributions():
            for ep in dist.entry_points:
                if (
                    ep.group != group  # type: ignore
                    or (name and ep.name != name)
                    # already registered
                    or self.is_registered(ep.name)
                    or self.is_blocked(ep.name)
                ):
                    continue

                try:
                    if self._load_and_register(ep, ep.name):
                        count += 1
                except PluginError as e:
                    errors.append(e)
                    self.set_blocked(ep.name)
                    if ignore_errors:
                        continue
                    raise e

        return count, errors

    def load_modules_by_prefix(
        self, prefix: str, ignore_errors: bool = True
    ) -> Tuple[int, List[PluginError]]:
        """Load plugins by module naming convention.

        https://packaging.python.org/guides/creating-and-discovering-plugins/#using-naming-convention

        Parameters
        ----------
        prefix : str
            Any modules found in sys.path whose names begin with ``prefix``
            will be imported and searched for hook implementations.
        ignore_errors : bool, optional
            If ``False``, any errors raised during registration will be
            immediately raised, by default True

        Returns
        -------
        Tuple[int, List[PluginError]]
            A tuple of `(count, errors)` with the number of new modules
            registered and a list of any errors encountered (assuming
            ``ignore_errors`` was ``False``, otherwise they are raised.)

        Raises
        ------
        PluginError
            If ``ignore_errors`` is ``True`` and any errors are raised during
            registration.
        """
        if os.environ.get("NAPLUGI_DISABLE_PREFIX_PLUGINS") or not prefix:
            return 0, []
        count = 0
        errors: List[PluginError] = []
        for finder, mod_name, ispkg in pkgutil.iter_modules():
            if not mod_name.startswith(prefix):
                continue
            dist = module_to_dist().get(mod_name)
            name = dist.metadata.get("name") if dist else mod_name
            if self.is_registered(name) or self.is_blocked(name):
                continue

            try:
                if self._load_and_register(mod_name, name):
                    count += 1
            except PluginError as e:
                errors.append(e)
                self.set_blocked(name)
                if ignore_errors:
                    continue
                raise e

        return count, errors

    def _load_and_register(
        self,
        mod: Union[str, importlib_metadata.EntryPoint],
        plugin_name: Optional[str] = None,
    ) -> Optional[str]:
        """A helper function to register a module or EntryPoint under a name.

        Parameters
        ----------
        mod : str or importlib_metadata.EntryPoint
            The name of a module or an EntryPoint object instance to load.
        plugin_name : str, optional
            Optional name for plugin, by default ``get_canonical_name(plugin)``

        Returns
        -------
        str or None
            canonical plugin name, or ``None`` if the name is blocked from
            registering.

        Raises
        ------
        PluginImportError
            If an exception is raised when importing the module.
        PluginValidationError
            If an entry_point is declared that is neither a module nor a class.
        PluginRegistrationError
            If an exception is raised during plugin registration.
        """
        try:
            if isinstance(mod, importlib_metadata.EntryPoint):
                mod_name = mod.value
                module = mod.load()
            else:
                mod_name = mod
                module = importlib.import_module(mod)
            if self.is_registered(module):
                return None
        except Exception as exc:
            raise PluginImportError(
                f'Error while importing module {mod_name}',
                plugin_name=plugin_name,
                manager=self,
                cause=exc,
            )
        if not (inspect.isclass(module) or inspect.ismodule(module)):
            raise PluginValidationError(
                f'Plugin "{plugin_name}" declared entry_point "{mod_name}"'
                ' which is neither a module nor a class.',
                plugin_name=plugin_name,
                manager=self,
            )

        try:
            return self.register(module, plugin_name)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginRegistrationError(
                plugin_name=plugin_name, manager=self, cause=exc,
            )

    def _register_dict(
        self, dct: Dict[str, Callable], name: Optional[str] = None, **kwargs
    ) -> Optional[str]:
        mark = HookimplMarker(self.project_name)
        clean_dct = {
            key: mark(specname=key, **kwargs)(val)
            for key, val in dct.items()
            if inspect.isfunction(val)
        }
        namespace = ensure_namespace(clean_dct)
        return self.register(namespace, name)

    def register(
        self, namespace: Any, name: Optional[str] = None
    ) -> Optional[str]:
        """Register a plugin and return its canonical name or ``None``.

        Parameters
        ----------
        plugin : Any
            The namespace (class, module, dict, etc...) to register
        name : str, optional
            Optional name for plugin, by default ``get_canonical_name(plugin)``

        Returns
        -------
        str or None
            canonical plugin name, or ``None`` if the name is blocked from
            registering.

        Raises
        ------
        ValueError
            if the plugin is already registered.
        """
        if isinstance(namespace, dict):
            return self._register_dict(namespace, name)

        plugin_name = name or Plugin.get_canonical_name(namespace)

        if self.is_blocked(plugin_name):
            return None

        if self.is_registered(plugin_name):
            raise ValueError(f"Plugin name already registered: {plugin_name}")
        if self.is_registered(namespace):
            raise ValueError(f"Plugin module already registered: {namespace}")

        _plugin = Plugin(namespace, plugin_name)
        for hookimpl in _plugin.iter_implementations(self.project_name):
            hook_caller = getattr(self.hook, hookimpl.specname, None)
            # if we don't yet have a hookcaller by this name, create one.
            if hook_caller is None:
                hook_caller = HookCaller(hookimpl.specname, self._hookexec)
                setattr(self.hook, hookimpl.specname, hook_caller)
            # otherwise, if it has a specification, validate the new
            # hookimpl against the specification.
            elif hook_caller.has_spec():
                self._verify_hook(hook_caller, hookimpl)
                hook_caller._maybe_apply_history(hookimpl)
            # Finally, add the hookimpl to the hook_caller and the hook
            # caller to the list of callers for this plugin.
            hook_caller._add_hookimpl(hookimpl)
            _plugin._hookcallers.append(hook_caller)

        self.plugins[plugin_name] = _plugin
        return plugin_name

    def unregister(
        self, *, plugin_name: str = '', module: Any = None,
    ) -> Optional[Plugin]:
        """unregister a plugin object and all its contained hook implementations
        from internal data structures. """

        if module is not None:
            if plugin_name:
                warnings.warn(
                    'Both plugin_name and module provided '
                    'to unregister.  Will use module'
                )
            plugin = self.get_plugin_for_module(module)
            if not plugin:
                warnings.warn(f'No plugins registered for module {module}')
                return None
            plugin = self.plugins.pop(plugin.name)
        elif plugin_name:
            if plugin_name not in self.plugins:
                warnings.warn(
                    f'No plugins registered under the name {plugin_name}'
                )
                return None
            plugin = self.plugins.pop(plugin_name)
        else:
            raise ValueError("One of plugin_name or module must be provided")

        for hook_caller in plugin._hookcallers:
            hook_caller._remove_plugin(plugin.object)

        return plugin

    def _add_hookspec_dict(self, dct: Dict[str, Callable], **kwargs):
        mark = HookspecMarker(self.project_name)
        clean_dct = {
            key: mark(**kwargs)(val)
            for key, val in dct.items()
            if inspect.isfunction(val)
        }
        namespace = ensure_namespace(clean_dct)
        return self.add_hookspecs(namespace)

    def add_hookspecs(self, namespace: Any):
        """ add new hook specifications defined in the given ``namespace``.
        Functions are recognized if they have been decorated accordingly. """
        names = []
        for name in dir(namespace):
            method = getattr(namespace, name)
            if not inspect.isroutine(method):
                continue
            # TODO: make `_spec` a class attribute of HookSpec
            spec_opts = getattr(method, self.project_name + "_spec", None)
            if spec_opts is not None:
                hook_caller = getattr(self.hook, name, None,)
                if hook_caller is None:
                    hook_caller = HookCaller(
                        name, self._hookexec, namespace, spec_opts,
                    )
                    setattr(
                        self.hook, name, hook_caller,
                    )
                else:
                    # plugins registered this hook without knowing the spec
                    hook_caller.set_specification(
                        namespace, spec_opts,
                    )
                    for hookfunction in hook_caller.get_hookimpls():
                        self._verify_hook(
                            hook_caller, hookfunction,
                        )
                names.append(name)

        if not names:
            raise ValueError(
                f"did not find any {self.project_name!r} hooks in {namespace!r}"
            )

    def _object_is_registered(self, obj: Any) -> bool:
        return any(p.object == obj for p in self.plugins.values())

    def is_registered(self, obj: Any) -> bool:
        """ Return ``True`` if the plugin is already registered. """
        if isinstance(obj, str):
            return obj in self.plugins
        return self._object_is_registered(obj)

    def is_blocked(self, plugin_name: str) -> bool:
        """ return ``True`` if the given plugin name is blocked. """
        return plugin_name in self._blocked

    def set_blocked(self, plugin_name: str, blocked=True):
        """Block registrations of ``plugin_name``, unregister if registered.

        Parameters
        ----------
        plugin_name : str
            A plugin name to block.
        blocked : bool, optional
            Whether to block the plugin.  If ``False`` will "unblock"
            ``plugin_name``.  by default True
        """
        if blocked:
            self._blocked.add(plugin_name)
            if self.is_registered(plugin_name):
                self.unregister(plugin_name=plugin_name)
        else:
            if plugin_name in self._blocked:
                self._blocked.remove(plugin_name)

    def get_plugin_for_module(self, module: Any) -> Optional[Plugin]:
        try:
            return next(p for p in self.plugins.values() if p.object == module)
        except StopIteration:
            return None

    # TODO: fix sentinel
    def get_errors(
        self,
        plugin_name: Optional[str] = '_NULL',
        error_type: Union[Type[BaseException], str] = '_NULL',
    ) -> List[PluginError]:
        """Return a list of PluginErrors associated with this manager.

        Parameters
        ----------
        plugin_name : str
            If provided, will restrict errors to those that were raised by
            ``plugin_name``.
        error_type : Exception
            If provided, will restrict errors to instances of ``error_type``.
        """
        return PluginError.get(
            manager=self, plugin_name=plugin_name, error_type=error_type
        )

    def _verify_hook(self, hook_caller, hookimpl):
        if hook_caller.is_historic() and hookimpl.hookwrapper:
            raise PluginValidationError(
                f"Plugin {hookimpl.plugin_name!r}\nhook "
                f"{hook_caller.name!r}\nhistoric incompatible to hookwrapper",
                plugin_name=hookimpl.plugin_name,
                manager=self,
            )
        if hook_caller.spec.warn_on_impl:
            warnings.warn_explicit(
                hook_caller.spec.warn_on_impl,
                type(hook_caller.spec.warn_on_impl),
                lineno=hookimpl.function.__code__.co_firstlineno,
                filename=hookimpl.function.__code__.co_filename,
            )

        # positional arg checking
        notinspec = set(hookimpl.argnames) - set(hook_caller.spec.argnames)
        if notinspec:
            raise PluginValidationError(
                f"Plugin {hookimpl.plugin_name!r} for hook {hook_caller.name!r}"
                f"\nhookimpl definition: {_formatdef(hookimpl.function)}\n"
                f"Argument(s) {notinspec} are declared in the hookimpl but "
                "can not be found in the hookspec",
                plugin_name=hookimpl.plugin_name,
                manager=self,
            )

    def check_pending(self):
        """ Verify that all hooks which have not been verified against
        a hook specification are optional, otherwise raise
        :class:`.PluginValidationError`."""
        for name in self.hook.__dict__:
            if name[0] != "_":
                hook = getattr(self.hook, name)
                if not hook.has_spec():
                    for hookimpl in hook.get_hookimpls():
                        if not hookimpl.optionalhook:
                            raise PluginValidationError(
                                f"unknown hook {name!r} in "
                                f"plugin {hookimpl.plugin!r}",
                                plugin_name=hookimpl.plugin_name,
                                manager=self,
                            )

    def add_hookcall_monitoring(
        self,
        before: Callable[[str, List[HookImpl], dict], None],
        after: Callable[[HookResult, str, List[HookImpl], dict], None],
    ) -> Callable[[], None]:
        """ add before/after tracing functions for all hooks
        and return an undo function which, when called,
        will remove the added tracers.

        ``before(hook_name, hook_impls, kwargs)`` will be called ahead
        of all hook calls and receive a hookcaller instance, a list
        of HookImpl instances and the keyword arguments for the hook call.

        ``after(outcome, hook_name, hook_impls, kwargs)`` receives the
        same arguments as ``before`` but also a :py:class:`naplugi.callers._Result` object
        which represents the result of the overall hook call.
        """
        oldcall = self._inner_hookexec

        def traced_hookexec(
            caller: HookCaller, impls: List[HookImpl], kwargs: dict
        ):
            before(caller.name, impls, kwargs)
            outcome = HookResult.from_call(
                lambda: oldcall(caller, impls, kwargs)
            )
            after(outcome, caller.name, impls, kwargs)
            return outcome

        self._inner_hookexec = traced_hookexec

        def undo():
            self._inner_hookexec = oldcall

        return undo

    def enable_tracing(self):
        """ enable tracing of hook calls and return an undo function. """
        hooktrace = self.trace.root.get("hook")

        def before(hook_name, methods, kwargs):
            hooktrace.root.indent += 1
            hooktrace(hook_name, kwargs)

        def after(
            outcome, hook_name, methods, kwargs,
        ):
            if outcome.excinfo is None:
                hooktrace(
                    "finish", hook_name, "-->", outcome.result,
                )
            hooktrace.root.indent -= 1

        return self.add_hookcall_monitoring(before, after)


def _formatdef(func):
    return f"{func.__name__}{str(inspect.signature(func))}"


class _HookRelay:
    """Hook holder object for storing HookCaller instances.

    This object triggers (lazy) discovery of plugins as follows:  When a plugin
    hook is accessed (e.g. plugin_manager.hook.napari_get_reader), if
    ``self._needs_discovery`` is True, then it will trigger autodiscovery on
    the parent plugin_manager. Note that ``PluginManager.__init__`` sets
    ``self.hook._needs_discovery = True`` *after* hook_specifications and
    builtins have been discovered, but before external plugins are loaded.
    """

    def __init__(self, manager: PluginManager):
        self._manager = manager
        self._needs_discovery = True

    def __getattribute__(self, name) -> HookCaller:
        """Trigger manager plugin discovery when accessing hook first time."""
        if name not in ("_needs_discovery", "_manager",):
            if self._needs_discovery:
                self._manager.discover()
        return object.__getattribute__(self, name)

    def items(self) -> List[Tuple[str, HookCaller]]:
        """Iterate through hookcallers, removing private attributes."""
        return [
            (k, val) for k, val in vars(self).items() if not k.startswith("_")
        ]
