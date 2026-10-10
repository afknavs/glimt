"""Signals: short light-and-sound cues that tell a human something happened.

A signal borrows your lights for a moment and then puts them back exactly as they
were. That restore step is the whole point: an agent can interrupt the room without
changing it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from datetime import datetime

from . import paths
from .model import Device, parse_color, to_hex

log = logging.getLogger("glimt.signals")

PATTERNS = ("pulse", "flash", "breathe", "solid")
MAX_HOLD = 2 * 60 * 60  # an uncleared signal never holds your lights longer than this
# Seconds between attempts to put back a light that was unreachable: quick at first, then
# patient, for about an hour in all.
RETRY_DELAYS = (5, 20, 60, 300, 900, 1800)
UNTIL_UNMUTED = 10 * 365 * 86400  # "until I turn it back on": far enough ahead to mean never

BUILTIN: dict[str, dict] = {
    # Finished. Two calm green pulses, then your lights return.
    "done": {"color": "#30d158", "pattern": "pulse", "count": 2, "sound": "done"},
    # Blocked on you. Amber, and it stays amber until you clear it or five minutes pass.
    "attention": {"color": "#ff9f0a", "pattern": "breathe", "count": 2, "hold": 300, "sound": "attention"},
    # Something failed. Three sharp red flashes.
    "error": {"color": "#ff453a", "pattern": "flash", "count": 3, "sound": "error"},
    # FYI. One blue pulse, no sound.
    "info": {"color": "#0a84ff", "pattern": "pulse", "count": 1},
    # Busy. A dim indigo glow while a long job runs; clear it (or send another signal) to end.
    "working": {"color": "#5e5ce6", "pattern": "solid", "brightness": 25, "hold": -1},
}


def _in_quiet_hours(quiet: dict | None, now: datetime | None = None) -> bool:
    if not quiet or "from" not in quiet or "to" not in quiet:
        return False
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    start, end = (int(part[:2]) * 60 + int(part[3:5]) for part in (quiet["from"], quiet["to"]))
    return start <= current < end if start <= end else current >= start or current < end


class SignalEngine:
    def __init__(self, hub) -> None:
        self.hub = hub
        hub.signals = self
        self.config = hub.config.get("signals", {})
        self.log: deque = deque(maxlen=50)
        self.active: dict | None = None
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        # What each borrowed device looked like before the first signal touched it.
        self._snapshots: dict[str, dict] = {}
        # Devices a person took back mid-signal; the running signal leaves them alone.
        self._released: set[str] = set()
        # Borrowed lights a cue has actually changed. One that was out of reach the whole
        # time never showed the cue, so there is nothing to put back.
        self._touched: set[str] = set()
        # Bumped by every fire() and clear(). Work started under an older number stops as
        # soon as it notices, so two cues can never both believe they own the lights.
        self._generation = 0
        self._retry: asyncio.Task | None = None
        self._muted_until = self._read_mute()

    # ---- definitions --------------------------------------------------------------

    def definitions(self) -> dict[str, dict]:
        merged = {name: dict(spec) for name, spec in BUILTIN.items()}
        for name, spec in self.config.items():
            if isinstance(spec, dict) and name != "quiet":
                merged[name] = {**merged.get(name, {}), **spec}
        return merged

    def resolve(self, request: dict) -> dict:
        """Turn {'name': 'done'} or a fully custom request into a concrete spec."""
        spec: dict = {}
        name = request.get("name")
        if name:
            definitions = self.definitions()
            if name not in definitions:
                raise ValueError(f"No signal '{name}'. Signals: {', '.join(definitions)}; "
                                 "or pass color and pattern for a custom one")
            spec.update(definitions[name])
        for key in ("color", "pattern", "count", "hold", "sound", "brightness", "targets"):
            if request.get(key) is not None:
                spec[key] = request[key]
        if "color" not in spec and "sound" not in spec:
            raise ValueError("A signal needs a name, a color, or a sound")
        if "color" in spec:
            spec["color"] = to_hex(parse_color(spec["color"]))
        spec.setdefault("pattern", "pulse")
        if spec["pattern"] not in PATTERNS:
            raise ValueError(f"pattern must be one of: {', '.join(PATTERNS)}")
        spec["count"] = max(1, min(int(spec.get("count", 2)), 10))
        spec["brightness"] = max(1, min(int(spec.get("brightness", 100)), 100))
        hold = float(spec.get("hold", 0))
        spec["hold"] = MAX_HOLD if hold < 0 else min(hold, MAX_HOLD)
        spec["name"] = name or "custom"
        return spec

    def targets(self, spec: dict) -> list[Device]:
        wanted = spec.get("targets") or self.config.get("targets")
        if wanted:
            return [self.hub.get(device_id) for device_id in wanted]
        # Default: every light that can show colour, unless a device opted in or out.
        chosen = []
        for device in self.hub.devices.values():
            opted = device.signal
            if opted is True or (opted is None and device.kind == "light" and "color" in device.capabilities):
                chosen.append(device)
        return chosen

    def speaker(self) -> Device | None:
        wanted = self.config.get("speaker")
        if wanted is False:
            return None
        if isinstance(wanted, str):
            return self.hub.devices.get(wanted)
        return next((d for d in self.hub.devices.values() if d.kind == "speaker" and "play" in d.actions), None)

    # ---- mute ---------------------------------------------------------------------

    def muted(self) -> float | None:
        """The time cues stay silent until, or None. Survives a restart."""
        if self._muted_until is not None and self._muted_until <= time.time():
            self._muted_until = None
        return self._muted_until

    def mute(self, minutes: float | None = None) -> dict:
        """Silence cues for a while, or until unmuted. Urgent cues still show."""
        self._set_mute(time.time() + (minutes * 60 if minutes and minutes > 0 else UNTIL_UNMUTED))
        return {"ok": True, "mute": self._muted_until}

    def unmute(self) -> dict:
        self._set_mute(None)
        return {"ok": True, "mute": None}

    def _set_mute(self, until: float | None) -> None:
        self._muted_until = until
        try:
            if until is None:
                (paths.home() / "mute").unlink(missing_ok=True)
            else:
                (paths.home() / "mute").write_text(str(until))
        except OSError:
            pass
        self.hub.emit({"type": "mute", "mute": until})

    @staticmethod
    def _read_mute() -> float | None:
        try:
            until = float((paths.home() / "mute").read_text())
        except (OSError, ValueError):
            return None
        return until if until > time.time() else None

    # ---- firing -------------------------------------------------------------------

    def fire(self, request: dict) -> dict:
        """Start a signal and return immediately. The lights change in the background."""
        spec = self.resolve(request)
        entry = {"id": next(self._ids), "name": spec["name"], "message": request.get("message"),
                 "source": request.get("source"), "time": time.time()}
        silent = "quiet hours" if _in_quiet_hours(self.config.get("quiet")) else "muted" if self.muted() else None
        if silent and not request.get("urgent"):
            entry["skipped"] = silent
            self.log.appendleft(entry)
            self.hub.emit({"type": "signal", "signal": entry})
            return {"ok": True, "id": entry["id"], "skipped": silent}

        lights = self.targets(spec) if "color" in spec else []
        entry["targets"] = [device.id for device in lights]
        self._generation += 1
        if self._task and not self._task.done():
            self._task.cancel()  # the newer signal takes over; snapshots are kept
        self._released.clear()
        self.active = {**entry, "color": spec.get("color"), "hold": spec["hold"]}
        self.log.appendleft(entry)
        self._task = asyncio.create_task(self._run(spec, lights, self._generation))
        if spec.get("sound"):
            asyncio.create_task(self._play_sound(spec["sound"]))
        self.hub.emit({"type": "signal", "signal": self.active})
        return {"ok": True, "id": entry["id"], "targets": entry["targets"]}

    async def clear(self) -> dict:
        """End any signal now and restore the lights."""
        was_active = self.active is not None or bool(self._snapshots)
        self._generation += 1
        generation = self._generation
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._restore(generation)
        return {"ok": True, "cleared": was_active}

    async def shutdown(self) -> None:
        """Called as the daemon stops: never leave the room in a cue's colours."""
        try:
            await asyncio.wait_for(self.clear(), 6)
        except Exception:
            log.warning("could not restore every light before stopping")
        if self._retry and not self._retry.done():
            self._retry.cancel()

    def baseline(self, device: Device) -> dict:
        """The device's own state: what it looked like before a cue borrowed it, if one has."""
        return self._snapshots.get(device.id, device.state)

    def user_changed(self, device_id: str) -> None:
        """Someone took direct control through Glimt: stop borrowing that device."""
        if self._snapshots.pop(device_id, None) is None:
            return
        self._released.add(device_id)
        self._touched.discard(device_id)
        if not self._snapshots:
            self._generation += 1
            if self._task and not self._task.done():
                self._task.cancel()
            self.active = None
            self.hub.emit({"type": "signal", "signal": None})

    async def _run(self, spec: dict, lights: list[Device], generation: int) -> None:
        for device in lights:
            self._snapshots.setdefault(device.id, dict(device.state))
        try:
            # One light failing (unplugged, out of range) must not stop the others, and no
            # animation may still be running when restoring starts.
            results = await asyncio.gather(*(self._play(device, spec) for device in lights),
                                           return_exceptions=True)
            for device, result in zip(lights, results):
                if isinstance(result, Exception):
                    log.warning("signal %s could not play on %s: %s", spec["name"], device.id, result)
            if spec["hold"] > 0:
                await asyncio.sleep(spec["hold"])
        except asyncio.CancelledError:
            return  # replaced by a newer signal, cleared, or the user took over
        await self._restore(generation)

    async def _restore(self, generation: int) -> None:
        if self._generation != generation:
            return
        self.active = None
        self.hub.emit({"type": "signal", "signal": None})
        done = await self._restore_pass(generation)
        if done is False:  # something was unreachable: keep its snapshot and try again shortly
            if self._retry and not self._retry.done():
                self._retry.cancel()
            self._retry = asyncio.create_task(self._retry_restore(generation))
        if done is not None:
            self.hub.remember()

    async def _restore_pass(self, generation: int) -> bool | None:
        """Restore every borrowed light once. True: all restored. False: some could not be.
        None: a newer cue took over, and inherits whatever is still borrowed."""
        async def one(device_id: str, before: dict) -> bool:
            restored = device_id not in self._touched or await self._restore_one(device_id, before, generation)
            # Forget the snapshot only if it is still ours: not taken back by the user, and
            # not inherited by a newer cue, while this light was being restored.
            if restored and self._generation == generation and self._snapshots.get(device_id) is before:
                del self._snapshots[device_id]
                self._touched.discard(device_id)
            return restored
        # All lights at once: one that hangs must not keep the reachable ones in cue colours.
        results = await asyncio.gather(*(one(device_id, before) for device_id, before in list(self._snapshots.items())))
        if self._generation != generation:
            return None
        return all(results)

    async def _retry_restore(self, generation: int) -> None:
        for delay in RETRY_DELAYS:
            await asyncio.sleep(delay)
            if self._generation != generation:
                return
            done = await self._restore_pass(generation)
            if done is None:
                return
            if done:
                self.hub.remember()
                return
        if self._generation == generation and self._snapshots:
            log.warning("gave up restoring %s after a cue; it stayed unreachable", ", ".join(self._snapshots))
            self._snapshots.clear()
            self._touched.clear()

    async def _restore_one(self, device_id: str, before: dict, generation: int) -> bool:
        device = self.hub.devices.get(device_id)
        if device is None:
            return True
        was_on = bool(before.get("power"))
        look = {key: before[key] for key in ("effect", "color", "brightness")
                if key in before and key in device.capabilities}
        if look.get("effect"):
            look.pop("color", None)  # an effect was running; colour would cancel it
        else:
            look.pop("effect", None)
        try:
            if was_on:
                await self._set(device, {"power": True, **look})
            else:
                # Put the old colour back first so the next manual "on" is not signal-coloured.
                if look:
                    await self._set(device, look)
                if self._generation != generation:
                    return False  # a newer cue started between the two steps: it owns the light now
                await self._set(device, {"power": False})
            return True
        except Exception as exc:
            log.warning("could not restore %s: %s", device_id, exc)
            return False

    # ---- rendering ----------------------------------------------------------------

    async def _set(self, device: Device, changes: dict) -> None:
        usable = {key: value for key, value in changes.items() if key in device.capabilities}
        if usable and device.id not in self._released:
            await self.hub.apply(device.id, usable, source="signal")
            self._touched.add(device.id)

    async def _play(self, device: Device, spec: dict) -> None:
        caps = device.capabilities
        bright = spec["brightness"]
        if "color" not in caps and "brightness" not in caps:
            await self._blink(device, spec)
            return
        on = {"power": True, "color": spec["color"], "brightness": bright}
        pattern, count = spec["pattern"], spec["count"]
        if pattern == "solid":
            await self._set(device, on)
        elif pattern == "flash":
            for index in range(count):
                await self._set(device, on)
                await asyncio.sleep(0.16)
                if index < count - 1 or not spec["hold"]:
                    await self._set(device, {"brightness": 1} if "brightness" in caps else {"power": False})
                    await asyncio.sleep(0.14)
        elif pattern == "breathe":
            ramp = [12, 35, 60, 85, 100, 85, 60, 35, 12]
            await self._set(device, {**on, "brightness": ramp[0] * bright // 100 or 1})
            for _ in range(count):
                for level in ramp:
                    await self._set(device, {"brightness": max(1, level * bright // 100)})
                    await asyncio.sleep(0.13)
        else:  # pulse
            for index in range(count):
                await self._set(device, on)
                await asyncio.sleep(0.42)
                if index < count - 1 or not spec["hold"]:
                    await self._set(device, {"brightness": max(1, bright // 12)})
                    await asyncio.sleep(0.26)
        if spec["hold"]:
            await self._set(device, on)

    async def _blink(self, device: Device, spec: dict) -> None:
        """On/off-only devices (relays): a slow blink, capped so we do not wear the switch."""
        if "power" not in device.capabilities:
            return
        start = bool(device.state.get("power"))
        for _ in range(min(spec["count"], 3)):
            await self._set(device, {"power": not start})
            await asyncio.sleep(0.6)
            await self._set(device, {"power": start})
            await asyncio.sleep(0.6)

    async def _play_sound(self, sound: str) -> None:
        speaker = self.speaker()
        if speaker is None:
            return
        try:
            await self.hub.act(speaker.id, "play", {"sound": sound})
        except Exception as exc:
            log.debug("sound failed: %s", exc)

    def status(self) -> dict:
        return {"active": self.active, "recent": list(self.log)[:10], "signals": self.definitions(),
                "mute": self.muted()}
