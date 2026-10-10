"""WLED protocol and conformance checks against an in-memory HTTP strip."""

import asyncio
import json
import sys

import httpx
import pytest

from glimt import config as config_module, conformance
from glimt.bridges.base import BridgeError
from glimt.bridges.wled import WledBridge
from glimt.hub import Hub
from glimt.model import Cap


@pytest.fixture
def strip(monkeypatch):
    raw = {"on": True, "bri": 128, "seg": [{"col": [[255, 0, 32, 0]], "fx": 9}]}
    fake = {"raw": raw, "offline": False, "requests": [], "clients": []}

    def handle(request):
        fake["requests"].append(request)
        assert request.url.path == "/json/state"
        if fake["offline"]:
            raise httpx.ConnectError("simulated disconnection", request=request)
        if request.method == "GET":
            return httpx.Response(200, json=fake["raw"])
        return httpx.Response(200, json={"success": True})

    def client(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        fake["clients"].append(http)
        return http

    monkeypatch.setattr(WledBridge, "_client", client)
    return fake


def config(**options):
    return config_module.check({"bridges": {"wled": {"poll_seconds": 0.01}}, "devices": {
        "strip": {"name": "Strip", "kind": "light", "bridge": "wled", "host": "192.168.1.70:81",
                  "effects": {"rainbow": 9}, **options}}})


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 2)


def test_devices(strip):
    hub = Hub(config())
    bridge = WledBridge("wled", hub.config["bridges"]["wled"], hub)
    device = bridge.devices()[0]
    assert device.id == "strip" and not device.assumed
    assert device.capabilities == {"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color(),
                                   "effect": Cap.enum(["rainbow"])}
    assert strip["requests"] == []
    hub.config["devices"]["strip"].pop("effects")
    assert "effect" not in bridge.devices()[0].capabilities
    hub.config["devices"]["strip"].pop("host")
    with pytest.raises(BridgeError, match=r"\[devices.strip\].*host"):
        bridge.devices()


def test_poll_apply_and_recovery(run, strip):
    async def scenario():
        hub = Hub(config())
        await hub.start()
        try:
            device = hub.get("strip")
            await until(lambda: device.online is True)
            assert device.state == {"power": True, "brightness": 50, "color": "#ff0020", "effect": "rainbow"}
            bridge = hub.bridges["wled"]
            before = len(strip["requests"])
            await bridge.apply(device, {"power": True, "brightness": 40, "color": "#123456", "effect": "rainbow"})
            requests = strip["requests"][before:]
            assert [request.method for request in requests] == ["POST"]
            assert requests[0].url.host == "192.168.1.70" and requests[0].url.port == 81
            assert json.loads(requests[0].content) == {"tt": 1, "on": True, "bri": 102,
                                                      "seg": [{"col": [[18, 52, 86]], "fx": 9}]}
            device.config["transition_ms"] = 300
            await bridge.apply(device, {"power": False, "brightness": 100})
            assert json.loads(strip["requests"][-1].content) == {"tt": 3, "on": False, "bri": 255}
            strip["raw"] = {"on": False, "bri": 255, "seg": [{"col": [[0, 255, 0]], "fx": 0}]}
            await until(lambda: device.state.get("color") == "#00ff00")
            assert device.state == {"power": False, "brightness": 100, "color": "#00ff00", "effect": None}
            strip["offline"] = True
            await until(lambda: device.online is False)
            assert device.error and "Wi-Fi" in device.error
            with pytest.raises(BridgeError, match="WLED command"):
                await bridge.apply(device, {"power": True})
            strip["offline"] = False
            await until(lambda: device.online is True)
            assert device.error is None and len(strip["clients"]) == 1
        finally:
            await hub.stop()
        assert strip["clients"][0].is_closed and bridge.poller.done()
    run(scenario())


def test_conformance(run, strip):
    results = run(conformance.check("wled", config(), write=True, settle=0.2))
    assert all(result.ok for result in results), [str(result) for result in results if not result.ok]
    assert any(request.method == "POST" for request in strip["requests"])


def test_discover_without_zeroconf(run, monkeypatch):
    monkeypatch.setitem(sys.modules, "zeroconf", None)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", None)
    assert run(WledBridge.discover({})) == []


def test_discover_with_zeroconf(run, monkeypatch):
    from types import SimpleNamespace

    closed = []

    class Zeroconf:
        def __init__(self):
            self.zeroconf = self

        async def async_close(self):
            closed.append("zeroconf")

    class Browser:
        def __init__(self, zeroconf, service_type, handlers):
            assert service_type == "_wled._tcp.local."
            handlers[0](zeroconf=zeroconf, service_type=service_type,
                        name="Desk._wled._tcp.local.", state_change=None)

        async def async_cancel(self):
            closed.append("browser")

    class Info:
        port = 81

        def __init__(self, service_type, name):
            assert service_type == "_wled._tcp.local." and name == "Desk._wled._tcp.local."

        async def async_request(self, zeroconf, timeout):
            return True

        def parsed_scoped_addresses(self):
            return ["192.168.1.70"]

    monkeypatch.setitem(sys.modules, "zeroconf", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", SimpleNamespace(
        AsyncServiceBrowser=Browser, AsyncServiceInfo=Info, AsyncZeroconf=Zeroconf))
    assert run(WledBridge.discover({})) == [{"id": "strip-1", "name": "Desk", "kind": "light",
                                           "host": "192.168.1.70:81"}]
    assert closed == ["browser", "zeroconf"]
