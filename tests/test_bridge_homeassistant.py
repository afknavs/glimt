"""Home Assistant's shared socket, entity mappings and discovery with fake transports."""

import asyncio
import json
import sys

import httpx
import pytest

from glimt import config as config_module, conformance, paths
from glimt.bridges.base import BridgeError
from glimt.bridges.homeassistant import HomeAssistantBridge
from glimt.hub import Hub
from glimt.model import Cap


class Socket:
    def __init__(self, states):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.states = states
        self.queue.put_nowait(json.dumps({"type": "auth_required"}))

    async def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message["type"] == "auth":
            assert message["access_token"] == "test-token"
            self.queue.put_nowait(json.dumps({"type": "auth_ok"}))
        elif message["type"] in ("get_states", "subscribe_events"):
            self.queue.put_nowait(json.dumps({"type": "result", "id": message["id"], "success": True,
                                              "result": self.states if message["type"] == "get_states" else None}))

    async def recv(self):
        message = await self.queue.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def close(self):
        self.closed = True

    def state(self, entity, state, attributes):
        self.queue.put_nowait(json.dumps({"type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": entity, "new_state": {"entity_id": entity, "state": state, "attributes": attributes}}}}))


@pytest.fixture
def ha(monkeypatch):
    paths.write_private(paths.secrets_dir() / "homeassistant.json",
                        json.dumps({"homeassistant": {"token": "test-token"}}))
    states = [
        {"entity_id": "light.desk_lamp", "state": "on", "attributes": {"friendly_name": "Desk lamp",
         "brightness": 128, "rgb_color": [255, 0, 32], "supported_color_modes": ["rgb"]}},
        {"entity_id": "switch.plug", "state": "off", "attributes": {"friendly_name": "Plug"}},
        {"entity_id": "input_boolean.focus", "state": "on", "attributes": {}},
        {"entity_id": "fan.ceiling", "state": "on", "attributes": {"friendly_name": "Ceiling fan"}},
        {"entity_id": "climate.ac", "state": "cool", "attributes": {"friendly_name": "AC", "temperature": 24,
         "current_temperature": 25.5, "fan_mode": "auto", "fan_modes": ["auto", "low", "high"]}},
    ]
    fake = {"states": states, "sockets": [], "offline": False}

    async def connect(self):
        if fake["offline"]:
            raise OSError("simulated disconnection")
        socket = Socket(states)
        fake["sockets"].append(socket)
        return socket

    monkeypatch.setattr(HomeAssistantBridge, "_connect", connect)
    # The fake connection must work even when the optional package is absent.
    monkeypatch.setitem(sys.modules, "websockets", None)
    return fake


def config():
    return config_module.check({"bridges": {"homeassistant": {"url": "http://homeassistant.local:8123"}},
        "devices": {
            "desk": {"name": "Desk", "kind": "light", "bridge": "homeassistant", "entity": "light.desk_lamp",
                     "capabilities": ["power", "brightness", "color"],
                     "actions": {"movie": "scene.movie", "start": "script.start", "reset": "button.reset"}},
            "plug": {"name": "Plug", "kind": "switch", "bridge": "homeassistant", "entity": "switch.plug"},
            "focus": {"name": "Focus", "kind": "switch", "bridge": "homeassistant", "entity": "input_boolean.focus"},
            "fan": {"name": "Fan", "kind": "fan", "bridge": "homeassistant", "entity": "fan.ceiling"},
            "ac": {"name": "AC", "kind": "climate", "bridge": "homeassistant", "entity": "climate.ac",
                   "capabilities": ["power", "temperature", "fan"], "fan_modes": ["auto", "low", "high"]}}})


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 3)


def calls(socket):
    return [message for message in socket.sent if message["type"] == "call_service"]


