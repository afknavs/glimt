"""Conformance checks every bridge must pass. Used by `glimt bridge test` and by the
test suite, so "does my new bridge work?" has one answer.

Checks are read-only unless ``write=True``, in which case each device is sent the value
it already has (so nothing visibly changes) to prove the write path and time it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from . import bridges, config as config_module
from .bridges.base import Bridge
from .hub import Hub
from .model import KINDS, Device

SPEC_TYPES = {"bool": set(), "int": {"min", "max"}, "enum": {"options"}, "color": set()}
FAST_MS = 1000


@dataclass
class Result:
    ok: bool
    name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{'✓' if self.ok else '✗'} {self.name}" + (f": {self.detail}" if self.detail else "")


def _check_device(device: Device, cfg: dict) -> list[Result]:
    results = []
    problems = []
    if device.kind not in KINDS:
        problems.append(f"kind '{device.kind}' is not one of {KINDS}")
    for name, spec in device.capabilities.items():
        kind = spec.get("type")
        if kind not in SPEC_TYPES:
            problems.append(f"capability '{name}' has unknown type {kind!r}")
        elif not SPEC_TYPES[kind] <= spec.keys():
            problems.append(f"capability '{name}' is missing {sorted(SPEC_TYPES[kind] - spec.keys())}")
    if not device.capabilities and not device.actions:
        problems.append("declares no capabilities and no actions, so nothing can control it")
    results.append(Result(not problems, f"{device.id}: device model is valid", "; ".join(problems)))
    return results


async def check(bridge_type: str, config: dict, *, write: bool = False, settle: float = 3.0) -> list[Result]:
    """Run the checks against the bridge instances of ``bridge_type`` in ``config``."""
    results: list[Result] = []
    config = config_module.check(config)
    names = [name for name, cfg in config["bridges"].items() if cfg["type"] == bridge_type]
    if not names:
        return [Result(False, f"config has a [bridges.*] table with type = \"{bridge_type}\"",
                       "add one, plus at least one [devices.*] that uses it")]
    try:
        cls = bridges.load(bridge_type)
        results.append(Result(issubclass(cls, Bridge), f"loads as a Bridge subclass ({cls.__module__}.{cls.__name__})"))
    except Exception as exc:
        return [Result(False, "bridge loads", str(exc))]

    hub = Hub({**config, "bridges": {name: config["bridges"][name] for name in names},
               "devices": {k: v for k, v in config["devices"].items() if v["bridge"] in names}})
    started = time.perf_counter()
    await hub.start()
    declare_ms = (time.perf_counter() - started) * 1000
    for name in names:
        if name in hub.bridge_errors:
            results.append(Result(False, f"{name}: constructs and declares devices", hub.bridge_errors[name]))
    owned = [device for device in hub.devices.values() if device.bridge in names]
    wanted = set(hub.config["devices"])
    results.append(Result({d.id for d in owned} == wanted, "devices() returns exactly the configured devices",
                          "" if {d.id for d in owned} == wanted else
                          f"configured {sorted(wanted)}, returned {sorted(d.id for d in owned)}"))
    results.append(Result(declare_ms < 500, "devices() does no slow I/O", f"{declare_ms:.0f} ms"))
    for device in owned:
        results += _check_device(device, hub.config["devices"].get(device.id, {}))

    if hub._tasks:
        await asyncio.wait(hub._tasks, timeout=settle + 30)
    for name in names:
        results.append(Result(name not in hub.bridge_errors, f"{name}: start() succeeds",
                              hub.bridge_errors.get(name, "")))
    await asyncio.sleep(settle)  # give listeners a moment to report first state
    for device in owned:
        if device.assumed:
            results.append(Result(True, f"{device.id}: marked assumed (cannot report state)"))
        elif device.capabilities:
            reported = device.online is not None and bool(device.state)
            results.append(Result(reported, f"{device.id}: reports state after start()",
                                  f"{device.state}" if reported else "no report() call seen; call self.report() "
                                  "from start() or a listener, or declare the device assumed=True"))

    if write:
        for device in owned:
            same = {key: device.state[key] for key in device.capabilities if key in device.state}
            if not same or device.online is False:
                continue
            key, value = next(iter(same.items()))
            try:
                outcome = await hub.apply(device.id, {key: value}, source="conformance")
                results.append(Result(outcome["ms"] < FAST_MS, f"{device.id}: apply() returns quickly",
                                      f"{outcome['ms']} ms (re-sent {key}={value})"))
            except Exception as exc:
                results.append(Result(False, f"{device.id}: apply() works", str(exc)))
        try:
            ghost = owned[0] if owned else None
            if ghost and ghost.capabilities:
                await hub.apply(ghost.id, {"__nope__": 1})
                results.append(Result(False, "invalid settings are rejected"))
        except ValueError:
            results.append(Result(True, "invalid settings are rejected before reaching the bridge"))
        except Exception as exc:
            results.append(Result(False, "invalid settings are rejected", str(exc)))

    started = time.perf_counter()
    await hub.stop()
    results.append(Result(time.perf_counter() - started < 6, "stop() returns promptly"))
    return results
