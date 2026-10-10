"""Bridge registry.

Bridges are found in three places, later ones overriding earlier ones:

1. Built in:    glimt/bridges/<type>.py
2. Installed:   packages exposing the ``glimt.bridges`` entry point
3. Local:       ~/.glimt/bridges/<type>.py   (drop a file in, no packaging needed)
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from importlib.metadata import entry_points

from .. import paths
from .base import Bridge, BridgeError

BUILTIN = {
    "virtual": "glimt.bridges.virtual",
    "sound": "glimt.bridges.sound",
    "tuya": "glimt.bridges.tuya",
    "elk-ble": "glimt.bridges.elk_ble",
    "homeassistant": "glimt.bridges.homeassistant",
    "wled": "glimt.bridges.wled",
    "hue": "glimt.bridges.hue",
}

__all__ = ["Bridge", "BridgeError", "available", "load"]


def _from_module(module, bridge_type: str) -> type[Bridge]:
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Bridge) and value is not Bridge \
                and value.type == bridge_type:
            return value
    raise BridgeError(f"{module.__name__} defines no Bridge subclass with type = \"{bridge_type}\"")


def _local_file(bridge_type: str):
    path = paths.user_bridges_dir() / (bridge_type.replace("-", "_") + ".py")
    return path if path.exists() else None


def available() -> dict[str, str]:
    """type -> where it comes from."""
    found = {name: "built in" for name in BUILTIN}
    for entry in entry_points(group="glimt.bridges"):
        found[entry.name] = f"package {entry.value.split(':')[0].split('.')[0]}"
    local = paths.user_bridges_dir()
    if local.is_dir():
        for path in sorted(local.glob("*.py")):
            if not path.name.startswith("_"):
                found[path.stem.replace("_", "-")] = str(path)
    return found


def load(bridge_type: str) -> type[Bridge]:
    local = _local_file(bridge_type)
    if local:
        name = f"glimt_local_bridge_{local.stem}"
        spec = importlib.util.spec_from_file_location(name, local)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return _from_module(module, bridge_type)
    for entry in entry_points(group="glimt.bridges"):
        if entry.name == bridge_type:
            return entry.load()
    if bridge_type in BUILTIN:
        try:
            module = importlib.import_module(BUILTIN[bridge_type])
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("glimt"):
                raise BridgeError(f"The '{bridge_type}' bridge is not included in this build") from None
            raise BridgeError(
                f"The '{bridge_type}' bridge needs the '{exc.name}' package. "
                f"Install it with: pip install 'glimt[{_extra_for(bridge_type)}]'") from None
        return _from_module(module, bridge_type)
    known = ", ".join(sorted(available()))
    raise BridgeError(f"No bridge of type '{bridge_type}'. Available: {known}. "
                      "To add one, see docs/bridges.md or run: glimt bridge new " + bridge_type)


def _extra_for(bridge_type: str) -> str:
    return {"tuya": "tuya", "elk-ble": "ble", "homeassistant": "homeassistant"}.get(bridge_type, "all")