def test_devices(ha):
    hub = Hub(config())
    bridge = HomeAssistantBridge("homeassistant", hub.config["bridges"]["homeassistant"], hub)
    devices = {device.id: device for device in bridge.devices()}
    assert devices["desk"].capabilities == {"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()}
    assert devices["desk"].actions == ["movie", "start", "reset"]
    assert devices["plug"].capabilities == devices["focus"].capabilities == devices["fan"].capabilities == {
        "power": Cap.bool()}
    assert devices["ac"].capabilities == {"power": Cap.bool(), "temperature": Cap.integer(16, 30, "°C"),
                                          "fan": Cap.enum(["auto", "low", "high"])}
    assert not ha["sockets"]
    hub.config["devices"]["desk"].pop("capabilities")
    assert bridge.devices()[0].capabilities == {"power": Cap.bool(), "brightness": Cap.percent()}
    hub.config["devices"]["ac"].pop("capabilities")
    assert bridge.devices()[-1].capabilities == {"power": Cap.bool(), "temperature": Cap.integer(16, 30, "°C")}
    hub.config["devices"]["desk"].pop("entity")
    with pytest.raises(BridgeError, match=r"\[devices.desk\].*entity"):
        bridge.devices()


def test_socket_commands_and_external_state(run, ha):
    async def scenario():
        hub = Hub(config())
        await hub.start()
        try:
            await until(lambda: all(device.online is True for device in hub.devices.values()))
            bridge = hub.bridges["homeassistant"]
            socket = ha["sockets"][0]
            assert [message["type"] for message in socket.sent] == ["auth", "get_states", "subscribe_events"]
            assert socket.sent[2]["event_type"] == "state_changed"
            assert hub.get("desk").state == {"power": True, "brightness": 50, "color": "#ff0020"}
            assert hub.get("ac").state == {"power": True, "temperature": 24, "current_temperature": 25.5, "fan": "auto"}
            # No call_service replies are queued: returning proves commands do not await confirmation.
            await asyncio.wait_for(bridge.apply(hub.get("desk"), {
                "power": True, "brightness": 40, "color": "#123456"}), 0.2)
            assert calls(socket)[-1] == {"id": 3, "type": "call_service", "domain": "light", "service": "turn_on",
                "service_data": {"entity_id": "light.desk_lamp", "brightness_pct": 40,
                                 "rgb_color": [18, 52, 86], "transition": 0.1}}
            await bridge.apply(hub.get("desk"), {"power": False})
            assert calls(socket)[-1]["service"] == "turn_off"
            assert calls(socket)[-1]["service_data"] == {"entity_id": "light.desk_lamp", "transition": 0.1}
            for device_id in ("plug", "focus", "fan"):
                await bridge.apply(hub.get(device_id), {"power": False})
                assert calls(socket)[-1]["domain"] == "homeassistant"
                assert calls(socket)[-1]["service"] == "turn_off"
                await bridge.apply(hub.get(device_id), {"power": True})
                assert calls(socket)[-1]["service"] == "turn_on"
            await bridge.apply(hub.get("ac"), {"power": False, "temperature": 22, "fan": "high"})
            assert [{key: value for key, value in message.items() if key != "id"}
                    for message in calls(socket)[-3:]] == [
                {"type": "call_service", "domain": "climate", "service": "set_temperature",
                 "service_data": {"entity_id": "climate.ac", "temperature": 22}},
                {"type": "call_service", "domain": "climate", "service": "set_fan_mode",
                 "service_data": {"entity_id": "climate.ac", "fan_mode": "high"}},
                {"type": "call_service", "domain": "climate", "service": "turn_off",
                 "service_data": {"entity_id": "climate.ac"}}]
            assert hub.get("desk").state == {"power": True, "brightness": 50, "color": "#ff0020"}
            await bridge.apply(hub.get("ac"), {"power": True})
            assert calls(socket)[-1]["service"] == "turn_on"
            for action, domain, service in (("movie", "scene", "turn_on"), ("start", "script", "turn_on"),
                                            ("reset", "button", "press")):
                await bridge.act(hub.get("desk"), action, {})
                assert calls(socket)[-1]["domain"] == domain and calls(socket)[-1]["service"] == service
            assert len(ha["sockets"]) == 1
            ids = [message["id"] for message in socket.sent if "id" in message]
            assert ids == sorted(set(ids))
            socket.state("light.desk_lamp", "off", {"brightness": 255, "rgb_color": [0, 255, 0]})
            await until(lambda: hub.get("desk").state.get("color") == "#00ff00")
            assert hub.get("desk").state == {"power": False, "brightness": 100, "color": "#00ff00"}
            socket.state("climate.ac", "off", {"temperature": 22, "current_temperature": 23.5,
                                               "fan_mode": "low"})
            await until(lambda: hub.get("ac").state.get("fan") == "low")
            assert hub.get("ac").state == {"power": False, "temperature": 22, "current_temperature": 23.5, "fan": "low"}
            assert "current_temperature" not in hub.get("ac").capabilities
            socket.state("light.desk_lamp", "unavailable", {})
            await until(lambda: hub.get("desk").online is False)
            assert "unavailable" in hub.get("desk").error
            socket.state("light.desk_lamp", "on", {"brightness": 0, "rgb_color": [0, 0, 0]})
            await until(lambda: hub.get("desk").online is True)
            assert hub.get("desk").error is None and hub.get("desk").state["brightness"] == 0
            ha["offline"] = True
            socket.queue.put_nowait(OSError("simulated socket loss"))
            await until(lambda: all(device.online is False for device in hub.devices.values()))
            assert all("reconnecting" in device.error for device in hub.devices.values())
            with pytest.raises(BridgeError, match="disconnected"):
                await bridge.apply(hub.get("desk"), {"power": True})
            ha["offline"] = False
            await until(lambda: len(ha["sockets"]) == 2 and all(d.online is True for d in hub.devices.values()))
            assert all(device.error is None for device in hub.devices.values())
        finally:
            await hub.stop()
        assert all(socket.closed for socket in ha["sockets"]) and bridge.listener.done()
    run(scenario())


def test_conformance(run, ha):
    results = run(conformance.check("homeassistant", config(), write=True, settle=0.2))
    assert all(result.ok for result in results), [str(result) for result in results if not result.ok]
    assert len(calls(ha["sockets"][0])) == 5


def test_missing_token(run):
    hub = Hub(config())
    bridge = HomeAssistantBridge("homeassistant", hub.config["bridges"]["homeassistant"], hub)
    with pytest.raises(BridgeError, match="glimt setup homeassistant"):
        run(bridge.start())
    assert run(HomeAssistantBridge.discover({"url": "http://homeassistant.local:8123"})) == []


def test_discover(run, ha, monkeypatch):
    original = httpx.AsyncClient
    requests = []
    ha["states"].append({"entity_id": "light.plug", "state": "on", "attributes": {
        "friendly_name": "Plain bulb", "supported_color_modes": ["onoff"]}})

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert str(request.url) == "http://homeassistant.local:8123/api/states"
        return httpx.Response(200, json=ha["states"])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        original(transport=httpx.MockTransport(handle), **kwargs))
    assert run(HomeAssistantBridge.discover({"url": "http://homeassistant.local:8123/"})) == [
        {"id": "desk-lamp", "name": "Desk lamp", "kind": "light", "entity": "light.desk_lamp",
         "capabilities": ["power", "brightness", "color"]},
        {"id": "plug", "name": "Plug", "kind": "switch", "entity": "switch.plug", "capabilities": ["power"]},
        {"id": "ceiling", "name": "Ceiling fan", "kind": "fan", "entity": "fan.ceiling", "capabilities": ["power"]},
        {"id": "ac", "name": "AC", "kind": "climate", "entity": "climate.ac",
         "capabilities": ["power", "temperature", "fan"], "fan_modes": ["auto", "low", "high"]},
        {"id": "plug-2", "name": "Plain bulb", "kind": "light", "entity": "light.plug", "capabilities": ["power"]}]
    assert len(requests) == 1


