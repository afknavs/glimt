"""Simulated devices. Used by `glimt demo`, the test suite, and as the smallest
complete example of a bridge.

    [bridges.virtual]
    latency_ms = 0            # optional: pretend the hardware is slow

    [devices.lamp]
    bridge = "virtual"
    kind = "light"
    capabilities = ["power", "brightness", "color"]
    actions = ["party"]       # optional
"""

from __future__ import annotations

import asyncio

from ..model import Cap, Device
from .base import Bridge, BridgeError

SPECS = {
    "power": Cap.bool(),
    "brightness": Cap.percent(),
    "color": Cap.color(),
    "temperature": Cap.integer(16, 30, "°C"),
    "fan": Cap.enum(["auto", "low", "medium", "high"]),
    "effect": Cap.enum(["color-flow", "red-white"]),
    "speed": Cap.integer(1, 100),
    "eco": Cap.bool(),
    "sleep": Cap.bool(),
}
DEFAULTS = {"power": False, "brightness": 60, "color": "#ffb46b", "temperature": 24, "fan": "auto",
            "effect": None, "speed": 50, "eco": False, "sleep": False}


class VirtualBridge(Bridge):
    type = "virtual"

    def devices(self) -> list[Device]:
        made = []
        for device_id, cfg in self.device_configs.items():
            names = cfg.get("capabilities", ["power"])
            unknown = [name for name in names if name not in SPECS]
            if unknown:
                raise BridgeError(f"[devices.{device_id}] virtual capabilities are: {', '.join(SPECS)}")
            device = self.make_device(device_id, cfg, capabilities={name: SPECS[name] for name in names},
                                      actions=list(cfg.get("actions", [])))
            device.state = {name: DEFAULTS[name] for name in names}
            device.online, device.route = True, "virtual"
            made.append(device)
        return made

    async def apply(self, device: Device, changes: dict) -> None:
        await asyncio.sleep(self.config.get("latency_ms", 0) / 1000)
        if self.config.get("fail"):
            raise BridgeError("Simulated failure")
        self.report(device.id, changes)

    async def act(self, device: Device, action: str, args: dict):
        return {"ran": action, "args": args}
