"""The hub: one registry of devices, one fast path for commands, one stream of events."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from . import bridges, paths
from .bridges.base import Bridge, BridgeError
from .model import Device, validate

log = logging.getLogger("glimt.hub")

APPLY_TIMEOUT = 8.0
START_TIMEOUT = 30.0


class UnknownDevice(KeyError):
    pass


class Hub:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.devices: dict[str, Device] = {}
        self.bridges: dict[str, Bridge] = {}
        self.bridge_errors: dict[str, str] = {}
        self.signals = None  # set by SignalEngine
        self.started = time.time()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        self._save_handle: asyncio.TimerHandle | None = None
        self.closing = False

    # ---- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        for name, cfg in self.config["bridges"].items():
            try:
                bridge = bridges.load(cfg["type"])(name, cfg, self)
                for device in bridge.devices():
                    self.devices[device.id] = device
                self.bridges[name] = bridge
            except Exception as exc:  # one broken bridge must never take the others down
                self._fail_bridge(name, exc)
        self._load_remembered()
        # Connect in the background so the API is up immediately.
        self._tasks = [asyncio.create_task(self._start_bridge(bridge)) for bridge in self.bridges.values()]

    async def _start_bridge(self, bridge: Bridge) -> None:
        try:
            await asyncio.wait_for(bridge.start(), START_TIMEOUT)
        except Exception as exc:
            self._fail_bridge(bridge.name, exc)

    def _fail_bridge(self, name: str, exc: Exception) -> None:
        message = str(exc) or type(exc).__name__
        if not isinstance(exc, BridgeError):
            log.exception("bridge %s failed", name)
        self.bridge_errors[name] = message
        if name in self.bridges:
            self.bridges[name].error = message
        for device_id, cfg in self.config["devices"].items():
            if cfg["bridge"] == name:
                device = self.devices.setdefault(
                    device_id, Device(id=device_id, name=cfg["name"], kind=cfg["kind"], bridge=name, config=cfg))
                device.online, device.error = False, message
        log.warning("bridge %s: %s", name, message)

    # Devices that cannot report (infrared, cheap Bluetooth) only have "what we last sent".
    # Remember it across restarts so cues can still restore them correctly.

    def _load_remembered(self) -> None:
        path = paths.home() / "state.json"
        try:
            remembered = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            remembered = {}
        for device_id, state in remembered.items():
            device = self.devices.get(device_id)
            if device and device.assumed and isinstance(state, dict):
                device.state.update({key: value for key, value in state.items() if key in device.capabilities})

    def remember(self) -> None:
        """Schedule a save of what assumed-state devices were last told."""
        if self._save_handle is None and self._loop:
            self._save_handle = self._loop.call_later(1.0, self._write_remembered)

    def _write_remembered(self) -> None:
        self._save_handle = None
        # A light a cue is borrowing is saved as it was before the cue, never in cue colours.
        own = self.signals.baseline if self.signals else (lambda device: device.state)
        data = {device.id: own(device) for device in self.devices.values() if device.assumed and own(device)}
        try:
            (paths.home() / "state.json").write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    async def stop(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._write_remembered()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*(self._stop_bridge(bridge) for bridge in self.bridges.values()))

    async def _stop_bridge(self, bridge: Bridge) -> None:
        try:
            await asyncio.wait_for(bridge.stop(), 5)
        except Exception:
            log.debug("bridge %s did not stop cleanly", bridge.name, exc_info=True)

    # ---- state --------------------------------------------------------------------

    def get(self, device_id: str) -> Device:
        try:
            return self.devices[device_id]
        except KeyError:
            known = ", ".join(self.devices) or "none configured"
            raise UnknownDevice(f"No device '{device_id}'. Devices: {known}") from None

    def report(self, device_id: str, state: dict | None = None, *, online: bool | None = None,
               route: str | None = None, error: str | None = None) -> None:
        """Record a device's real state. Callable from any thread."""
        if self._loop and threading.get_ident() != self._loop_thread:
            self._loop.call_soon_threadsafe(lambda: self._report(device_id, state, online, route, error))
        else:
            self._report(device_id, state, online, route, error)

    def _report(self, device_id, state, online, route, error) -> None:
        device = self.devices.get(device_id)
        if device is None:
            return
        before = (dict(device.state), device.online, device.route, device.error)
        if state:
            device.state.update(state)
        if online is not None:
            device.online = online
            if online:
                device.error = None
        if route is not None:
            device.route = route
        if error is not None:
            device.error = error
        if before != (device.state, device.online, device.route, device.error):
            device.touch()
            self.emit({"type": "device", "device": device.public()})

    # ---- commands -----------------------------------------------------------------

    async def apply(self, device_id: str, changes: dict, source: str = "api") -> dict:
        device = self._usable(device_id)
        clean = validate(device, changes)
        if source != "signal" and self.signals:
            self.signals.user_changed(device_id)
        started = time.perf_counter()
        await self._run(device, lambda bridge: bridge.apply(device, clean))
        device.state.update(clean)
        device.error = None
        device.touch()
        if device.assumed and source != "signal":
            self.remember()
        self.emit({"type": "device", "device": device.public()})
        return {"ok": True, "device": device.id, "state": device.state,
                "ms": round((time.perf_counter() - started) * 1000)}

    async def act(self, device_id: str, action: str, args: dict | None = None) -> dict:
        device = self._usable(device_id)
        if action not in device.actions:
            available = ", ".join(device.actions) or "none"
            raise ValueError(f"{device.id} has no action '{action}'. Actions: {available}")
        started = time.perf_counter()
        result = await self._run(device, lambda bridge: bridge.act(device, action, args or {}))
        out: dict[str, Any] = {"ok": True, "device": device.id, "action": action,
                               "ms": round((time.perf_counter() - started) * 1000)}
        if result is not None:
            out["result"] = result
        return out

    def _usable(self, device_id: str) -> Device:
        device = self.get(device_id)
        if device.bridge not in self.bridges:  # its bridge never loaded: say why, not "no such setting"
            raise BridgeError(self.bridge_errors.get(device.bridge, "Its bridge is not running"))
        return device

    async def _run(self, device: Device, call):
        bridge = self.bridges.get(device.bridge)
        if bridge is None or (bridge.error and device.online is False):
            raise BridgeError(self.bridge_errors.get(device.bridge, "Bridge is not running"))
        lock = self._locks.setdefault(device.id, asyncio.Lock())
        async with lock:  # commands to one device run in the order they arrived
            try:
                return await asyncio.wait_for(call(bridge), APPLY_TIMEOUT)
            except BridgeError as exc:
                self._report(device.id, None, None, None, str(exc))
                raise
            except asyncio.TimeoutError:
                message = f"{device.name} did not respond within {APPLY_TIMEOUT:.0f}s"
                self._report(device.id, None, None, None, message)
                raise BridgeError(message) from None
            except Exception as exc:
                log.exception("unexpected error from bridge %s", bridge.name)
                message = f"{type(exc).__name__}: {exc}"
                self._report(device.id, None, None, None, message)
                raise BridgeError(message) from None

    async def refresh(self, device_id: str | None = None) -> None:
        targets = [self.get(device_id)] if device_id else list(self.devices.values())
        await asyncio.gather(*(self._refresh(device) for device in targets), return_exceptions=True)

    async def _refresh(self, device: Device) -> None:
        bridge = self.bridges.get(device.bridge)
        if bridge:
            await asyncio.wait_for(bridge.refresh(device), APPLY_TIMEOUT)

    # ---- events -------------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        return [device.public() for device in self.devices.values()]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def close_streams(self) -> None:
        """End every event stream. Called first at shutdown, so long-lived listeners do not
        hold the server open until its timeout."""
        self.closing = True
        for queue in list(self._subscribers):
            self._end(queue)

    def _end(self, queue: asyncio.Queue) -> None:
        """Finish one listener's stream: drop what it has not read and leave only the end marker."""
        self._subscribers.discard(queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    def emit(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled listener never blocks the hub. Its stream is ended rather than left
                # silently incomplete, so it reconnects and starts again from a full snapshot.
                self._end(queue)
