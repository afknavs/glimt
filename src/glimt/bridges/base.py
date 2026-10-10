"""The bridge contract. Read docs/bridges.md before writing a new one.

A bridge connects one family of hardware (or one hub, or one cloud) to Glimt.
It does three things:

1. ``devices()``  - declare the devices it owns, in the common model.
2. ``apply()``    - push a validated change to one device, fast.
3. ``report()``   - tell the hub whenever a device's real state is known.

Everything else (validation, optimistic state, signals, the HTTP API, MCP tools,
HomeKit, the apps) is done for you by the hub.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from ..model import Device

if TYPE_CHECKING:
    from ..hub import Hub


class BridgeError(Exception):
    """Raise with a short, human-readable reason. It is shown to the user and to agents."""


class Bridge:
    #: The value people write as ``type = "..."`` in config.toml.
    type: ClassVar[str] = ""
    #: pip extra that provides this bridge's dependencies, e.g. "tuya". Shown when imports fail.
    extra: ClassVar[str | None] = None

    def __init__(self, name: str, config: dict, hub: "Hub") -> None:
        self.name = name
        self.config = config
        self.hub = hub
        self.log = logging.getLogger(f"glimt.bridge.{name}")
        self.error: str | None = None

    # ---- required -----------------------------------------------------------------

    def devices(self) -> list[Device]:
        """Return this bridge's devices. Called once, before start(); must not do I/O.

        Build them from ``self.device_configs`` (the [devices.*] tables that name this
        bridge) using ``self.make_device(...)``.
        """
        raise NotImplementedError

    async def apply(self, device: Device, changes: dict) -> None:
        """Send already-validated ``changes`` to the device and return quickly.

        Return once the command is on its way (acknowledged by the transport if that is
        cheap). Do not poll for confirmation here: call ``self.report()`` when the device
        confirms. Never block the event loop: wrap sync libraries in ``asyncio.to_thread``.
        Raise BridgeError if the command could not be sent.
        """
        raise NotImplementedError

    # ---- optional -----------------------------------------------------------------

    async def start(self) -> None:
        """Open connections and start background listeners. Runs concurrently with other
        bridges; an exception here marks only this bridge as failed."""

    async def stop(self) -> None:
        """Close connections and cancel tasks."""

    async def act(self, device: Device, action: str, args: dict) -> Any:
        """Run a momentary action (one of ``device.actions``)."""
        raise BridgeError(f"{device.id} has no action '{action}'")

    async def refresh(self, device: Device) -> None:
        """Force a fresh read of one device and report() it. Optional."""

    @classmethod
    async def discover(cls, config: dict) -> list[dict]:
        """Find devices for `glimt setup`. Return [devices.*]-shaped dicts, each with a
        suggested ``id``. Optional."""
        return []

    # ---- helpers ------------------------------------------------------------------

    @property
    def device_configs(self) -> dict[str, dict]:
        return {device_id: cfg for device_id, cfg in self.hub.config["devices"].items()
                if cfg["bridge"] == self.name}

    def make_device(self, device_id: str, cfg: dict, *, capabilities: dict[str, dict] | None = None,
                    actions: list[str] | None = None, assumed: bool = False) -> Device:
        return Device(id=device_id, name=cfg["name"], kind=cfg["kind"], bridge=self.name,
                      capabilities=capabilities or {}, actions=actions or [], assumed=assumed,
                      signal=cfg.get("signal"), icon=cfg.get("icon"), config=cfg)

    def report(self, device_id: str, state: dict | None = None, *, online: bool | None = None,
               route: str | None = None, error: str | None = None) -> None:
        """Tell the hub what a device's real state is. Safe to call from any thread."""
        self.hub.report(device_id, state, online=online, route=route, error=error)
