import asyncio
import json
import time
from datetime import datetime

import pytest

from glimt import signals as signals_module
from glimt.bridges.base import BridgeError
from glimt.signals import SignalEngine, _in_quiet_hours

from conftest import demo_config, started_hub

QUICK = {"color": "#ff0000", "pattern": "flash", "count": 1}
HELD = {"color": "#00ff00", "pattern": "solid", "hold": 30}


async def finished(signals) -> None:
    if signals._task:
        await asyncio.wait_for(asyncio.shield(signals._task), 5)


def look(hub, device_id: str) -> dict:
    return dict(hub.devices[device_id].state)


def test_a_signal_borrows_the_colour_lights_and_puts_them_back(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        await hub.apply("lamp", {"power": True, "color": "#123456", "brightness": 33})
        before = {name: look(hub, name) for name in ("lamp", "strip", "fan")}
        seen = []
        original = hub.apply

        async def spy(device_id, changes, source="api"):
            seen.append((device_id, dict(changes)))
            return await original(device_id, changes, source)
        hub.apply = spy

        result = signals.fire({"name": "done"})
        assert result["targets"] == ["lamp", "strip"]          # the fan has no colour: left alone
        assert signals.active["name"] == "done"
        await finished(signals)

        assert ("lamp", {"power": True, "color": "#30d158", "brightness": 100}) in seen
        assert {name: look(hub, name) for name in before} == before   # lit lamp and dark strip both restored
        assert signals.active is None
        await hub.stop()
    run(scenario())


def test_attention_holds_until_cleared(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        before = look(hub, "lamp")
        signals.fire({"name": "attention"})
        await asyncio.sleep(2.9)                                # the breathe animation is over
        assert signals.active["name"] == "attention"
        assert look(hub, "lamp")["color"] == "#ff9f0a" and look(hub, "lamp")["power"] is True
        assert (await signals.clear())["cleared"] is True
        assert look(hub, "lamp") == before and signals.active is None
        await hub.stop()
    run(scenario())


def test_a_newer_signal_takes_over_and_the_room_still_returns_to_its_original_look(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        await hub.apply("lamp", {"power": True, "color": "#abcdef"})
        before = look(hub, "lamp")
        signals.fire({"name": "working"})                       # holds a dim glow
        await asyncio.sleep(0.05)
        assert look(hub, "lamp")["color"] == "#5e5ce6"
        signals.fire(QUICK)
        await finished(signals)
        assert look(hub, "lamp") == before
        await hub.stop()
    run(scenario())


def test_taking_manual_control_releases_that_light(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        signals.fire({"name": "working"})
        await asyncio.sleep(0.05)
        await hub.apply("lamp", {"color": "#00ff00"})           # a person picks a colour mid-signal
        await signals.clear()
        assert look(hub, "lamp")["color"] == "#00ff00"          # their choice wins
        assert look(hub, "strip")["power"] is False             # the other light is still restored
        await hub.stop()
    run(scenario())


def test_on_off_only_devices_blink_and_end_where_they_started(run):
    async def scenario():
        config = demo_config()
        config["devices"]["fan"]["signal"] = True
        config["signals"] = {"targets": ["fan"]}
        hub, signals = await started_hub(config)
        signals.fire({**QUICK, "count": 1})
        await asyncio.sleep(0.1)
        assert look(hub, "fan")["power"] is True
        await finished(signals)
        assert look(hub, "fan")["power"] is False
        await hub.stop()
    run(scenario())


def test_quiet_hours_skip_cues_unless_urgent(run):
    async def scenario():
        config = demo_config()
        config["signals"] = {"quiet": {"from": "00:00", "to": "23:59"}}
        hub, signals = await started_hub(config)
        assert signals.fire({"name": "done"})["skipped"] == "quiet hours"
        assert signals.active is None
        assert "skipped" not in signals.fire({**QUICK, "urgent": True})
        await finished(signals)
        await hub.stop()
    run(scenario())
    night = {"from": "23:00", "to": "07:00"}
    assert _in_quiet_hours(night, datetime(2026, 1, 1, 23, 30)) and _in_quiet_hours(night, datetime(2026, 1, 1, 6, 59))
    assert not _in_quiet_hours(night, datetime(2026, 1, 1, 12, 0)) and not _in_quiet_hours(None)


def test_custom_and_configured_signals_resolve(run):
    async def scenario():
        config = demo_config()
        config["signals"] = {"deploy": {"color": "purple", "pattern": "flash", "count": 99}, "done": {"count": 1}}
        hub, signals = await started_hub(config)
        assert signals.resolve({"name": "deploy"})["color"] == "#bf5af2"
        assert signals.resolve({"name": "deploy"})["count"] == 10          # clamped
        assert signals.resolve({"name": "done"})["count"] == 1             # built-in, overridden
        assert signals.resolve({"color": "teal"})["name"] == "custom"
        assert signals.resolve({"name": "working"})["hold"] == 7200        # "until cleared" is still bounded
        with pytest.raises(ValueError, match="No signal 'nope'"):
            signals.resolve({"name": "nope"})
        with pytest.raises(ValueError, match="needs a name, a color, or a sound"):
            signals.resolve({})
        await hub.stop()
    run(scenario())


# ---- when things go wrong mid-cue ---------------------------------------------------


def unplug(hub, device_id: str):
    """Make one device fail every command; returns a function that plugs it back in."""
    bridge = hub.bridges[hub.devices[device_id].bridge]
    original = bridge.apply

    async def apply(device, changes):
        if device.id == device_id:
            raise BridgeError("unplugged")
        await original(device, changes)
    bridge.apply = apply
    return lambda: setattr(bridge, "apply", original)


def test_one_unreachable_light_does_not_stop_the_others(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        await hub.apply("lamp", {"power": True, "color": "#123456"})
        before = look(hub, "lamp")
        unplug(hub, "strip")
        seen = []
        original = hub.apply

        async def spy(device_id, changes, source="api"):
            seen.append((device_id, changes.get("color")))
            return await original(device_id, changes, source)
        hub.apply = spy
        signals.fire(QUICK)
        await finished(signals)
        assert ("lamp", "#ff0000") in seen                       # the healthy light still showed the cue
        assert look(hub, "lamp") == before and signals.active is None
        commands = len(seen)
        await asyncio.sleep(0.4)
        assert len(seen) == commands                             # no animation left running to repaint it
        await signals.shutdown()
        await hub.stop()
    run(scenario())


def test_a_light_that_never_showed_the_cue_is_not_put_back_later(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        plug_in = unplug(hub, "strip")                           # out of range before the cue even starts
        signals.fire(QUICK)
        await finished(signals)
        assert not signals._snapshots and signals._retry is None  # nothing borrowed, so nothing owed
        plug_in()
        await hub.apply("strip", {"power": True, "color": "#abcdef"})   # set some other way since
        signals.fire(QUICK)
        await finished(signals)
        assert look(hub, "strip")["color"] == "#abcdef"          # and a later cue restores to *that*
        await hub.stop()
    run(scenario())


def test_a_light_that_cannot_be_put_back_is_retried_until_it_can(run, monkeypatch):
    monkeypatch.setattr(signals_module, "RETRY_DELAYS", (0.05, 0.05))

    async def scenario():
        hub, signals = await started_hub(demo_config())
        await hub.apply("lamp", {"power": True, "color": "#123456", "brightness": 40})
        before = look(hub, "lamp")
        signals.fire(HELD)
        await asyncio.sleep(0.05)
        plug_in = unplug(hub, "lamp")
        await signals.clear()
        assert look(hub, "lamp")["color"] == "#00ff00"           # still stuck in the cue's colour...
        assert signals.active is None and signals.baseline(hub.devices["lamp"]) == before   # ...and not forgotten
        plug_in()
        await asyncio.sleep(0.2)
        assert look(hub, "lamp") == before and not signals._snapshots
        await hub.stop()
    run(scenario())


def test_a_cue_fired_while_the_last_one_is_being_put_back_keeps_the_lights(run):
    async def scenario():
        hub, signals = await started_hub(demo_config(latency_ms=40))
        before = look(hub, "lamp")                               # off, warm white
        signals.fire({"name": "working"})
        await asyncio.sleep(0.15)
        clearing = asyncio.create_task(signals.clear())          # restoring takes two slow steps...
        await asyncio.sleep(0.01)
        signals.fire(HELD)                                       # ...and a new cue arrives between them
        await clearing
        await asyncio.sleep(0.3)
        assert signals.active["name"] == "custom"
        assert (look(hub, "lamp")["power"], look(hub, "lamp")["color"]) == (True, "#00ff00")
        await signals.clear()
        assert look(hub, "lamp") == before                       # and the original look still comes back
        await hub.stop()
    run(scenario())


def test_stopping_the_daemon_puts_a_held_cue_back(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        before = {name: look(hub, name) for name in ("lamp", "strip")}
        signals.fire({"name": "working"})
        await asyncio.sleep(0.05)
        assert look(hub, "lamp")["power"] is True
        await signals.shutdown()
        assert {name: look(hub, name) for name in before} == before and signals.active is None
        await hub.stop()
    run(scenario())


def test_a_light_that_hangs_does_not_keep_the_others_in_cue_colours_at_shutdown(run):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        await hub.apply("lamp", {"power": True, "color": "#123456"})
        before = look(hub, "lamp")
        signals.fire(HELD)
        await asyncio.sleep(0.05)
        bridge, original = hub.bridges["virtual"], hub.bridges["virtual"].apply

        async def apply(device, changes):                        # the strip stops answering altogether
            if device.id == "strip":
                await asyncio.sleep(60)
            await original(device, changes)
        bridge.apply = apply
        stopping = asyncio.create_task(signals.shutdown())
        await asyncio.sleep(0.2)
        assert look(hub, "lamp") == before                       # restored at once, not queued behind the strip
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        await hub.stop()
    run(scenario())


def test_a_cue_colour_is_never_saved_as_a_devices_own_state(run, home):
    from test_hub import remote_config

    async def scenario():
        hub, signals = await started_hub(remote_config(home))    # a light that cannot report its state
        await hub.apply("tv-light", {"power": True, "color": "#123456"})
        signals.fire({**HELD, "targets": ["tv-light"]})
        await asyncio.sleep(0.05)
        assert look(hub, "tv-light")["color"] == "#00ff00"
        hub._write_remembered()                                  # a save lands in the middle of the cue
        saved = json.loads((home / "state.json").read_text())
        assert saved == {"tv-light": {"power": True, "color": "#123456"}}
        await signals.shutdown()
        await hub.stop()
        assert json.loads((home / "state.json").read_text()) == saved
    run(scenario())


def test_mute_silences_cues_survives_a_restart_and_runs_out(run, home):
    async def scenario():
        hub, signals = await started_hub(demo_config())
        heard = hub.subscribe()
        until = signals.mute(30)["mute"]
        assert 29 * 60 < until - time.time() <= 30 * 60 and heard.get_nowait() == {"type": "mute", "mute": until}
        assert signals.fire({"name": "done"})["skipped"] == "muted" and signals.active is None
        assert "skipped" not in signals.fire({**QUICK, "urgent": True})          # urgent still gets through
        await finished(signals)
        assert SignalEngine._read_mute() == until                                # a restarted daemon stays quiet
        assert signals.mute()["mute"] - time.time() > 365 * 86400                # no minutes: until unmuted

        assert signals.unmute() == {"ok": True, "mute": None} and not (home / "mute").exists()
        assert "skipped" not in signals.fire(QUICK)
        await finished(signals)
        signals.mute(1)
        signals._muted_until = time.time() - 1                                   # the minute has passed
        (home / "mute").write_text(str(time.time() - 1))
        assert signals.muted() is None and SignalEngine._read_mute() is None
        await hub.stop()
    run(scenario())
