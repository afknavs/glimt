"""Cheap Bluetooth LED strips that speak the ELK-BLEDOM protocol.

These are the strips sold with apps such as Lotus Lamp X, duoCo Strip and LED Lamp;
they advertise names like ELK-BLEDOM, MELK-OA10, LEDBLE.

    [bridges.ble]
    type = "elk-ble"

    [devices.strip]
    name = "LED strip"
    kind = "light"
    bridge = "ble"
    address = "AA:BB:CC:DD:EE:FF"       # on macOS this is a UUID; `glimt setup elk-ble` finds it
    # ble_name = "MELK-OA10 A0"         # optional: refuse to talk to anything else
    # keepalive = true                  # hold the connection open (default)
    # effects = { "color-flow" = 133 }  # optional: name the effect numbers your strip has

Why it is fast: the usual approach scans, connects, sends, disconnects for every single
command (about three seconds). This bridge connects once and stays connected, so a colour
change is one small write on a link that is already up. The strip cannot report its
state, so state shown for it is "last sent".

One connection at a time is a Bluetooth limit of these strips: while Glimt holds it,
the phone app cannot connect. Set keepalive = false to release it between commands.
"""

from __future__ import annotations

import asyncio

from ..model import Cap, Device, parse_color
from .base import Bridge, BridgeError

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE = "0000fff3-0000-1000-8000-00805f9b34fb"
NAME_PREFIXES = ("ELK-", "MELK-", "LEDBLE", "ELK_", "MELK_")
DEFAULT_EFFECTS = {
    "jump-rgb": 0x87, "jump-all": 0x88, "fade-rgb": 0x89, "fade-all": 0x8A, "fade-red": 0x8B,
    "fade-green": 0x8C, "fade-blue": 0x8D, "blink-all": 0x95, "blink-red": 0x96, "blink-white": 0x9C,
}
IDLE_RELEASE = 20.0


def packets(changes: dict, effects: dict[str, int]) -> list[bytes]:
    """Translate common-model changes into ELK-BLEDOM frames, in a safe order."""
    if changes.get("power") is False:
        return [bytes.fromhex("7e0404000000ff00ef")]
    out = []
    if changes.get("power") is True:
        out.append(bytes.fromhex("7e0404f00001ff00ef"))
    if "color" in changes:
        red, green, blue = parse_color(changes["color"])
        out.append(bytes([0x7E, 0x00, 0x05, 0x03, red, green, blue, 0x00, 0xEF]))
    if changes.get("effect") is not None:
        out.append(bytes([0x7E, 0x07, 0x03, effects[changes["effect"]], 0xFF, 0xFF, 0xFF, 0x00, 0xEF]))
    if "speed" in changes:
        out.append(bytes([0x7E, 0x04, 0x02, changes["speed"], 0xFF, 0xFF, 0xFF, 0x00, 0xEF]))
    if "brightness" in changes:
        out.append(bytes([0x7E, 0x04, 0x01, changes["brightness"], 0x01, 0xFF, 0xFF, 0x00, 0xEF]))
    return out


