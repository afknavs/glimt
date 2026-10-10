"""WLED strips over their local JSON API.

    [bridges.wled]
    poll_seconds = 10

    [devices.strip]
    name = "LED strip"
    kind = "light"
    bridge = "wled"
    host = "192.168.1.70"
    transition_ms = 100
    effects = { rainbow = 9 }
"""

from __future__ import annotations

import asyncio

import httpx

from ..model import Cap, Device, parse_color, to_hex
from .base import Bridge, BridgeError


class WledBridge(Bridge):
    type = "wled"

    def __init__(self, name: str, config: dict, hub) -> None:
        super().__init__(name, config, hub)
        self.http: httpx.AsyncClient | None = None
        self.poller: asyncio.Task | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=3)

    def devices(self) -> list[Device]:
        made = []
        for device_id, cfg in self.device_configs.items():
            if not cfg.get("host"):
                raise BridgeError(f'[devices.{device_id}] needs host = "192.168.1.70"')
            caps = {"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()}
            if cfg.get("effects"):
                caps["effect"] = Cap.enum(list(cfg["effects"]))
            made.append(self.make_device(device_id, cfg, capabilities=caps))
        return made

    async def start(self) -> None:
        self.http = self._client()
        self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        if self.http:
            await self.http.aclose()

    async def _read(self, device_id: str, cfg: dict) -> None:
        try:
            response = await self.http.get(f"http://{cfg['host']}/json/state")
            response.raise_for_status()
            raw = response.json()
            segment = raw["seg"][0]
            state = {"power": raw["on"], "brightness": round(raw["bri"] * 100 / 255),
                     "color": to_hex(tuple(segment["col"][0][:3]))}
            if cfg.get("effects"):
                state["effect"] = next((name for name, value in cfg["effects"].items()
                                        if value == segment.get("fx")), None)
            self.report(device_id, state, online=True, route="lan")
        except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError):
            self.report(device_id, online=False, error="No WLED reply; check its power, Wi-Fi and host address")

    async def _poll(self) -> None:
        while True:
            for device_id, cfg in self.device_configs.items():
                await self._read(device_id, cfg)
            await asyncio.sleep(self.config.get("poll_seconds", 10))

    async def refresh(self, device: Device) -> None:
        await self._read(device.id, device.config)

    async def apply(self, device: Device, changes: dict) -> None:
        body = {"tt": round(device.config.get("transition_ms", 100) / 100)}
        if "power" in changes:
            body["on"] = changes["power"]
        if "brightness" in changes:
            body["bri"] = round(changes["brightness"] * 255 / 100)
        segment = {}
        if "color" in changes:
            segment["col"] = [list(parse_color(changes["color"]))]
        if "effect" in changes:
            segment["fx"] = device.config["effects"][changes["effect"]]
        if segment:
            body["seg"] = [segment]
        try:
            (await self.http.post(f"http://{device.config['host']}/json/state", json=body)).raise_for_status()
        except httpx.HTTPError:
            self.report(device.id, online=False, error="WLED command failed; check its power and Wi-Fi")
            raise BridgeError(f"{device.name} did not accept the WLED command; check its power and Wi-Fi") from None

    @classmethod
    async def discover(cls, config: dict) -> list[dict]:
        try:
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            return []

        names: set[str] = set()

        def found(zeroconf, service_type: str, name: str, state_change) -> None:
            names.add(name)

        zeroconf = AsyncZeroconf()
        browser = AsyncServiceBrowser(zeroconf.zeroconf, "_wled._tcp.local.", handlers=[found])
        try:
            await asyncio.sleep(2)
            devices = []
            for name in sorted(names):
                info = AsyncServiceInfo("_wled._tcp.local.", name)
                if not await info.async_request(zeroconf.zeroconf, 1000):
                    continue
                addresses = info.parsed_scoped_addresses()
                if not addresses:
                    continue
                host = addresses[0]
                if ":" in host:
                    host = f"[{host}]"
                if info.port != 80:
                    host += f":{info.port}"
                devices.append({"id": f"strip-{len(devices) + 1}", "name": name.split("._wled.")[0],
                                "kind": "light", "host": host})
            return devices
        finally:
            await browser.async_cancel()
            await zeroconf.async_close()
