"""Home Assistant entities over one persistent, authenticated WebSocket.

    [bridges.homeassistant]
    url = "http://homeassistant.local:8123"

    [devices.desk]
    name = "Desk lamp"
    kind = "light"
    bridge = "homeassistant"
    entity = "light.desk"
    capabilities = ["power", "brightness", "color"]
    actions = { movie = "scene.movie" }

The long-lived token lives in secrets/homeassistant.json; run `glimt setup homeassistant`.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from .. import paths
from ..model import Cap, Device, parse_color, to_hex
from .base import Bridge, BridgeError

DEFAULTS = {"light": ["power", "brightness"], "switch": ["power"], "input_boolean": ["power"],
            "fan": ["power"], "climate": ["power", "temperature"]}
SPECS = {"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color(),
         "temperature": Cap.integer(16, 30, "°C")}


def load_token(name: str) -> str:
    try:
        token = json.loads((paths.secrets_dir() / "homeassistant.json").read_text())[name]["token"]
        if isinstance(token, str) and token.strip():
            return token
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise BridgeError("Home Assistant is not linked yet. Run: glimt setup homeassistant")


class HomeAssistantBridge(Bridge):
    type = "homeassistant"
    extra = "homeassistant"

    def __init__(self, name: str, config: dict, hub) -> None:
        super().__init__(name, config, hub)
        self.socket = None
        self.listener: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.pending: dict[int, str] = {}
        self.token = ""

    async def _connect(self):
        from websockets import connect

        url = self.config["url"].rstrip("/")
        url = ("wss" if url.startswith("https:") else "ws") + url[url.index(":"):]
        return await connect(f"{url}/api/websocket", open_timeout=5)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=5)

    def devices(self) -> list[Device]:
        if not self.config.get("url"):
            raise BridgeError(f'[bridges.{self.name}] needs url = "http://homeassistant.local:8123"')
        made = []
        for device_id, cfg in self.device_configs.items():
            domain = cfg.get("entity", "").partition(".")[0]
            if domain not in DEFAULTS or not cfg["entity"].partition(".")[2]:
                raise BridgeError(f'[devices.{device_id}] needs entity = "light.desk" '
                                  '(light/switch/input_boolean/fan/climate)')
            names = cfg.get("capabilities", DEFAULTS[domain])
            allowed = {"light": {"power", "brightness", "color"}, "climate": {"power", "temperature", "fan"}}
            if any(name not in allowed.get(domain, {"power"}) for name in names):
                raise BridgeError(f"[devices.{device_id}] capabilities do not match the {domain} entity")
            caps = {name: SPECS[name] for name in names if name != "fan"}
            if "fan" in names:
                if not cfg.get("fan_modes"):
                    raise BridgeError(f"[devices.{device_id}] needs fan_modes when declaring fan")
                caps["fan"] = Cap.enum(cfg["fan_modes"])
            for entity in cfg.get("actions", {}).values():
                if entity.partition(".")[0] not in ("scene", "script", "button") or not entity.partition(".")[2]:
                    raise BridgeError(f"[devices.{device_id}] actions must target scene, script or button entities")
            made.append(self.make_device(device_id, cfg, capabilities=caps, actions=list(cfg.get("actions", {}))))
        return made

    async def start(self) -> None:
        self.token = load_token(self.name)
        self.listener = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self.listener:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)

    def _offline(self) -> None:
        for device_id in self.device_configs:
            self.report(device_id, online=False,
                        error="Home Assistant connection lost; check its server, URL and token; reconnecting")

    def _state(self, raw: dict) -> None:
        for device_id, cfg in self.device_configs.items():
            if cfg["entity"] != raw.get("entity_id"):
                continue
            if raw.get("state") in ("unavailable", "unknown"):
                self.report(device_id, online=False, error="Entity is unavailable in Home Assistant; check its device")
                continue
            domain = cfg["entity"].partition(".")[0]
            caps = cfg.get("capabilities", DEFAULTS[domain])
            attrs = raw.get("attributes", {})
            state = {}
            if "power" in caps:
                state["power"] = raw["state"] != "off" if domain == "climate" else raw["state"] == "on"
            if "brightness" in caps and attrs.get("brightness") is not None:
                state["brightness"] = round(attrs["brightness"] * 100 / 255)
            if "color" in caps and attrs.get("rgb_color") is not None:
                state["color"] = to_hex(tuple(attrs["rgb_color"]))
            if "temperature" in caps and attrs.get("temperature") is not None:
                state["temperature"] = round(attrs["temperature"])
            if domain == "climate" and attrs.get("current_temperature") is not None:
                state["current_temperature"] = attrs["current_temperature"]
            if "fan" in caps and attrs.get("fan_mode") is not None:
                state["fan"] = attrs["fan_mode"]
            self.report(device_id, state, online=True, route="hub")

    async def _send(self, socket, message: dict, device_id: str | None = None) -> int:
        async with self.lock:
            self.sequence += 1
            message = {"id": self.sequence, **message}
            if device_id:
                self.pending[self.sequence] = device_id
            await socket.send(json.dumps(message))
            return self.sequence

    async def _listen(self) -> None:
        backoff = 1.0
        while True:
            socket = None
            try:
                socket = await self._connect()
                hello = json.loads(await asyncio.wait_for(socket.recv(), 10))
                if hello.get("type") != "auth_required":
                    raise BridgeError("Home Assistant did not request authentication")
                await socket.send(json.dumps({"type": "auth", "access_token": self.token}))
                auth = json.loads(await asyncio.wait_for(socket.recv(), 10))
                if auth.get("type") != "auth_ok":
                    raise BridgeError("Home Assistant rejected authentication")
                states_id = await self._send(socket, {"type": "get_states"})
                subscription_id = await self._send(socket, {"type": "subscribe_events", "event_type": "state_changed"})
                self.socket = socket
                backoff = 1.0
                while True:
                    message = json.loads(await socket.recv())
                    if message.get("type") == "result":
                        device_id = self.pending.pop(message["id"], None)
                        if message["id"] in (states_id, subscription_id) and not message.get("success"):
                            raise BridgeError("Home Assistant rejected the state subscription")
                        if message["id"] == states_id:
                            reported = set()
                            for state in message["result"]:
                                self._state(state)
                                reported.add(state["entity_id"])
                            for configured_id, cfg in self.device_configs.items():
                                if cfg["entity"] not in reported:
                                    self.report(configured_id, online=False,
                                                error="Entity was not found in Home Assistant; check entity in config")
                        elif device_id and not message.get("success"):
                            self.report(device_id,
                                        error="Home Assistant rejected the command; check the entity and service")
                    elif message.get("type") == "event":
                        data = message.get("event", {}).get("data", {})
                        if data.get("new_state") is not None:
                            self._state(data["new_state"])
                        elif data.get("entity_id"):
                            self._state({"entity_id": data["entity_id"], "state": "unavailable"})
            except asyncio.CancelledError:
                raise
            except ModuleNotFoundError:  # retrying cannot fix a missing package: say so and stop
                for device_id in self.device_configs:
                    self.report(device_id, online=False, error="Home Assistant needs one more package. Install it "
                                "with: pip install 'glimt[homeassistant]', then: glimt restart")
                return
            except Exception:
                self._offline()
            finally:
                self.socket = None
                self.pending.clear()
                if socket is not None:
                    try:
                        await socket.close()
                    except Exception:
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _service(self, device: Device, domain: str, service: str, data: dict) -> None:
        socket = self.socket
        if socket is None:
            raise BridgeError("Home Assistant is disconnected; check its server, URL and token")
        try:
            await self._send(socket, {"type": "call_service", "domain": domain, "service": service,
                                      "service_data": data}, device.id)
        except Exception:
            self.socket = None
            self._offline()
            try:
                await socket.close()
            except Exception:
                pass
            raise BridgeError("Home Assistant command could not be sent; reconnecting") from None

    async def apply(self, device: Device, changes: dict) -> None:
        entity = device.config["entity"]
        domain = entity.partition(".")[0]
        data = {"entity_id": entity}
        if domain == "light":
            if "brightness" in changes:
                data["brightness_pct"] = changes["brightness"]
            if "color" in changes:
                data["rgb_color"] = list(parse_color(changes["color"]))
            if changes.get("power") is False:
                data = {"entity_id": entity}
            data["transition"] = 0.1
            await self._service(device, "light", "turn_off" if changes.get("power") is False else "turn_on", data)
        elif domain == "climate":
            if "temperature" in changes:
                await self._service(device, domain, "set_temperature", {**data, "temperature": changes["temperature"]})
            if "fan" in changes:
                await self._service(device, domain, "set_fan_mode", {**data, "fan_mode": changes["fan"]})
            if "power" in changes:
                await self._service(device, domain, "turn_on" if changes["power"] else "turn_off", data)
        else:
            await self._service(device, "homeassistant", "turn_on" if changes["power"] else "turn_off", data)

    async def act(self, device: Device, action: str, args: dict) -> None:
        entity = device.config.get("actions", {}).get(action)
        if entity is None:
            raise BridgeError(f"{device.id} has no action '{action}'")
        domain = entity.partition(".")[0]
        await self._service(device, domain, "press" if domain == "button" else "turn_on", {"entity_id": entity})

    @classmethod
    async def discover(cls, config: dict) -> list[dict]:
        if not config.get("url"):
            return []
        try:
            token = load_token(config.get("name", cls.type))
        except BridgeError:
            return []
        async with httpx.AsyncClient(timeout=5, headers={"Authorization": f"Bearer {token}"}) as http:
            try:
                response = await http.get(f"{config['url'].rstrip('/')}/api/states")
                response.raise_for_status()
                found = []
                ids: set[str] = set()
                for raw in response.json():
                    domain, _, object_id = raw["entity_id"].partition(".")
                    if domain not in ("light", "switch", "fan", "climate"):
                        continue
                    attrs = raw.get("attributes", {})
                    caps = ["power"]
                    modes = set(attrs.get("supported_color_modes", []))
                    if domain == "light":
                        if modes - {"onoff"} or attrs.get("brightness") is not None:
                            caps.append("brightness")
                        if modes & {"hs", "xy", "rgb", "rgbw", "rgbww"} or attrs.get("rgb_color") is not None:
                            caps.append("color")
                    elif domain == "climate":
                        if attrs.get("supported_features", 0) & 1 or attrs.get("temperature") is not None:
                            caps.append("temperature")
                        if attrs.get("fan_modes"):
                            caps.append("fan")
                    slug = object_id.replace("_", "-")[:35]
                    device_id = slug
                    number = 2
                    while device_id in ids:
                        device_id = f"{slug}-{number}"
                        number += 1
                    ids.add(device_id)
                    device = {"id": device_id, "name": attrs.get("friendly_name", object_id), "kind": domain,
                              "entity": raw["entity_id"], "capabilities": caps}
                    if "fan" in caps:
                        device["fan_modes"] = attrs["fan_modes"]
                    found.append(device)
                return found
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise BridgeError("Home Assistant discovery failed; check its URL and long-lived token") from None
