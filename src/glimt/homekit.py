"""Optional: show every Glimt device in Apple Home (and so to Siri and Shortcuts).

    [homekit]
    enabled = true
    name = "Glimt"          # bridge name in the Home app
    # port = 51826

    [homekit.devices]          # optional per-device settings
    speaker = false            # hide a device
    ac = { require = { mode = "cool" } }   # refuse power-on unless this state holds
    desk = { aid = 4 }         # pin the accessory id (keeps an existing pairing's rooms/names)

    [[homekit.switches]]       # expose device actions as switches
    name = "Fan faster"
    device = "fan"
    on = "faster"              # add off = "..." for a two-way switch; otherwise it is momentary

It runs inside the daemon, so Home shows state changes the moment the hub learns of them
instead of on a polling timer. Pair with `glimt homekit`.
"""

from __future__ import annotations

import asyncio
import colorsys
import json
import logging
import re

from . import __version__, paths
from .model import Device, parse_color, to_hex

log = logging.getLogger("glimt.homekit")
DEBOUNCE = 0.06
# Apple devices re-read a blank firmware version on every connection and then hang up,
# so every accessory states one, in the x.y.z form Home expects.
FIRMWARE = ".".join((re.findall(r"\d+", __version__) + ["0", "0"])[:3])


def state_file():
    return paths.secrets_dir() / "homekit.state"


def code_file():
    return paths.home() / "homekit-pairing-code"


