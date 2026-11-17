"""Make the repo importable as the plugin package exactly as the Hermes plugin
host loads directory plugins.

hermes_cli/plugin_host_child.py::_import_plugin imports the plugin directory
under the assigned module name (``portal``) via
``importlib.util.spec_from_file_location(module_name, <dir>/__init__.py,
submodule_search_locations=[<dir>])``. The checkout dir is named
``hermes-portal-plugin``, which plain path-based importing cannot alias to
``portal``, so register the same spec here before test collection.
"""

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

if "portal" not in sys.modules:
    _init = _REPO / "__init__.py"
    _spec = importlib.util.spec_from_file_location(
        "portal", _init, submodule_search_locations=[str(_REPO)]
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    _module.__package__ = "portal"
    _module.__path__ = [str(_REPO)]
    sys.modules["portal"] = _module
    _spec.loader.exec_module(_module)
