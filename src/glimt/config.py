"""Load and write ~/.glimt/config.toml.

Config holds names, mappings and preferences only. Credentials live in
~/.glimt/secrets/ so a config file is always safe to paste into an issue.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from . import paths
from .model import KINDS

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

DEMO = """\
# Glimt demo: simulated devices, no hardware needed.
[bridges.virtual]

[bridges.sound]

[devices.lamp]
name = "Desk lamp"
bridge = "virtual"
kind = "light"
capabilities = ["power", "brightness", "color"]

[devices.strip]
name = "LED strip"
bridge = "virtual"
kind = "light"
capabilities = ["power", "brightness", "color"]

[devices.fan]
name = "Fan"
bridge = "virtual"
kind = "fan"
capabilities = ["power"]

[devices.speaker]
name = "This computer"
bridge = "sound"
kind = "speaker"
"""


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> dict:
    path = path or paths.config_file()
    if not path.exists():
        return {"daemon": {}, "bridges": {}, "devices": {}, "signals": {}}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return check(data, str(path))


def check(data: dict, origin: str = "config") -> dict:
    data.setdefault("daemon", {})
    data.setdefault("bridges", {})
    data.setdefault("devices", {})
    data.setdefault("signals", {})
    for name, bridge in data["bridges"].items():
        if not isinstance(bridge, dict):
            raise ConfigError(f"{origin}: [bridges.{name}] must be a table")
        bridge.setdefault("type", name)
    for device_id, device in data["devices"].items():
        where = f"{origin}: [devices.{device_id}]"
        if not ID_PATTERN.match(device_id):
            raise ConfigError(f"{where}: ids are lowercase letters, digits, '-' or '_'")
        if "bridge" not in device:
            raise ConfigError(f"{where}: missing bridge = \"...\"")
        if device["bridge"] not in data["bridges"]:
            known = ", ".join(data["bridges"]) or "none defined"
            raise ConfigError(f"{where}: unknown bridge '{device['bridge']}' (defined: {known})")
        device.setdefault("name", device_id.replace("-", " ").replace("_", " ").capitalize())
        device.setdefault("kind", "other")
        if device["kind"] not in KINDS:
            raise ConfigError(f"{where}: kind must be one of {', '.join(KINDS)}")
    return data


def write_table(name: str, values: dict | None, path: Path | None = None) -> Path:
    """Replace one top-level table (or remove it, with None) and leave every other line of
    the file, comments included, exactly as its owner wrote it."""
    import tomli_w

    path = path or paths.config_file()
    lines = path.read_text().splitlines() if path.exists() else []
    first = next((i for i, line in enumerate(lines) if line.strip() == f"[{name}]"), None)
    if first is not None:
        last = next((i for i in range(first + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        while last > first + 1 and (not lines[last - 1].strip() or lines[last - 1].startswith("#")):
            last -= 1  # a comment sitting above the next table belongs to that table
        del lines[first:last]
        if 0 < first < len(lines) and not lines[first - 1].strip() and not lines[first].strip():
            del lines[first]
    text = "\n".join(lines).strip("\n")
    if values is not None:
        text += ("\n\n" if text else "") + f"[{name}]\n" + tomli_w.dumps(values).rstrip("\n")
    text += "\n"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: could not update [{name}] ({exc}); edit the file by hand") from None
    path.write_text(text)
    return path


def save(data: dict, path: Path | None = None) -> Path:
    import tomli_w

    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))
    return path