class Strip:
    """One strip and its long-lived connection."""

    def __init__(self, bridge: "ElkBleBridge", device_id: str, cfg: dict) -> None:
        self.bridge, self.id, self.cfg = bridge, device_id, cfg
        self.effects = {**(cfg.get("effects") or DEFAULT_EFFECTS)}
        self.client = None
        self.lock = asyncio.Lock()
        self.lost = asyncio.Event()
        self.keeper: asyncio.Task | None = None
        self.release: asyncio.TimerHandle | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    async def connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        if self.connected:
            return
        address = self.cfg.get("address")
        if not address:
            raise BridgeError(f"[devices.{self.id}] needs address = \"...\"; run `glimt setup elk-ble` to find it")
        found = await BleakScanner.find_device_by_address(address, timeout=6)
        if found is None:
            raise BridgeError("Strip not found over Bluetooth. Is it powered, in range, and is its phone app closed?")
        wanted = self.cfg.get("ble_name")
        if wanted and " ".join((found.name or "").split()) != wanted:
            raise BridgeError(f"The device at this address is now called '{found.name}', not '{wanted}'; not sending")
        self.lost.clear()
        client = BleakClient(found, disconnected_callback=lambda _: self.lost.set(), timeout=10)
        try:
            await client.connect()
            service = client.services.get_service(SERVICE)
            if service is None or service.get_characteristic(WRITE) is None:
                raise BridgeError("This device does not expose the ELK-BLEDOM control service")
            if (found.name or "").upper().startswith("MELK") or self.cfg.get("variant") == "melk":
                # MELK controllers ignore commands until they receive this handshake.
                for frame in ("7e0783", "7e0404"):
                    await client.write_gatt_char(WRITE, bytes.fromhex(frame), response=False)
                    await asyncio.sleep(1)
        except BaseException:
            # A timeout or cancellation here would otherwise strand a live link that nothing
            # tracks, and these strips only accept one: nobody could connect until it expired.
            await asyncio.shield(self._close(client))
            raise
        self.client = client
        self.bridge.report(self.id, online=True, route="bluetooth")

    async def send(self, frames: list[bytes]) -> None:
        async with self.lock:
            for attempt in (1, 2):
                try:
                    await self.connect()
                    # Only the last frame waits for an acknowledgement: one round trip per
                    # command instead of one per frame, and still proof the strip heard us.
                    for index, frame in enumerate(frames):
                        await self.client.write_gatt_char(WRITE, frame, response=index == len(frames) - 1)
                    break
                except BridgeError:
                    raise
                except Exception as exc:  # link dropped mid-write: reconnect once and resend
                    await self.drop()
                    if attempt == 2:
                        raise BridgeError(f"Bluetooth write failed ({type(exc).__name__}: {exc})") from None
        if not self.cfg.get("keepalive", True):
            if self.release:
                self.release.cancel()
            self.release = asyncio.get_running_loop().call_later(
                IDLE_RELEASE, lambda: asyncio.ensure_future(self.drop()))

    async def drop(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await self._close(client)

    @staticmethod
    async def _close(client) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), 4)
        except Exception:
            pass

    async def keep(self) -> None:
        """Stay connected: reconnect with backoff whenever the link drops (sleep, power cut, range)."""
        backoff = 2.0
        while True:
            try:
                async with self.lock:
                    await self.connect()
                backoff = 2.0
                await self.lost.wait()
                self.client = None
                self.bridge.report(self.id, online=False, error="Bluetooth link dropped; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.bridge.report(self.id, online=False, error=str(exc) or type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


class ElkBleBridge(Bridge):
    type = "elk-ble"
    extra = "ble"

    def __init__(self, name, config, hub) -> None:
        super().__init__(name, config, hub)
        self.strips: dict[str, Strip] = {}

    def devices(self) -> list[Device]:
        made = []
        for device_id, cfg in self.device_configs.items():
            effects = cfg.get("effects") or DEFAULT_EFFECTS
            made.append(self.make_device(device_id, cfg, assumed=True, capabilities={
                "power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color(),
                "effect": Cap.enum(list(effects)), "speed": Cap.integer(1, 100),
            }))
        return made

    async def start(self) -> None:
        for device_id, cfg in self.device_configs.items():
            strip = self.strips[device_id] = Strip(self, device_id, cfg)
            if cfg.get("keepalive", True):
                strip.keeper = asyncio.create_task(strip.keep())

    async def stop(self) -> None:
        for strip in self.strips.values():
            if strip.keeper:
                strip.keeper.cancel()
            await strip.drop()

    async def apply(self, device: Device, changes: dict) -> None:
        strip = self.strips[device.id]
        await strip.send(packets(changes, strip.effects))
        # Colour and effect replace each other on the strip; keep state honest about that.
        if "color" in changes:
            self.report(device.id, {"effect": None})

    @classmethod
    async def discover(cls, config: dict) -> list[dict]:
        from bleak import BleakScanner

        found = []
        for device in await BleakScanner.discover(timeout=6):
            name = " ".join((device.name or "").split())
            if name.upper().startswith(NAME_PREFIXES):
                found.append({"id": "strip" if not found else f"strip-{len(found) + 1}", "name": "LED strip",
                              "kind": "light", "address": device.address, "ble_name": name})
        return found
