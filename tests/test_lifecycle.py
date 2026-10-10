"""Starting, stopping and losing connections: the places a long-running daemon goes wrong."""

import asyncio
import json
import os
import threading
import time
import types

import pytest

from glimt import client, paths

from conftest import demo_config, started_hub


def test_stop_only_signals_the_daemon_that_answered_and_waits_for_it_to_go(monkeypatch, home):
    (home / "daemon.pid").write_text("1")                        # a stale file naming someone else's process
    sent = []
    alive = {"polls": 3}                                         # still shutting down for the first few looks

    def kill(pid, sig):
        sent.append((pid, sig))
        if sig == 0:
            alive["polls"] -= 1
            if alive["polls"] < 0:
                raise ProcessLookupError
    monkeypatch.setattr(os, "kill", kill)

    def nobody_home(*args):
        raise ConnectionRefusedError
    monkeypatch.setattr(client, "_once", nobody_home)
    assert client.stop_daemon() is False and sent == []

    monkeypatch.setattr(client, "_once", lambda *args: {"pid": 4242})
    assert client.stop_daemon() is True
    assert sent[0][0] == 4242 and {pid for pid, _ in sent} == {4242}
    assert len(sent) == 5                                        # asked to stop, then watched until it was gone

    alive["polls"] = 10 ** 9                                     # one that never exits is reported, not assumed stopped
    with pytest.raises(client.DaemonError, match="has not stopped"):
        client.stop_daemon(wait=0.2)


def test_cloud_commands_leave_in_order_even_when_a_caller_gives_up(run):
    from glimt.bridges.tuya import TuyaBridge
    paths.write_private(paths.secrets_dir() / "tuya.json", json.dumps({"devices": {}}))

    async def scenario():
        bridge = TuyaBridge("tuya", {}, hub=None)
        sent = []

        def send(what):
            time.sleep(0.15 if what == "on" else 0)              # the first command is slow to leave
            sent.append((what, threading.current_thread().name))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bridge._in_order(send, "on"), 0.02)
        await bridge._in_order(send, "off")                      # issued later, so it must land later
        assert [what for what, _ in sent] == ["on", "off"]
        assert {thread for _, thread in sent} == {"tuya-cloud_0"}
        bridge._writer.shutdown()
    run(scenario())


def test_a_bluetooth_connection_that_fails_half_way_is_closed(run, monkeypatch):
    bleak = pytest.importorskip("bleak")
    from glimt.bridges import elk_ble
    closed = []

    class Scanner:
        @staticmethod
        async def find_device_by_address(address, timeout):
            return types.SimpleNamespace(name="MELK-OA10", address=address)

    class Client:
        is_connected = True

        def __init__(self, *args, **kwargs):
            self.services = self

        async def connect(self):
            pass

        def get_service(self, uuid):
            return self

        def get_characteristic(self, uuid):
            return self

        async def write_gatt_char(self, *args, **kwargs):
            raise OSError("link dropped during the handshake")

        async def disconnect(self):
            closed.append(True)
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    monkeypatch.setattr(bleak, "BleakClient", Client)

    async def scenario():
        strip = elk_ble.Strip(bridge=None, device_id="strip", cfg={"address": "AA:BB:CC:DD:EE:FF"})
        with pytest.raises(OSError):
            await strip.connect()
        assert closed == [True] and strip.client is None         # the strip is free for the next attempt
    run(scenario())


def test_apple_home_starts_over_after_its_event_stream_is_cut(run):
    from glimt.homekit import HomeKitExport

    async def scenario():
        hub, _ = await started_hub(demo_config())
        pushed = []
        export = types.SimpleNamespace(hub=hub, _push=lambda device: pushed.append(device.id))
        listening = asyncio.create_task(HomeKitExport._listen(export))
        await asyncio.sleep(0)
        everything = list(hub.devices)
        assert pushed == everything
        for number in range(201):                                # more than a listener may fall behind
            hub.emit({"type": "tick", "n": number})
        await asyncio.sleep(0.01)
        assert pushed == everything * 2                          # resubscribed and resent the whole picture
        hub.close_streams()
        await asyncio.wait_for(listening, 1)                     # and it ends cleanly at shutdown
        await hub.stop()
    run(scenario())