def test_command_rejection(run, ha):
    async def scenario():
        hub = Hub(config())
        await hub.start()
        try:
            await until(lambda: hub.get("desk").online is True)
            await hub.apply("desk", {"power": True})
            socket = ha["sockets"][0]
            socket.queue.put_nowait(json.dumps({"id": calls(socket)[-1]["id"], "type": "result", "success": False,
                                               "error": {"message": "simulated service rejection"}}))
            await until(lambda: hub.get("desk").error is not None)
            assert "rejected" in hub.get("desk").error
        finally:
            await hub.stop()
    run(scenario())


@pytest.mark.parametrize("url,expected", [
    ("http://homeassistant.local:8123/", "ws://homeassistant.local:8123/api/websocket"),
    ("https://ha.example", "wss://ha.example/api/websocket")])
def test_connection_factory(run, monkeypatch, url, expected):
    from types import SimpleNamespace

    seen = []
    sentinel = object()

    async def connect(address, **options):
        seen.append((address, options))
        return sentinel

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    hub = Hub(config())
    bridge = HomeAssistantBridge("homeassistant", {"url": url}, hub)
    assert run(bridge._connect()) is sentinel
    assert seen == [(expected, {"open_timeout": 5})]


def test_send_failure(run, ha, monkeypatch):
    async def scenario():
        hub = Hub(config())
        await hub.start()
        try:
            await until(lambda: hub.get("desk").online is True)
            socket = ha["sockets"][0]

            async def fail(text):
                raise OSError("simulated write failure")

            monkeypatch.setattr(socket, "send", fail)
            with pytest.raises(BridgeError, match="could not be sent"):
                await hub.bridges["homeassistant"].apply(hub.get("desk"), {"power": True})
            assert all(device.online is False and device.error for device in hub.devices.values())
            assert socket.closed
        finally:
            await hub.stop()
    run(scenario())


def test_authentication_rejected(run, ha, monkeypatch):
    async def scenario():
        hub = Hub(config())
        socket = Socket(ha["states"])

        async def send(text):
            socket.queue.put_nowait(json.dumps({"type": "auth_invalid"}))

        async def connect(self):
            return socket

        monkeypatch.setattr(socket, "send", send)
        monkeypatch.setattr(HomeAssistantBridge, "_connect", connect)
        await hub.start()
        try:
            await until(lambda: hub.get("desk").online is False)
            assert "token" in hub.get("desk").error and socket.closed
        finally:
            await hub.stop()
    run(scenario())
