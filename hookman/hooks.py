import ctypes
import inspect
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from zipfile import ZipFile

from hookman import hookman_utils
from hookman.exceptions import (
    ConflictBetweenPluginsError, InvalidDestinationPathError, PluginAlreadyInstalledError)
from hookman.plugin_config import ConflictStatus, PluginInfo


class HookSpecs:
    """
    A class that holds the specification of the hooks, currently the following specification are available:
    - Project Name:
        This field will be used to identify the project and to name the hook functions
    - Version:
        The current version of the spec, when a new hook is created or modified this version should be incremented
    - Hooks:
        A list with the hooks available for the project, each hook is represented by a python function
    """

    def __init__(self, *,
            project_name: str,
            version: str,
            pyd_name: str,
            hooks: List[Callable]
        ) -> None:
        for hook in hooks:
            self._check_hook_arguments(hook)
        self.project_name = project_name
        self.version = version
        self.pyd_name = pyd_name
        self.hooks = hooks

    def _check_hook_arguments(self, hook: Callable):
        """
        Check if the arguments of the hooks are valid.
        If an error is found, a TypeError exception will be raised
        """
        hook_args = inspect.getfullargspec(hook)

        if not hook_args.args:
            raise TypeError("It's not possible to create a hook without argument")

        annotate_args = {
            arg: hook_args.annotations[arg]
            for arg in hook_args.annotations
            if arg != 'return'
        }

        if len(annotate_args) != len(hook_args.args):
            raise TypeError("All hooks arguments must have the type informed")

        if not inspect.getdoc(hook):
            raise TypeError("All hooks must have documentation")


class HookMan:
    """
    Main class of HookMan, this class holds all the information related to the plugins
    """

    def __init__(self, *, specs: HookSpecs, plugin_dirs: List[Path]):
        self.specs = specs
        self.plugins_dirs = plugin_dirs
        self.hooks_available = {
            f'{hook.__name__.lower()}': f'{specs.project_name.lower()}_v{specs.version}_{hook.__name__.lower()}'
            for hook in specs.hooks
        }

    def install_plugin(self, plugin_file_path: Path, dst_path: Path):
        """
        Extract the content of the zip file into dst_path.

        The following checks will be executed to validate the consistency of the inputs:

            1. The destination Path should be one of the paths informed during the initialization of HookMan (plugins_dirs field).

            2. The plugins_dirs cannot have two plugins with the same name.

        :plugin_file_path: The Path for the ``.hmplugin``
        :dst_path: The destination to where the plugin should be placed.
        """
        plugin_file_zip = ZipFile(plugin_file_path)
        PluginInfo.validate_plugin_file(plugin_file_zip=plugin_file_zip)

        if dst_path not in self.plugins_dirs:
            raise InvalidDestinationPathError(f"Invalid destination path, {dst_path} is not one of "
                                              f"the paths that were informed when the HookMan "
                                              f"object was initialized: {self.plugins_dirs}.")

        plugin_name = Path(plugin_file_zip.filename).stem.replace('-linux64', '').replace('-win64', '')

        plugins_dirs = [x for x in dst_path.iterdir() if x.is_dir()]

        if plugin_name in [x.name for x in plugins_dirs]:
            raise PluginAlreadyInstalledError("Plugin already installed")

        plugin_destination_folder = dst_path / plugin_name
        plugin_destination_folder.mkdir(parents=True)
        plugin_file_zip.extractall(plugin_destination_folder)

    def remove_plugin(self, plugin_name: str):
        """
        This method receives the name of the plugin as input, and will remove completely the plugin from ``plugin_dirs``.

        :plugin_name: Name of the plugin to be removed
        """
        for plugin in self.get_plugins_available():
            if plugin.name == plugin_name:
                shutil.rmtree(plugin.yaml_location.parents[1])
                break

    def get_plugins_available(self, ignored_plugins: Sequence[str]=()) -> Optional[List[PluginInfo]]:
        """
        Return a list of :ref:`plugin-info-api-section` that are available on ``plugins_dirs``

        Optionally you can pass a list of plugins that should be ignored.

        The :ref:`plugin-info-api-section` is a object that holds all information related to the plugin.
        """
        plugin_config_files = hookman_utils.find_config_files(self.plugins_dirs)

        plugins_available = [PluginInfo(plugin_file, self.hooks_available) for plugin_file in plugin_config_files]
        return [plugin_info for plugin_info in plugins_available if plugin_info.name not in ignored_plugins]

    def get_hook_caller(self, ignored_plugins: Sequence[str]=()):
        """
        Return a HookCaller class that holds all references for the functions implemented
        on the plugins.
        """
        self.ensure_is_valid(ignored_plugins)

        _hookman = __import__(self.specs.pyd_name)
        hook_caller = _hookman.HookCaller()
        for plugin in self.get_plugins_available(ignored_plugins):
            self._bind_libs_functions_on_hook_caller(plugin.shared_lib_path, hook_caller)
        return hook_caller

    def _bind_libs_functions_on_hook_caller(self, shared_lib_path: Path, hook_caller):
        """
        Load the shared_lib_path from the plugin and bind methods that are implemented on the
        hook_caller.
        """
        plugin_dll = ctypes.cdll.LoadLibrary(str(shared_lib_path))

        hooks_to_bind = {}
        for hook_name, full_hook_name in self.hooks_available.items():
            if PluginInfo.is_implemented_on_plugin(plugin_dll, full_hook_name):
                func_address = PluginInfo.get_function_address(plugin_dll, full_hook_name)
                hooks_to_bind[f'set_{hook_name}_function'] = func_address

        for hook in hooks_to_bind:
            cpp_func = getattr(hook_caller, hook)
            cpp_func(hooks_to_bind[hook])

    def ensure_is_valid(self, ignored_plugins: Sequence[str]=()):
        """
        Auxiliary methods that checks if the get_status has any conflict
        """
        if self.get_status(ignored_plugins):
            raise ConflictBetweenPluginsError(
                f"Could not get a Hook Caller due to existing conflict between installed plugins")

    def get_status(self, ignored_plugins: Sequence[str]=()) -> List[Optional[ConflictStatus]]:
        """
        Check if the plugins has conflicts between then.
        If a conflict is found a list of ConflictStatus object will be returned.
        Otherwise a empty list is returned.

        Optionally you can pass a list of plugins that should be ignored.

        .. :
            The ``get_status`` method currently just checks if more than on plugin implements the same hook.
        """
        list_of_conflicts = []
        plugins_available = self.get_plugins_available(ignored_plugins)
        if not plugins_available:
            return list_of_conflicts

        hooks_status = defaultdict(list)
        for plugin in plugins_available:
            for hook in plugin.hooks_implemented:
                hooks_status[hook].append(plugin.name)

        for hook_name, plugins in hooks_status.items():
            if len(plugins) > 1:
                list_of_conflicts.append(ConflictStatus(plugins=plugins, hook=hook_name))

        return list_of_conflicts
