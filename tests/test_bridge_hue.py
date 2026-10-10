"""Hue HTTP commands, SSE state and pairing without a network."""

import asyncio
import json

import httpx
import pytest

from glimt import config as config_module, conformance, paths
from glimt.bridges.base import BridgeError
from glimt.bridges.hue import HueBridge, rgb_to_xy, xy_to_rgb
from glimt.hub import Hub
from glimt.model import Cap


class Events(httpx.AsyncByteStream):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def __aiter__(self):
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self):
        self.closed = True

    def push(self, data):
        self.queue.put_nowait(("data: " + json.dumps(data) + "\n\n").encode())


@pytest.fixture
def hue(monkeypatch):
    paths.write_private(paths.secrets_dir() / "hue.json", json.dumps({"hue": {"key": "test-key"}}))
    light = {"id": "light-id", "type": "light", "metadata": {"name": "Desk Lamp"},
             "owner": {"rid": "device-id"}, "on": {"on": True}, "dimming": {"brightness": 50},
             "color": {"xy": {"x": 0.3, "y": 0.3}}}
    fake = {"lights": [light], "requests": [], "streams": [], "offline": False, "reject": False, "clients": []}
    original = httpx.AsyncClient

    def handle(request):
        fake["requests"].append(request)
        if fake["offline"]:
            raise httpx.ConnectError("simulated disconnection", request=request)
        if request.url.path == "/eventstream/clip/v2":
            assert request.headers["Accept"] == "text/event-stream"
            events = Events()
            fake["streams"].append(events)
            return httpx.Response(200, stream=events)
        if request.method == "PUT":
            return httpx.Response(200, json={"errors": [{"description": "simulated error"}] if fake["reject"] else [],
                                            "data": []})
        return httpx.Response(200, json={"data": fake["lights"], "errors": []})

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        http = original(*args, **kwargs)
        fake["clients"].append(http)
        return http

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return fake


def config(**options):
    return config_module.check({"bridges": {"hue": {"host": "192.168.1.71"}}, "devices": {
        "desk": {"name": "Desk", "kind": "light", "bridge": "hue", "light_id": "light-id",
                 "capabilities": ["power", "brightness", "color"], **options}}})


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 3)


