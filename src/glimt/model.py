"""The one device model every bridge speaks.

A device has *capabilities* (settable values), *actions* (momentary commands with no
state) and a *state* dict. Bridges translate this vocabulary to their own protocol;
everything above the bridge (API, MCP tools, signals, HomeKit, the apps) only ever
sees this model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

KINDS = ("light", "outlet", "switch", "climate", "fan", "speaker", "other")

NAMED_COLORS = {
    "red": "#ff453a", "orange": "#ff9f0a", "amber": "#ff9f0a", "yellow": "#ffd60a",
    "green": "#30d158", "mint": "#63e6e2", "teal": "#40c8e0", "cyan": "#64d2ff",
    "blue": "#0a84ff", "indigo": "#5e5ce6", "purple": "#bf5af2", "violet": "#8c00ff",
    "pink": "#ff375f", "white": "#ffffff", "warm": "#ff9a3c",
}


class Cap:
    """Capability specs. A capability is settable and appears in state under its name."""

    @staticmethod
    def bool() -> dict:
        return {"type": "bool"}

    @staticmethod
    def percent() -> dict:
        return {"type": "int", "min": 0, "max": 100}

    @staticmethod
    def integer(minimum: int, maximum: int, unit: str | None = None) -> dict:
        spec = {"type": "int", "min": minimum, "max": maximum}
        if unit:
            spec["unit"] = unit
        return spec

    @staticmethod
    def enum(options: list[str]) -> dict:
        return {"type": "enum", "options": list(options)}

    @staticmethod
    def color() -> dict:
        return {"type": "color"}


def parse_color(value: Any) -> tuple[int, int, int]:
    """Accept '#rrggbb', 'rrggbb', a colour name, or [r, g, b]."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        rgb = tuple(int(channel) for channel in value)
        if any(channel < 0 or channel > 255 for channel in rgb):
            raise ValueError("RGB channels must be 0 to 255")
        return rgb
    if isinstance(value, str):
        text = NAMED_COLORS.get(value.strip().lower(), value.strip()).lstrip("#")
        if len(text) == 6:
            try:
                return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
    raise ValueError(f"Unrecognised colour {value!r}; use #rrggbb or one of: {', '.join(NAMED_COLORS)}")


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


@dataclass
class Device:
    id: str
    name: str
    kind: str
    bridge: str
    capabilities: dict[str, dict] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    # True when the device cannot report back (infrared, cheap BLE): state is "last sent".
    assumed: bool = False
    # May signals use this device? Defaults to colour-capable lights; set per device in config.
    signal: bool | None = None
    state: dict[str, Any] = field(default_factory=dict)
    online: bool | None = None
    route: str | None = None
    error: str | None = None
    updated: float = 0.0
    # Optional SF Symbol name for the apps, e.g. icon = "lamp.desk.fill" in config.
    icon: str | None = None
    config: dict = field(default_factory=dict, repr=False)

    def public(self) -> dict:
        out = {
            "id": self.id, "name": self.name, "kind": self.kind, "bridge": self.bridge,
            "capabilities": self.capabilities, "actions": self.actions,
            "state": self.state, "assumed": self.assumed, "online": self.online,
            "route": self.route, "updated": self.updated,
        }
        if self.error:
            out["error"] = self.error
        if self.icon:
            out["icon"] = self.icon
        return out

    def summary(self) -> str:
        """One compact line for agents: 'desk (light): power=on · sets power'."""
        caps = []
        for name, spec in self.capabilities.items():
            if spec["type"] == "enum":
                caps.append(f"{name}[{'|'.join(spec['options'])}]")
            elif spec["type"] == "int":
                caps.append(f"{name} {spec['min']}-{spec['max']}")
            else:
                caps.append(name)
        head = f'{self.id} ({self.kind}, "{self.name}")'
        tail = []
        if caps:
            tail.append("sets " + ", ".join(caps))
        if self.actions:
            tail.append("actions " + ", ".join(self.actions))
        return head + (": " + "; ".join(tail) if tail else "")

    def touch(self) -> None:
        self.updated = time.time()


def validate(device: Device, changes: dict) -> dict:
    """Check and normalise a change request against the device's capabilities."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Give at least one setting to change")
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        spec = device.capabilities.get(key)
        if spec is None:
            available = ", ".join(device.capabilities) or "nothing (it only has actions)"
            raise ValueError(f"{device.id} has no '{key}' setting. It can set: {available}")
        kind = spec["type"]
        if kind == "bool":
            if isinstance(value, str) and value.lower() in ("on", "true", "off", "false"):
                value = value.lower() in ("on", "true")
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{key} must be a number")
            try:
                value = int(round(float(value)))
            except ValueError:
                raise ValueError(f"{key} must be a number") from None
            if not spec["min"] <= value <= spec["max"]:
                raise ValueError(f"{key} must be between {spec['min']} and {spec['max']}")
        elif kind == "enum":
            if value not in spec["options"]:
                raise ValueError(f"{key} must be one of: {', '.join(spec['options'])}")
        elif kind == "color":
            value = to_hex(parse_color(value))
        clean[key] = value
    return clean
