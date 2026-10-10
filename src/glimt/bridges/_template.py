"""TODO: one line saying what hardware this bridge talks to.

Config this bridge reads:

    [bridges.template]
    host = "192.168.1.50"          # TODO: whatever your hardware needs

    [devices.my-light]
    bridge = "template"
    kind = "light"
    # TODO: per-device keys, e.g. the id the hardware uses for this device
    remote_id = "1"

The whole contract is in docs/bridges.md. The short version:
  devices()  declare what exists           (no I/O)
  start()    connect, report first state   (may be slow; runs in the background)
  apply()    send a change and return fast (do not wait for confirmation)
  report()   call whenever real state is known
"""

from __future__ import annotations

from glimt.bridges.base import Bridge, BridgeError
from glimt.model import Cap, Device


class TemplateBridge(Bridge):
    type = "template"

    def devices(self) -> list[Device]:
        made = []
        for device_id, cfg in self.device_configs.items():
            # TODO: declare only what this hardware can really do. Available specs:
            #   Cap.bool()  Cap.percent()  Cap.color()  Cap.integer(min, max, unit)  Cap.enum([...])
            # Conventional names: power, brightness, color, temperature, fan, mode, effect.
            made.append(self.make_device(
                device_id, cfg,
                capabilities={"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()},
                actions=[],          # momentary commands with no state, e.g. ["faster", "slower"]
                assumed=False,       # True if the hardware can never tell you its state
            ))
        return made

    async def start(self) -> None:
        # TODO: open your connection / session here and keep it open: a connection that
        # is already up is what makes commands feel instant.
        # Then report every device's current state. Use `await asyncio.to_thread(fn)` for
        # blocking libraries; never block the event loop.
        for device_id in self.device_configs:
            self.report(device_id, {"power": False}, online=True, route="lan")

    async def stop(self) -> None:
        # TODO: close connections, cancel background tasks.
        pass

    async def apply(self, device: Device, changes: dict) -> None:
        # `changes` is already validated against the capabilities above and uses the common
        # vocabulary: power is a bool, brightness 0-100, color "#rrggbb".
        # TODO: translate and send. Return as soon as the command is sent/acknowledged.
        # Raise BridgeError("short human reason") if it could not be sent.
        remote_id = device.config.get("remote_id")
        if remote_id is None:
            raise BridgeError(f"[devices.{device.id}] needs remote_id")
        # Example: await self.http.put(f"/lights/{remote_id}", json=changes)
        # If the hardware answers with its new state, pass it on:
        self.report(device.id, changes)

    # Optional: momentary actions
    # async def act(self, device, action, args): ...

    # Optional: let `glimt setup` find devices for the user
    # @classmethod
    # async def discover(cls, config): return [{"id": "my-light", "name": "My light", "kind": "light", "remote_id": "1"}]
