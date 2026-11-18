"""Portal dashboard plugin — backend API routes mounted at /api/plugins/portal/.

Re-exports the plugin package's FastAPI ``router`` (phone-data sinks, Pulse
feed, cloud voice tokens). The dashboard ``api`` file is loaded standalone by
the plugin host (``op_asgi``), so relative imports do NOT resolve here by
default. We import the plugin package under Hermes' dotted-name convention
with synthetic parent packages — mirroring
``hermes_cli.plugin_host_child._import_plugin`` — so ``from .sibling import x``
inside the plugin resolves. If the host already loaded the package under this
name (e.g. for tool registration), we reuse that exact module so state is shared.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODULE = "hermes_plugins.portal"


def _load_plugin():
    if _MODULE in sys.modules:
        return sys.modules[_MODULE]
    # Synthetic parent packages (hermes_plugins) so relative imports resolve.
    parts = _MODULE.split(".")[:-1]
    for i in range(1, len(parts) + 1):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            ns_pkg = types.ModuleType(parent)
            ns_pkg.__path__ = []  # type: ignore[attr-defined]
            ns_pkg.__package__ = parent
            sys.modules[parent] = ns_pkg
    spec = importlib.util.spec_from_file_location(
        _MODULE,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load portal plugin from {_PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _MODULE
    module.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for name in [m for m in sys.modules if m == _MODULE or m.startswith(_MODULE + ".")]:
            sys.modules.pop(name, None)
        raise
    return module


router = _load_plugin().router