def test_devices(hue):
    hub = Hub(config())
    bridge = HueBridge("hue", hub.config["bridges"]["hue"], hub)
    device = bridge.devices()[0]
    assert device.capabilities == {"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()}
    assert device.id == "desk" and not device.assumed and not hue["requests"]
    hub.config["devices"]["desk"].pop("capabilities")
    assert bridge.devices()[0].capabilities == {"power": Cap.bool(), "brightness": Cap.percent()}
    hub.config["devices"]["desk"].pop("light_id")
    with pytest.raises(BridgeError, match=r"\[devices.desk\].*light_id"):
        bridge.devices()


def test_first_state_apply_push_and_recovery(run, hue):
    async def scenario():
        hub = Hub(config())
        await hub.start()
        try:
            device = hub.get("desk")
            await until(lambda: bool(hue["streams"]))
            assert device.online is True
            assert device.state == {"power": True, "brightness": 50, "color": "#bfb8d0"}
            bridge = hub.bridges["hue"]
            before = len(hue["requests"])
            await bridge.apply(device, {"power": False, "brightness": 40, "color": "#ff0000"})
            requests = hue["requests"][before:]
            assert [request.method for request in requests] == ["PUT"]
            request = requests[0]
            assert request.url.path == "/clip/v2/resource/light/light-id"
            assert request.headers["hue-application-key"] == "test-key"
            assert json.loads(request.content) == {"on": {"on": False}, "dimming": {"brightness": 40},
                                                  "color": {"xy": {"x": 0.639999925519409, "y": 0.33000006827387507}},
                                                  "dynamics": {"duration": 100}}
            hue["streams"][-1].push([{"type": "update", "data": [{"id": "light-id", "type": "light",
                "on": {"on": False}, "dimming": {"brightness": 100}, "color": {"xy": rgb_to_xy("#00ff00")}}]}])
            await until(lambda: device.state.get("color") == "#00ff00")
            assert device.state["brightness"] == 100 and device.state["power"] is False
            hue["streams"][-1].push([{"data": [{"id": "light-id", "type": "light", "on": {"on": True}}]}])
            await until(lambda: device.state["power"] is True)
            assert device.state["color"] == "#00ff00"
            hue["streams"][-1].push([{"data": [{"type": "zigbee_connectivity", "owner": {"rid": "device-id"},
                                                "status": "disconnected"}]}])
            await until(lambda: device.online is False)
            assert "power" in device.error
            hue["streams"][-1].push([{"data": [{"type": "zigbee_connectivity", "owner": {"rid": "device-id"},
                                                "status": "connected"}]}])
            await until(lambda: device.online is True)
            assert device.error is None
            hue["streams"][-1].queue.put_nowait(httpx.ReadError("simulated stream loss"))
            await until(lambda: device.online is False)
            assert "reconnecting" in device.error
            await until(lambda: len(hue["streams"]) == 2 and device.online is True)
            assert device.error is None and len(hue["clients"]) == 1
            hue["reject"] = True
            with pytest.raises(BridgeError, match="rejected"):
                await bridge.apply(device, {"power": True})
        finally:
            await hub.stop()
        assert hue["clients"][0].is_closed and all(stream.closed for stream in hue["streams"])
    run(scenario())


def test_conformance(run, hue):
    results = run(conformance.check("hue", config(), write=True, settle=0.2))
    assert all(result.ok for result in results), [str(result) for result in results if not result.ok]
    assert any(request.method == "PUT" for request in hue["requests"])


def test_missing_key(run, home):
    hub = Hub(config())
    bridge = HueBridge("hue", hub.config["bridges"]["hue"], hub)
    with pytest.raises(BridgeError, match="glimt setup hue"):
        run(bridge.start())
    assert run(HueBridge.discover({"host": "192.168.1.71"})) == []


def test_discover(run, hue):
    hue["lights"].append({"id": "plain-id", "on": {"on": False}, "metadata": {"name": "Desk Lamp"}})
    assert run(HueBridge.discover({"host": "192.168.1.71"})) == [
        {"id": "desk-lamp", "name": "Desk Lamp", "kind": "light", "light_id": "light-id",
         "capabilities": ["power", "brightness", "color"]},
        {"id": "desk-lamp-2", "name": "Desk Lamp", "kind": "light", "light_id": "plain-id", "capabilities": ["power"]}]


@pytest.mark.parametrize("response,expected", [([{ "success": {"username": "test-key"}}], "test-key"),
                                               ([{"error": {"type": 101}}], None)])
def test_pair(run, monkeypatch, response, expected):
    original = httpx.AsyncClient

    def handle(request):
        assert request.method == "POST" and str(request.url) == "https://192.168.1.71/api"
        assert json.loads(request.content) == {"devicetype": "glimt#computer", "generateclientkey": True}
        return httpx.Response(200, json=response)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        original(transport=httpx.MockTransport(handle), **kwargs))
    if expected:
        assert run(HueBridge.pair("192.168.1.71")) == expected
    else:
        with pytest.raises(BridgeError, match="Press the round button on the Hue bridge, then try again"):
            run(HueBridge.pair("192.168.1.71"))


def test_color_conversion():
    assert rgb_to_xy("#000000") == {"x": 0.0, "y": 0.0}
    assert xy_to_rgb({"x": 0.0, "y": 0.0}, 100) == "#000000"
    assert xy_to_rgb(rgb_to_xy("#ffffff"), 100) == "#ffffff"
    assert xy_to_rgb(rgb_to_xy("#ff0000"), 100) == "#ff0000"


def test_http_failure_and_partial_updates(run, hue):
    async def scenario():
        hue["offline"] = True
        hub = Hub(config())
        await hub.start()
        try:
            device = hub.get("desk")
            await until(lambda: device.online is False)
            assert device.error and "network" in device.error
            hue["offline"] = False
            await until(lambda: device.online is True and bool(hue["streams"]))
            assert device.error is None
            hue["streams"][-1].push([{"data": [{"id": "light-id", "type": "light",
                                                "dimming": {"min_dim_level": 0.2}}]}])
            await asyncio.sleep(0.01)
            assert device.state["brightness"] == 50
            hue["streams"][-1].push([{"type": "delete", "data": [{"id": "light-id", "type": "light"}]}])
            await until(lambda: device.online is False)
            assert "removed" in device.error
        finally:
            await hub.stop()
    run(scenario())
