import asyncio
import json

import pytest

from glimt import config as config_module
from glimt.bridges.base import BridgeError
from glimt.hub import Hub, UnknownDevice

from conftest import demo_config, started_hub

# A bridge dropped into ~/.glimt/bridges/, the way a user (or their agent) adds one.
REMOTE = '''
from glimt.bridges.base import Bridge
from glimt.model import Cap

class RemoteBridge(Bridge):
    type = "remote"
    sent = []

    def devices(self):
        return [self.make_device(i, cfg, assumed=True, capabilities={"power": Cap.bool(), "color": Cap.color()})
                for i, cfg in self.device_configs.items()]

    async def apply(self, device, changes):
        self.sent.append((device.id, changes))
'''


def remote_config(home) -> dict:
    (home / "bridges").mkdir(exist_ok=True)
    (home / "bridges" / "remote.py").write_text(REMOTE)
    return config_module.check({"bridges": {"remote": {}},
                                "devices": {"tv-light": {"bridge": "remote", "kind": "light"}}})


def test_apply_validates_sends_and_records_state(run):
    async def scenario():
        hub, _ = await started_hub(demo_config())
        result = await hub.apply("lamp", {"power": "on", "color": "blue"})
        assert result["ok"] and result["state"]["power"] is True and result["state"]["color"] == "#0a84ff"
        with pytest.raises(UnknownDevice, match="No device 'nope'. Devices: lamp, strip, fan"):
            await hub.apply("nope", {"power": True})
        with pytest.raises(ValueError):
            await hub.apply("fan", {"color": "red"})  # the fan has no colour
        await hub.stop()
    run(scenario())


def test_a_failing_command_reports_why_and_keeps_the_old_state(run):
    async def scenario():
        hub, _ = await started_hub(demo_config(fail=True))
        with pytest.raises(BridgeError, match="Simulated failure"):
            await hub.apply("lamp", {"power": True})
        assert hub.devices["lamp"].state["power"] is False
        assert hub.devices["lamp"].error == "Simulated failure"
        await hub.stop()
    run(scenario())


def test_one_broken_bridge_does_not_take_the_others_down(run):
    async def scenario():
        config = demo_config()
        config["bridges"]["ghost"] = {"type": "does-not-exist"}
        config["devices"]["phantom"] = {"bridge": "ghost", "name": "Phantom", "kind": "light"}
        hub, _ = await started_hub(config)
        assert "No bridge of type 'does-not-exist'" in hub.bridge_errors["ghost"]
        assert hub.devices["phantom"].online is False
        assert (await hub.apply("lamp", {"power": True}))["ok"]
        with pytest.raises(BridgeError):
            await hub.apply("phantom", {"power": True})
        await hub.stop()
    run(scenario())


def test_commands_to_one_device_run_in_the_order_they_arrived(run):
    async def scenario():
        hub, _ = await started_hub(demo_config(latency_ms=30))
        await asyncio.gather(*(hub.apply("lamp", {"brightness": level}) for level in (10, 20, 30, 40)))
        assert hub.devices["lamp"].state["brightness"] == 40
        await hub.stop()
    run(scenario())


def test_listeners_see_changes_and_are_released_at_shutdown(run):
    async def scenario():
        hub, _ = await started_hub(demo_config())
        queue = hub.subscribe()
        await hub.apply("lamp", {"power": True})
        event = queue.get_nowait()
        assert event["type"] == "device" and event["device"]["id"] == "lamp" and event["device"]["state"]["power"]
        hub.close_streams()
        while (event := queue.get_nowait()) is not None:
            pass
        await hub.stop()
    run(scenario())


def test_a_listener_that_stops_reading_is_cut_off_instead_of_left_stale(run):
    async def scenario():
        hub, _ = await started_hub(demo_config())
        stalled, healthy = hub.subscribe(), hub.subscribe()
        for number in range(200):
            hub.emit({"type": "tick", "n": number})
            healthy.get_nowait()
        hub.emit({"type": "tick", "n": 200})                     # one more than the stalled listener can hold
        assert stalled.qsize() == 1 and stalled.get_nowait() is None   # told to start over, nothing half-delivered
        assert healthy.get_nowait() == {"type": "tick", "n": 200}
        hub.emit({"type": "tick", "n": 201})
        assert stalled.empty()                                   # and it is no longer subscribed
        await hub.stop()
    run(scenario())


def test_a_local_bridge_file_loads_and_assumed_state_survives_a_restart(run, home):
    async def scenario():
        hub, _ = await started_hub(remote_config(home))
        device = hub.devices["tv-light"]
        assert device.assumed and device.bridge == "remote"
        await hub.apply("tv-light", {"power": True, "color": "#123456"})
        await hub.stop()
        assert json.loads((home / "state.json").read_text()) == {"tv-light": {"power": True, "color": "#123456"}}

        again, _ = await started_hub(remote_config(home))
        assert again.devices["tv-light"].state == {"power": True, "color": "#123456"}
        await again.stop()
    run(scenario())


def test_reports_from_another_thread_reach_the_hub(run):
    async def scenario():
        hub, _ = await started_hub(demo_config())
        await asyncio.to_thread(hub.report, "lamp", {"brightness": 7}, online=False, error="unplugged")
        await asyncio.sleep(0.01)
        device = hub.devices["lamp"]
        assert (device.state["brightness"], device.online, device.error) == (7, False, "unplugged")
        hub.report("lamp", online=True)
        assert device.error is None
        await hub.stop()
    run(scenario())


def test_config_mistakes_are_explained():
    with pytest.raises(config_module.ConfigError, match=r"\[devices.lamp\]: unknown bridge 'hue' \(defined: none defined\)"):
        config_module.check({"devices": {"lamp": {"bridge": "hue"}}})
    with pytest.raises(config_module.ConfigError, match="kind must be one of"):
        config_module.check({"bridges": {"virtual": {}}, "devices": {"lamp": {"bridge": "virtual", "kind": "lamp"}}})
    with pytest.raises(config_module.ConfigError, match="ids are lowercase"):
        config_module.check({"bridges": {"virtual": {}}, "devices": {"My Lamp": {"bridge": "virtual"}}})
    checked = config_module.check({"bridges": {"virtual": {}}, "devices": {"desk-lamp": {"bridge": "virtual"}}})
    assert checked["devices"]["desk-lamp"]["name"] == "Desk lamp" and checked["bridges"]["virtual"]["type"] == "virtual"