class HomeKitExport:
    def __init__(self, hub, config: dict) -> None:
        self.hub, self.config = hub, config
        self.driver = None
        self.by_device: dict[str, list] = {}
        self._listener: asyncio.Task | None = None
        self.error: str | None = None

    def status(self) -> dict:
        paired = bool(self.driver and self.driver.state.paired)
        # Controllers only hold a live subscription while someone has Home open (or a home
        # hub is watching), so "0 listening" is normal and not a fault.
        connected = len(getattr(getattr(self.driver, "http_server", None), "connections", {}) or {})
        listening = {client for clients in getattr(self.driver, "topics", {}).values() for client in clients}
        return {"paired": paired, "accessories": sum(len(items) for items in self.by_device.values()),
                "connected": connected, "listening": len(listening), "error": self.error}

    # ---- accessory ids: stable forever, so Home never shuffles your rooms ---------------

    def _aids(self) -> dict[str, int]:
        path = paths.home() / "homekit-aids.json"
        known: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
        overrides = self.config.get("devices", {})
        wanted = [device_id for device_id in self.hub.devices if overrides.get(device_id) is not False]
        wanted += ["switch:" + entry["name"] for entry in self.config.get("switches", [])]
        pinned = {device_id: entry["aid"] for device_id, entry in overrides.items()
                  if isinstance(entry, dict) and "aid" in entry}
        pinned.update({"switch:" + entry["name"]: entry["aid"] for entry in self.config.get("switches", [])
                       if "aid" in entry})
        known.update(pinned)
        used = set(known.values())
        for key in wanted:
            if key not in known:
                known[key] = next(aid for aid in range(2, 10_000) if aid not in used)
                used.add(known[key])
        path.write_text(json.dumps(known, indent=2))
        return known

    # ---- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        try:
            from pyhap.accessory import Bridge
            from pyhap.accessory_driver import AccessoryDriver
        except ImportError:
            self.error = "HomeKit needs an extra package: pip install 'glimt[homekit]'"
            log.warning(self.error)
            return
        try:
            export = self

            class GlimtBridge(Bridge):
                def setup_message(self):  # keep the pairing code out of logs
                    paths.write_private(code_file(), self.driver.state.pincode.decode())
                    log.info("HomeKit is ready to pair. Run: glimt homekit")

            self.driver = AccessoryDriver(port=int(self.config.get("port", 51826)),
                                          persist_file=str(state_file()), loop=asyncio.get_running_loop())
            bridge = GlimtBridge(self.driver, self.config.get("name", "Glimt"))
            bridge.set_info_service(firmware_revision=FIRMWARE, manufacturer="Glimt", model="Bridge",
                                    serial_number="glimt-bridge")
            aids = self._aids()
            overrides = self.config.get("devices", {})
            for device in self.hub.devices.values():
                options = overrides.get(device.id)
                if options is False:
                    continue
                accessory = self._accessory(device, aids[device.id], options if isinstance(options, dict) else {})
                if accessory is not None:
                    bridge.add_accessory(accessory)
                    self.by_device.setdefault(device.id, []).append(accessory)
            for entry in self.config.get("switches", []):
                accessory = ActionSwitch(self, entry, aids["switch:" + entry["name"]])
                bridge.add_accessory(accessory)
                self.by_device.setdefault(entry["device"], []).append(accessory)
            self.driver.add_accessory(bridge)
            await self.driver.async_start()
            self._listener = asyncio.create_task(self._listen())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("HomeKit export failed to start")

    async def stop(self) -> None:
        if self._listener:
            self._listener.cancel()
        if self.driver:
            try:
                await asyncio.wait_for(self.driver.async_stop(), 5)
            except Exception:
                pass

    def _accessory(self, device: Device, aid: int, options: dict):
        caps = device.capabilities
        if device.kind == "climate" and "power" in caps and "temperature" in caps:
            return Thermostat(self, device, aid, options)
        if "power" in caps:
            return PowerAccessory(self, device, aid, options)
        return None  # action-only devices appear through [[homekit.switches]]

    async def _listen(self) -> None:
        while not self.hub.closing:
            queue = self.hub.subscribe()
            try:
                for device in self.hub.devices.values():  # (re)start from the whole picture
                    self._push(device)
                while (event := await queue.get()) is not None:
                    if event.get("type") == "device":
                        device = self.hub.devices.get(event["device"]["id"])
                        if device:
                            self._push(device)
            finally:
                self.hub.unsubscribe(queue)

    def _push(self, device: Device) -> None:
        for accessory in self.by_device.get(device.id, []):
            try:
                accessory.sync(device)
            except Exception:
                log.debug("could not sync %s to HomeKit", device.id, exc_info=True)

    def spawn(self, coroutine) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, self.driver.loop)

    def on_loop(self, callback, *args) -> None:
        """Run a callback on the event loop: at once if we are on it, else handed over safely."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.driver.loop:
            callback(*args)
        else:
            self.driver.loop.call_soon_threadsafe(callback, *args)


def _make_classes():
    from pyhap.accessory import Accessory
    from pyhap.const import CATEGORY_FAN, CATEGORY_LIGHTBULB, CATEGORY_OUTLET, CATEGORY_SWITCH, CATEGORY_THERMOSTAT

    class DeviceAccessory(Accessory):
        def __init__(self, export: HomeKitExport, device: Device, aid: int, options: dict, name: str | None = None):
            super().__init__(export.driver, name or device.name, aid=aid)
            self.export, self.device_id, self.options = export, device.id, options
            self.pending: dict = {}
            self.busy = 0  # commands from Home still on their way to the device
            self.timer = None
            self.set_info_service(firmware_revision=FIRMWARE, manufacturer="Glimt", model=device.kind,
                                  serial_number=f"glimt-{aid}")

        def queue(self, changes: dict) -> None:
            """Home often sends power, brightness and colour as separate writes a few
            milliseconds apart; merge them into one command."""
            # HAP may call this from another thread; `pending` is only ever touched on the loop.
            self.export.on_loop(self._enqueue, dict(changes))

        def _enqueue(self, changes: dict) -> None:
            self.pending.update(changes)
            if self.timer:
                self.timer.cancel()
            self.timer = self.export.driver.loop.call_later(DEBOUNCE, self._flush)

        def _flush(self) -> None:
            changes, self.pending = self.pending, {}
            if changes.get("power") is False:
                changes = {"power": False}
            self.busy += 1
            asyncio.ensure_future(self._apply(changes))

        @property
        def settling(self) -> bool:
            """True while Home's own command is in flight: pushing the old state back to
            Home in that window would make the tile bounce."""
            return bool(self.pending or self.busy)

        async def _apply(self, changes: dict) -> None:
            device = self.export.hub.devices[self.device_id]
            try:
                if changes.get("power") is True:
                    for key, needed in self.options.get("require", {}).items():
                        if device.state.get(key) != needed:
                            raise ValueError(f"{key} must be {needed}")
                await self.export.hub.apply(self.device_id, changes, source="homekit")
                failed = False
            except Exception as exc:
                log.info("HomeKit command for %s failed: %s", self.device_id, exc)
                failed = True
            self.busy -= 1
            self.sync(device)  # settle the tile on the truth, whatever happened
            if failed:
                self.fault.set_value(1)

    class PowerAccessory(DeviceAccessory):
        def __init__(self, export, device, aid, options):
            super().__init__(export, device, aid, options)
            caps = device.capabilities
            is_light = device.kind == "light" or "color" in caps or "brightness" in caps
            if is_light:
                self.category, service = CATEGORY_LIGHTBULB, "Lightbulb"
            elif device.kind == "outlet":
                self.category, service = CATEGORY_OUTLET, "Outlet"
            elif device.kind == "fan":
                self.category, service = CATEGORY_FAN, "Fan"
            else:
                self.category, service = CATEGORY_SWITCH, "Switch"
            extra = (["Brightness"] if "brightness" in caps and is_light else []) + \
                    (["Hue", "Saturation"] if "color" in caps and is_light else [])
            self.service = self.add_preload_service(service, chars=extra + ["StatusFault"])
            self.on = self.service.configure_char("On", setter_callback=lambda v: self.queue({"power": bool(v)}))
            self.fault = self.service.get_characteristic("StatusFault")
            if service == "Outlet":
                self.service.configure_char("OutletInUse", value=True)
            self.brightness = self.hue = self.saturation = None
            if "Brightness" in extra:
                self.brightness = self.service.configure_char(
                    "Brightness", value=50, setter_callback=lambda v: self.queue({"brightness": max(1, int(v))}))
            if "Hue" in extra:
                self.hue = self.service.configure_char("Hue", value=0, setter_callback=self._color)
                self.saturation = self.service.configure_char("Saturation", value=0, setter_callback=self._color)

        def _color(self, _value) -> None:
            def later():  # both halves of the colour have landed by the next loop turn
                rgb = colorsys.hsv_to_rgb(self.hue.value / 360, self.saturation.value / 100, 1)
                self._enqueue({"power": True, "color": to_hex(tuple(round(c * 255) for c in rgb))})
            self.export.driver.loop.call_soon_threadsafe(later)

        def sync(self, device: Device) -> None:
            if self.settling:
                return
            state = device.state
            if isinstance(state.get("power"), bool):
                self.on.set_value(state["power"])
            if self.brightness and isinstance(state.get("brightness"), int):
                self.brightness.set_value(max(1, state["brightness"]))
            if self.hue and state.get("color"):
                hue, sat, _ = colorsys.rgb_to_hsv(*(c / 255 for c in parse_color(state["color"])))
                self.hue.set_value(round(hue * 360))
                self.saturation.set_value(round(sat * 100))
            self.fault.set_value(1 if device.online is False else 0)

    class Thermostat(DeviceAccessory):
        """Off / Cool thermostat. Mode itself is left to the device: many air conditioners
        expose an unreliable mode setting, so we switch power and trust the unit's own mode."""

        def __init__(self, export, device, aid, options):
            super().__init__(export, device, aid, options)
            self.category = CATEGORY_THERMOSTAT
            spec = device.capabilities["temperature"]
            self.service = self.add_preload_service("Thermostat", chars=["StatusFault"])
            self.fault = self.service.get_characteristic("StatusFault")
            self.target = self.service.configure_char(
                "TargetTemperature", value=min(max(24, spec["min"]), spec["max"]),
                properties={"minValue": spec["min"], "maxValue": spec["max"], "minStep": 1},
                setter_callback=lambda v: self.queue({"temperature": int(v)}))
            self.current = self.service.configure_char("CurrentTemperature", value=25)
            self.mode = self.service.configure_char(
                "TargetHeatingCoolingState", valid_values={"Off": 0, "Cool": 2}, value=0,
                setter_callback=lambda v: self.queue({"power": v != 0}))
            # Home sends Heat or Auto regardless (for "turn it on", or a target above the room
            # temperature). Take any of them as "on" rather than failing the request.
            self.mode.allow_invalid_client_values = True
            self.current_mode = self.service.configure_char("CurrentHeatingCoolingState", value=0)
            self.service.configure_char("TemperatureDisplayUnits", value=0)

        def sync(self, device: Device) -> None:
            if self.settling:
                return
            state = device.state
            if isinstance(state.get("temperature"), (int, float)):
                self.target.set_value(state["temperature"])
            if isinstance(state.get("current_temperature"), (int, float)):
                self.current.set_value(state["current_temperature"])
            unmet = any(state.get(key) != needed for key, needed in self.options.get("require", {}).items())
            cooling = bool(state.get("power")) and not unmet
            self.mode.set_value(2 if cooling else 0)
            self.current_mode.set_value(2 if cooling else 0)
            self.fault.set_value(1 if device.online is False or (state.get("power") and unmet) else 0)

    class ActionSwitch(Accessory):
        """A device action as a switch: momentary with only `on`, two-way with `on` and `off`."""

        def __init__(self, export: HomeKitExport, entry: dict, aid: int):
            super().__init__(export.driver, entry["name"], aid=aid)
            self.export, self.entry, self.device_id = export, entry, entry["device"]
            self.category = CATEGORY_SWITCH
            self.set_info_service(firmware_revision=FIRMWARE, manufacturer="Glimt", model="action",
                                  serial_number=f"glimt-{aid}")
            self.service = self.add_preload_service("Switch", chars=["StatusFault"])
            self.on = self.service.configure_char("On", setter_callback=self._set)
            self.fault = self.service.get_characteristic("StatusFault")

        def _set(self, value) -> None:
            action = self.entry["on"] if value else self.entry.get("off")
            self.export.spawn(self._run(action))

        async def _run(self, action: str | None) -> None:
            try:
                if action:
                    await self.export.hub.act(self.device_id, action)
                self.fault.set_value(0)
            except Exception as exc:
                log.info("HomeKit action %s failed: %s", action, exc)
                self.fault.set_value(1)
            if "off" not in self.entry:  # momentary: spring back
                await asyncio.sleep(0.4)
                self.on.set_value(False)

        def sync(self, device: Device) -> None:
            pass

    return PowerAccessory, Thermostat, ActionSwitch


_classes = None


def _get(index: int):
    global _classes
    if _classes is None:
        _classes = _make_classes()
    return _classes[index]


def PowerAccessory(*args):
    return _get(0)(*args)


def Thermostat(*args):
    return _get(1)(*args)


def ActionSwitch(*args):
    return _get(2)(*args)
