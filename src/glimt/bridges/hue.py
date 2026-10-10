"""Philips Hue lights over CLIP v2, with state pushed by the bridge.

    [bridges.hue]
    host = "192.168.1.71"

    [devices.desk]
    name = "Desk lamp"
    kind = "light"
    bridge = "hue"
    light_id = "v2-resource-id"
    capabilities = ["power", "brightness", "color"]
    transition_ms = 100

The app key lives in secrets/hue.json, written by `glimt setup hue`.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from .. import paths
from ..model import Cap, Device, parse_color, to_hex
from .base import Bridge, BridgeError

SPECS = {"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()}


def load_key(name: str) -> str:
    try:
        key = json.loads((paths.secrets_dir() / "hue.json").read_text())[name]["key"]
        if isinstance(key, str) and key.strip():
            return key
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise BridgeError("Hue is not linked yet. Run: glimt setup hue")


def rgb_to_xy(color: str) -> dict[str, float]:
    rgb = [channel / 255 for channel in parse_color(color)]
    r, g, b = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    total = x + y + z
    return {"x": x / total if total else 0.0, "y": y / total if total else 0.0}


def xy_to_rgb(xy: dict, brightness: float) -> str:
    x, y = xy["x"], xy["y"]
    if y <= 0 or brightness <= 0:
        return "#000000"
    luminance = brightness / 100
    x_value, z_value = luminance * x / y, luminance * (1 - x - y) / y
    rgb = [3.2404542 * x_value - 1.5371385 * luminance - 0.4985314 * z_value,
           -0.9692660 * x_value + 1.8760108 * luminance + 0.0415560 * z_value,
           0.0556434 * x_value - 0.2040259 * luminance + 1.0572252 * z_value]
    scale = max(1.0, *rgb)
    rgb = [max(0.0, value / scale) for value in rgb]
    rgb = [12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055 for value in rgb]
    return to_hex(tuple(round(min(1.0, value) * 255) for value in rgb))


class HueBridge(Bridge):
    type = "hue"

    def __init__(self, name: str, config: dict, hub) -> None:
        super().__init__(name, config, hub)
        self.http: httpx.AsyncClient | None = None
        self.listener: asyncio.Task | None = None
        self.lights: dict[str, dict] = {}

    def _client(self) -> httpx.AsyncClient:
        # Hue bridges have a self-signed certificate.
        return httpx.AsyncClient(verify=False, timeout=5)

    def devices(self) -> list[Device]:
        if not self.config.get("host"):
            raise BridgeError(f'[bridges.{self.name}] needs host = "192.168.1.71"')
        made = []
        for device_id, cfg in self.device_configs.items():
            if not cfg.get("light_id"):
                raise BridgeError(f'[devices.{device_id}] needs light_id = "..."')
            names = cfg.get("capabilities", ["power", "brightness"])
            if any(name not in SPECS for name in names):
                raise BridgeError(f"[devices.{device_id}] Hue capabilities are: {', '.join(SPECS)}")
            made.append(self.make_device(device_id, cfg, capabilities={name: SPECS[name] for name in names}))
        return made

    async def start(self) -> None:
        key = load_key(self.name)
        self.http = self._client()
        self.http.headers["hue-application-key"] = key
        self.listener = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self.listener:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
        if self.http:
            await self.http.aclose()

    def _report_light(self, raw: dict) -> None:
        light = self.lights.setdefault(raw["id"], {})
        for key, value in raw.items():
            light[key] = {**light.get(key, {}), **value} if isinstance(value, dict) else value
        for device_id, cfg in self.device_configs.items():
            if cfg["light_id"] != raw["id"]:
                continue
            state = {}
            caps = cfg.get("capabilities", ["power", "brightness"])
            if "power" in caps and "on" in light:
                state["power"] = light["on"]["on"]
            brightness = light.get("dimming", {}).get("brightness", 100)
            if "brightness" in caps and "dimming" in light:
                state["brightness"] = round(brightness)
            if "color" in caps and "xy" in light.get("color", {}):
                state["color"] = xy_to_rgb(light["color"]["xy"], brightness)
            self.report(device_id, state, online=True, route="hub")

    def _events(self, events: list[dict]) -> None:
        for event in events:
            for raw in event.get("data", []):
                if raw.get("type") == "light":
                    if event.get("type") == "delete":
                        for device_id, cfg in self.device_configs.items():
                            if cfg["light_id"] == raw["id"]:
                                self.report(device_id, online=False,
                                            error="Hue light was removed; check its light_id or pair it again")
                    else:
                        self._report_light(raw)
                elif raw.get("type") == "zigbee_connectivity" and "status" in raw:
                    for device_id, cfg in self.device_configs.items():
                        owner = self.lights.get(cfg["light_id"], {}).get("owner", {}).get("rid")
                        if owner and owner == raw.get("owner", {}).get("rid"):
                            online = raw.get("status") == "connected"
                            self.report(device_id, online=online,
                                        error=None if online else "Hue light is unreachable; check its power")

    async def _listen(self) -> None:
        backoff = 1.0
        url = f"https://{self.config['host']}"
        while True:
            try:
                response = await self.http.get(f"{url}/clip/v2/resource/light")
                response.raise_for_status()
                raw = response.json()
                if raw.get("errors"):
                    raise BridgeError("Hue rejected the state request")
                self.lights.clear()
                for light in raw["data"]:
                    self._report_light(light)
                for device_id, cfg in self.device_configs.items():
                    if cfg["light_id"] not in self.lights:
                        self.report(device_id, online=False, error="Hue light was not found; check its light_id")
                async with self.http.stream("GET", f"{url}/eventstream/clip/v2",
                                            headers={"Accept": "text/event-stream"},
                                            timeout=httpx.Timeout(5, read=None)) as stream:
                    stream.raise_for_status()
                    backoff = 1.0
                    data = []
                    async for line in stream.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            self._events(json.loads("\n".join(data)))
                            data = []
                raise BridgeError("Hue event stream closed")
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, BridgeError, ValueError, KeyError, TypeError):
                for device_id in self.device_configs:
                    self.report(device_id, online=False,
                                error="Hue connection lost; check bridge power, network and app key; reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def apply(self, device: Device, changes: dict) -> None:
        body = {"dynamics": {"duration": device.config.get("transition_ms", 100)}}
        if "power" in changes:
            body["on"] = {"on": changes["power"]}
        if "brightness" in changes:
            body["dimming"] = {"brightness": changes["brightness"]}
        if "color" in changes:
            body["color"] = {"xy": rgb_to_xy(changes["color"])}
        try:
            response = await self.http.put(
                f"https://{self.config['host']}/clip/v2/resource/light/{device.config['light_id']}", json=body)
            response.raise_for_status()
            if response.json().get("errors"):
                raise BridgeError("Hue rejected the command; check the light id and app key")
        except (httpx.HTTPError, ValueError):
            self.report(device.id, online=False, error="Hue command failed; check bridge power and network")
            raise BridgeError(f"{device.name} did not accept the Hue command; check bridge power and network") from None

    @classmethod
    async def pair(cls, host: str) -> str:
        # Pairing also uses the bridge's self-signed certificate.
        async with httpx.AsyncClient(verify=False, timeout=5) as http:
            try:
                response = await http.post(f"https://{host}/api",
                                           json={"devicetype": "glimt#computer", "generateclientkey": True})
                response.raise_for_status()
                for item in response.json():
                    if item.get("error", {}).get("type") == 101:
                        raise BridgeError("Press the round button on the Hue bridge, then try again")
                    if item.get("success", {}).get("username"):
                        return item["success"]["username"]
            except (httpx.HTTPError, ValueError, TypeError):
                raise BridgeError("Hue pairing failed; check the bridge host and network") from None
        raise BridgeError("Hue pairing failed; press the link button and try again")

    @classmethod
    async def discover(cls, config: dict) -> list[dict]:
        if not config.get("host"):
            return []
        try:
            key = load_key(config.get("name", cls.type))
        except BridgeError:
            return []
        # Discovery uses the same self-signed bridge certificate.
        async with httpx.AsyncClient(verify=False, timeout=5, headers={"hue-application-key": key}) as http:
            try:
                response = await http.get(f"https://{config['host']}/clip/v2/resource/light")
                response.raise_for_status()
                raw = response.json()
                if raw.get("errors"):
                    raise BridgeError("Hue discovery failed; check the app key")
                found = []
                ids: set[str] = set()
                for light in raw["data"]:
                    name = light.get("metadata", {}).get("name", "Hue light")
                    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:35] or "light"
                    device_id = slug
                    number = 2
                    while device_id in ids:
                        device_id = f"{slug}-{number}"
                        number += 1
                    ids.add(device_id)
                    caps = [cap for cap, field in (("power", "on"), ("brightness", "dimming"), ("color", "color"))
                            if field in light]
                    found.append({"id": device_id, "name": name, "kind": "light", "light_id": light["id"],
                                  "capabilities": caps})
                return found
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise BridgeError("Hue discovery failed; check bridge power, host and app key") from None
