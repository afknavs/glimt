"""Tuya / Smart Life devices: local network first, cloud when it must.

    [bridges.tuya]
    # cloud = false        # optional: never use the cloud (LAN-only)

    [devices.desk]
    name = "Desk light"
    kind = "light"
    bridge = "tuya"
    tuya_id = "bf1234..."
    map = { power = "switch_1" }                  # capability = Tuya status code
    # read = { watts = "cur_power" }              # read-only values to show in state
    # scale = { watts = 0.1 }                     # multiply raw values
    # values = { fan = { medium = "middle" } }    # rename enum options
    # route = "auto"                              # auto | lan | cloud
    # [devices.desk.actions]                      # Tuya tap-to-run scenes as actions
    # faster = "Increase fan speed"

Why it is fast: each LAN device gets one long-lived encrypted socket, kept warm with
heartbeats. A command is a single packet on a connection that is already open, and the
device pushes state changes back on the same socket, so state is always current and
never polled. Credentials (local keys, cloud tokens) live in
~/.glimt/secrets/tuya.json, written by `glimt setup tuya`.
"""

from __future__ import annotations

import asyncio
import colorsys
import concurrent.futures
import ipaddress
import json
import logging
import os
import select
import socket
import threading
import time
from collections import deque

from .. import paths
from ..model import Cap, Device, parse_color, to_hex
from .base import Bridge, BridgeError

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"  # Tuya's public Home Assistant sharing client; not a secret
SECRETS = "tuya.json"
HEARTBEAT = 9.0
RECONCILE = 90.0
KEY_ERRORS = ("914",)  # wrong key or protocol version: retrying will not help


def load_secrets() -> dict:
    path = paths.secrets_dir() / SECRETS
    if not path.exists():
        raise BridgeError("Tuya is not linked yet. Run: glimt setup tuya")
    return json.loads(path.read_text())


def save_secrets(data: dict) -> None:
    paths.write_private(paths.secrets_dir() / SECRETS, json.dumps(data, indent=2))


def _ensure_ca_bundle() -> None:
    """python.org builds of Python on macOS ship without a CA bundle unless the user ran
    "Install Certificates". Tuya's push feed uses the default TLS context, so point it
    at certifi's bundle when the system one is missing."""
    import ssl

    if os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile:
        return
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


class LinkDown(Exception):
    pass


class LanLink(threading.Thread):
    """One persistent, heartbeated socket to one Tuya device."""

    def __init__(self, tuya_id: str, info: dict, on_dps, on_state, find_ip) -> None:
        super().__init__(name=f"tuya-{tuya_id[-6:]}", daemon=True)
        self.tuya_id, self.info = tuya_id, info
        self.on_dps, self.on_state, self.find_ip = on_dps, on_state, find_ip
        self.up = False
        self.fatal: str | None = None
        self._pending: deque = deque()
        self._wake_r, self._wake_w = os.pipe()
        self._stop = threading.Event()

    def send(self, dps: dict) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        if not self.up:
            future.set_exception(LinkDown("not connected"))
            return future
        self._pending.append((dps, future))
        os.write(self._wake_w, b"x")
        return future

    def stop(self) -> None:
        self._stop.set()
        os.write(self._wake_w, b"x")

    def _fail_pending(self, reason: str) -> None:
        while self._pending:
            _, future = self._pending.popleft()
            if not future.done():
                future.set_exception(LinkDown(reason))

    def run(self) -> None:
        import tinytuya

        backoff, failures = 1.0, 0
        while not self._stop.is_set():
            device = tinytuya.Device(self.tuya_id, self.info["ip"], self.info["local_key"],
                                     version=float(self.info["version"]), connection_timeout=3,
                                     connection_retry_limit=1, connection_retry_delay=0, persist=True)
            reason = "disconnected"
            try:
                first = device.status()
                if not isinstance(first, dict) or "dps" not in first:
                    code = str(first.get("Err")) if isinstance(first, dict) else "?"
                    if code in KEY_ERRORS:
                        self.fatal = "The device rejected its local key or protocol version"
                    raise LinkDown(f"local error {code}")
                device.set_retry(False)  # null acks return at once; state arrives as a push
                self.up, backoff, failures = True, 1.0, 0
                self.on_dps(self.tuya_id, first["dps"])
                self.on_state(self.tuya_id, True, None)
                self._serve(device)
            except LinkDown as exc:
                reason = str(exc)
            except Exception as exc:  # tinytuya can raise on malformed frames; reconnect
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self.up = False
                try:
                    device.close()
                except Exception:
                    pass
            self._fail_pending(reason)
            if self._stop.is_set():
                break
            failures += 1
            self.on_state(self.tuya_id, False, self.fatal or reason)
            if self.fatal:
                return
            if failures >= 2:  # the address may have changed (DHCP); look for the device again
                found = self.find_ip(self.tuya_id, self.info)
                if found and found != self.info["ip"]:
                    self.info["ip"], failures = found, 0
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _serve(self, device) -> None:
        last_beat = last_full = time.monotonic()
        while not self._stop.is_set():
            sock = device.socket
            if sock is None:
                raise LinkDown("socket closed")
            wait = max(0.0, HEARTBEAT - (time.monotonic() - last_beat))
            readable, _, _ = select.select([sock, self._wake_r], [], [], wait)
            if self._wake_r in readable:
                os.read(self._wake_r, 4096)
                while self._pending:
                    dps, future = self._pending.popleft()
                    reply = device.set_multiple_values(dps)
                    if isinstance(reply, dict) and ("Err" in reply or "Error" in reply):
                        future.set_exception(LinkDown(f"local error {reply.get('Err')}"))
                        raise LinkDown(f"write failed ({reply.get('Err')})")
                    future.set_result(None)
                    self._absorb(reply)
            if sock in readable:
                reply = device.receive()
                if isinstance(reply, dict) and ("Err" in reply or "Error" in reply):
                    raise LinkDown(f"local error {reply.get('Err')}")
                self._absorb(reply)
            now = time.monotonic()
            if now - last_full >= RECONCILE:  # belt and braces: catch any change we never saw
                device.status(nowait=True)
                last_full = last_beat = now
            elif now - last_beat >= HEARTBEAT:
                device.heartbeat(nowait=True)
                last_beat = now

    def _absorb(self, reply) -> None:
        if not isinstance(reply, dict):
            return
        dps = reply.get("dps") or (reply.get("data") or {}).get("dps")
        if isinstance(dps, dict) and dps:
            self.on_dps(self.tuya_id, dps)


class Cloud:
    """Smart Life cloud: fallback route, infrared scenes, and live push for cloud-only devices."""

    def __init__(self, secrets: dict, on_status, on_online) -> None:
        self.secrets, self.on_status, self.on_online = secrets, on_status, on_online
        self.lock = threading.RLock()
        self.manager = None

    def connect(self) -> None:
        from tuya_sharing import Manager, SharingTokenListener

        outer = self

        class Tokens(SharingTokenListener):
            def update_token(self, token_info):
                with outer.lock:
                    outer.secrets["auth"]["token_info"] = token_info
                    save_secrets(outer.secrets)

        auth = self.secrets["auth"]
        self.manager = Manager(CLIENT_ID, auth["user_code"], auth["terminal_id"], auth["endpoint"],
                               auth["token_info"], Tokens())

    def fetch(self, ids: list[str]) -> None:
        with self.lock:
            reply = self.manager.customer_api.get("/v1.0/m/life/ha/devices/detail", {"devIds": ",".join(ids)})
        if not reply.get("success"):
            raise BridgeError("Tuya cloud refused the request. Sign in again with: glimt setup tuya")
        for item in reply.get("result") or []:
            self.on_online(item["id"], bool(item.get("online")))
            self.on_status(item["id"], {entry["code"]: entry["value"] for entry in item.get("status", [])})

    def send(self, tuya_id: str, commands: list[dict]) -> None:
        with self.lock:
            reply = self.manager.customer_api.post(f"/v1.1/m/thing/{tuya_id}/commands", None, {"commands": commands})
        if not reply.get("success") or reply.get("result") is False:
            raise BridgeError(f"Tuya cloud did not accept the command ({reply.get('msg') or 'no reason given'})")

    def scenes(self) -> list[dict]:
        with self.lock:
            return [{"id": s.scene_id, "home_id": str(s.home_id), "name": s.name}
                    for s in self.manager.query_scenes()]

    def trigger(self, home_id: str, scene_id: str) -> None:
        with self.lock:
            self.manager.scene_repository.trigger_scene(home_id, scene_id)

    def start_push(self) -> None:
        """Subscribe to Tuya's MQTT feed so cloud-routed devices update the moment they change."""
        from tuya_sharing import SharingDeviceListener

        outer = self

        class Listener(SharingDeviceListener):
            def update_device(self, device, updated_status_properties=None, dp_timestamps=None):
                outer.on_online(device.id, bool(device.online))
                outer.on_status(device.id, dict(device.status))

            def add_device(self, device):
                pass

            def remove_device(self, device_id):
                pass

        _ensure_ca_bundle()
        with self.lock:
            self.manager.update_device_cache()
            self.manager.add_device_listener(Listener())
            self.manager.refresh_mq()

    def stop(self) -> None:
        mq = getattr(self.manager, "mq", None)
        if mq is not None:
            try:
                mq.stop()
            except Exception:
                pass


class TuyaBridge(Bridge):
    type = "tuya"
    extra = "tuya"

    def __init__(self, name, config, hub) -> None:
        super().__init__(name, config, hub)
        self.secrets = load_secrets()
        self.records: dict[str, dict] = self.secrets.get("devices", {})
        self.links: dict[str, LanLink] = {}
        self.cloud: Cloud | None = None
        self.members: dict[str, list[Device]] = {}  # tuya_id -> our devices (an outlet strip has several)
        self.scene_only: list[str] = []  # devices that are nothing but Smart Life scenes
        self._writer = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tuya-cloud")
        self._seen: dict[str, str] = {}  # tuya_id -> ip, from discovery broadcasts
        self._listener: threading.Thread | None = None
        self._stopping = threading.Event()
        self._swept: dict[str, float] = {}
        self._poller: asyncio.Task | None = None
        logging.getLogger("tuya_sharing").setLevel(logging.CRITICAL)  # it logs every reconnect attempt

    # ---- model --------------------------------------------------------------------

    def devices(self) -> list[Device]:
        made = []
        for device_id, cfg in self.device_configs.items():
            tuya_id = cfg.get("tuya_id")
            if tuya_id is None and not cfg.get("map") and not cfg.get("read"):
                # Scenes only (an infrared fan, say): nothing to connect to, just scenes to run.
                device = self.make_device(device_id, cfg, actions=list(cfg.get("actions", {})), assumed=True)
                device.route = "cloud"
                self.scene_only.append(device_id)
                made.append(device)
                continue
            record = self.records.get(tuya_id)
            if record is None:
                raise BridgeError(f"[devices.{device_id}] tuya_id '{tuya_id}' is not a linked device. "
                                  "Run `glimt setup tuya` to refresh the list.")
            capabilities = {cap: self._spec(cfg, record, cap, code) for cap, code in cfg.get("map", {}).items()}
            lan_capable = bool(record.get("local_key") and record.get("ip")) and cfg.get("route") != "cloud"
            device = self.make_device(device_id, cfg, capabilities=capabilities,
                                      actions=list(cfg.get("actions", {})),
                                      assumed=not capabilities and not cfg.get("read"))
            device.route = "lan" if lan_capable else "cloud"
            self.members.setdefault(tuya_id, []).append(device)
            made.append(device)
        return made

    def _spec(self, cfg: dict, record: dict, cap: str, code: str) -> dict:
        info = record.get("codes", {}).get(code, {})
        kind = (info.get("type") or "Boolean").lower()
        if cap == "color":
            return Cap.color()
        if kind == "boolean":
            return Cap.bool()
        if kind == "enum":
            renames = {raw: ours for ours, raw in cfg.get("values", {}).get(cap, {}).items()}
            return Cap.enum([renames.get(option, option) for option in info.get("range", [])])
        if kind == "integer":
            if cap == "brightness":
                return Cap.percent()
            factor = 10 ** -int(info.get("scale", 0))
            return Cap.integer(int(info.get("min", 0) * factor), int(info.get("max", 100) * factor), info.get("unit"))
        raise BridgeError(f"[devices.{cfg['name']}] cannot map '{cap}' to Tuya code '{code}' of type {kind}")

    # ---- value translation --------------------------------------------------------

    def _to_tuya(self, device: Device, cap: str, value):
        cfg, code = device.config, device.config["map"][cap]
        info = self.records[cfg["tuya_id"]].get("codes", {}).get(code, {})
        if cap == "color":
            hue, sat, val = colorsys.rgb_to_hsv(*(channel / 255 for channel in parse_color(value)))
            return json.dumps({"h": round(hue * 360), "s": round(sat * 1000), "v": round(val * 1000)})
        if device.capabilities[cap]["type"] == "enum":
            return cfg.get("values", {}).get(cap, {}).get(value, value)
        if cap == "brightness":
            low, high = info.get("min", 10), info.get("max", 1000)
            return round(low + (high - low) * value / 100)
        if device.capabilities[cap]["type"] == "int":
            return value * 10 ** int(info.get("scale", 0))
        return value

    def _from_tuya(self, device: Device, name: str, code: str, raw):
        cfg = device.config
        info = self.records[cfg["tuya_id"]].get("codes", {}).get(code, {})
        if name == "color":
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                rgb = colorsys.hsv_to_rgb(data["h"] / 360, data["s"] / 1000, data["v"] / 1000)
                return to_hex(tuple(round(channel * 255) for channel in rgb))
            except (ValueError, KeyError, TypeError):
                return None
        if name in cfg.get("scale", {}):
            return round(raw * cfg["scale"][name], 2)
        spec = device.capabilities.get(name)
        if spec and spec["type"] == "enum":
            renames = {tuya: ours for ours, tuya in cfg.get("values", {}).get(name, {}).items()}
            return renames.get(raw, raw)
        if name == "brightness" and isinstance(raw, (int, float)):
            low, high = info.get("min", 10), info.get("max", 1000)
            return max(0, min(100, round((raw - low) * 100 / max(1, high - low))))
        if spec and spec["type"] == "int" and isinstance(raw, (int, float)):
            return int(raw * 10 ** -int(info.get("scale", 0)))
        if isinstance(raw, str):  # renamed read-only enums, e.g. an AC's mode
            return {tuya: ours for ours, tuya in cfg.get("values", {}).get(name, {}).items()}.get(raw, raw)
        return raw

    def _status(self, tuya_id: str, status: dict) -> None:
        """Cloud-shaped status ({code: value}) arrived; update our devices."""
        for device in self.members.get(tuya_id, []):
            wanted = {**device.config.get("map", {}), **device.config.get("read", {})}
            state = {}
            for name, code in wanted.items():
                if code in status:
                    value = self._from_tuya(device, name, code, status[code])
                    if value is not None:
                        state[name] = value
            if state:
                self.report(device.id, state)

    def _dps(self, tuya_id: str, dps: dict) -> None:
        """LAN-shaped state ({dp_id: value}) arrived; translate dp ids to codes."""
        by_dp = {str(info["dp"]): code for code, info in self.records[tuya_id].get("codes", {}).items()
                 if info.get("dp") is not None}
        self._status(tuya_id, {by_dp[str(dp)]: value for dp, value in dps.items() if str(dp) in by_dp})

    def _link_state(self, tuya_id: str, up: bool, issue: str | None) -> None:
        for device in self.members.get(tuya_id, []):
            if up:
                self.report(device.id, online=True, route="lan")
            elif self.cloud is not None:
                # The cloud still reaches it; say which route is in use.
                self.report(device.id, route="cloud")
                self.log.info("%s: local route down (%s); using cloud", device.id, issue)
            else:
                self.report(device.id, online=False, error=f"Local connection failed ({issue})")

    def _online(self, tuya_id: str, online: bool) -> None:
        link = self.links.get(tuya_id)
        if link and link.up:
            return  # the open socket is better evidence than the cloud's opinion
        for device in self.members.get(tuya_id, []):
            self.report(device.id, online=online, route="cloud",
                        error=None if online else "Offline in Smart Life")

    # ---- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        self._listener = threading.Thread(target=self._listen, name="tuya-discovery", daemon=True)
        self._listener.start()
        for tuya_id, devices in self.members.items():
            record = self.records[tuya_id]
            routes = {device.config.get("route", "auto") for device in devices}
            if routes == {"cloud"} or not (record.get("local_key") and record.get("ip")):
                continue
            link = LanLink(tuya_id, record, self._dps, self._link_state, self._find_ip)
            self.links[tuya_id] = link
            link.start()
        if self.config.get("cloud", True) and self.secrets.get("auth"):
            await asyncio.to_thread(self._start_cloud)
            self._poller = asyncio.create_task(self._poll())
        for device_id in self.scene_only:
            self.report(device_id, online=self.cloud is not None, route="cloud",
                        error=None if self.cloud else "Smart Life scenes need the Tuya cloud connection")

    async def _poll(self) -> None:
        """Safety net under the push feed: re-read cloud-routed devices once a minute."""
        while True:
            await asyncio.sleep(60)
            await self._fetch_cloud_routed()

    async def _fetch_cloud_routed(self, only: str | None = None) -> None:
        ids = [tuya_id for tuya_id, devices in self.members.items()
               if not (tuya_id in self.links and self.links[tuya_id].up)
               and any(device.capabilities or device.config.get("read") for device in devices)
               and only in (None, tuya_id)]
        if ids and self.cloud:
            try:
                await asyncio.to_thread(self.cloud.fetch, ids)
            except Exception as exc:
                self.log.debug("cloud refresh failed: %s", exc)

    async def _confirm(self, tuya_id: str) -> None:
        await asyncio.sleep(1.5)
        await self._fetch_cloud_routed(tuya_id)

    def _start_cloud(self) -> None:
        cloud = Cloud(self.secrets, self._status_unless_local, self._online)
        cloud.connect()
        cloud.fetch(list(self.members))
        self.cloud = cloud
        try:
            cloud.start_push()
        except Exception as exc:  # push is a nicety; commands and polling still work
            self.log.warning("cloud push unavailable (%s); cloud-only devices will refresh on demand", exc)

    def _status_unless_local(self, tuya_id: str, status: dict) -> None:
        link = self.links.get(tuya_id)
        if not (link and link.up):
            self._status(tuya_id, status)

    async def stop(self) -> None:
        self._stopping.set()
        if self._poller:
            self._poller.cancel()
        for link in self.links.values():
            link.stop()
        self._writer.shutdown(wait=False, cancel_futures=True)
        if self.cloud:
            await asyncio.to_thread(self.cloud.stop)

    async def refresh(self, device: Device) -> None:
        if self.cloud and device.config.get("tuya_id"):
            await asyncio.to_thread(self.cloud.fetch, [device.config["tuya_id"]])

    # ---- commands -----------------------------------------------------------------

    async def apply(self, device: Device, changes: dict) -> None:
        cfg = device.config
        tuya_id = cfg["tuya_id"]
        values = {cfg["map"][cap]: self._to_tuya(device, cap, value) for cap, value in changes.items()}
        if "color" in changes and "work_mode" in self.records[tuya_id].get("codes", {}):
            values["work_mode"] = "colour"
        route = cfg.get("route", "auto")
        link = self.links.get(tuya_id)
        if route != "cloud" and link and link.up:
            codes = self.records[tuya_id]["codes"]
            try:
                await asyncio.wrap_future(link.send({str(codes[code]["dp"]): value for code, value in values.items()}))
                return
            except LinkDown as exc:
                if route == "lan" or not self.cloud:
                    raise BridgeError(f"{device.name} did not accept the command locally ({exc})") from None
                # Absolute settings are safe to repeat, so fall through to the cloud.
        if route == "lan" or not self.cloud:
            reason = (link.fatal if link else None) or "it is not reachable on your network"
            raise BridgeError(f"{device.name}: {reason}")
        commands = [{"code": code, "value": value} for code, value in values.items()]
        await self._in_order(self.cloud.send, tuya_id, commands)
        asyncio.create_task(self._confirm(tuya_id))  # the cloud only says "accepted"; check what it did

    def _in_order(self, call, *args):
        """Run a cloud command on the single writer thread. Commands leave strictly in the order
        they were issued, even if the caller that issued one has since timed out or been
        cancelled; otherwise a delayed "on" could land after the "off" that followed it."""
        return asyncio.get_running_loop().run_in_executor(self._writer, call, *args)

    async def act(self, device: Device, action: str, args: dict):
        if not self.cloud:
            raise BridgeError("Tuya scenes need the cloud connection")
        spec = device.config["actions"][action]
        wanted = spec if isinstance(spec, str) else spec.get("scene")
        for key, needed in ({} if isinstance(spec, str) else spec.get("requires", {})).items():
            if device.state.get(key) != needed:
                raise BridgeError(f"{action} needs {key} to be {needed} (it is {device.state.get(key)})")
        scene = self._scene(wanted)
        await self._in_order(self.cloud.trigger, scene["home_id"], scene["id"])
        return None

    def _scene(self, wanted: str) -> dict:
        scenes = self.secrets.get("scenes", [])
        matches = [s for s in scenes if s["id"] == wanted] or \
                  [s for s in scenes if s["name"].strip().lower() == wanted.strip().lower()]
        if len(matches) != 1:
            raise BridgeError(f"Expected exactly one Smart Life scene named '{wanted}', found {len(matches)}. "
                              "Run `glimt setup tuya` to refresh scenes.")
        return matches[0]

    # ---- finding devices whose address changed --------------------------------------

    def _listen(self) -> None:
        """Tuya devices announce themselves by UDP broadcast; remember where each one is."""
        try:
            import tinytuya
        except ImportError:
            return
        sockets = []
        for port in (6666, 6667):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.bind(("", port))
                sockets.append(sock)
            except OSError:
                pass
        while sockets and not self._stopping.is_set():
            readable, _, _ = select.select(sockets, [], [], 2.0)
            for sock in readable:
                try:
                    data, address = sock.recvfrom(4096)
                    try:
                        text = tinytuya.decrypt_udp(data)
                    except Exception:
                        text = data[20:-8].decode(errors="ignore")
                    info = json.loads(text)
                    if info.get("gwId") in self.records:
                        self._seen[info["gwId"]] = info.get("ip") or address[0]
                except Exception:
                    continue
        for sock in sockets:
            sock.close()

    def _find_ip(self, tuya_id: str, info: dict) -> str | None:
        """Called by a link that cannot connect: has the device moved to another address?"""
        found = self._seen.get(tuya_id)
        if (not found or found == info["ip"]) and time.time() - self._swept.get(tuya_id, 0) > 300:
            self._swept[tuya_id] = time.time()
            found = self._sweep(tuya_id, info)
        if found and found != info["ip"]:
            self.log.info("%s moved from %s to %s", tuya_id[-6:], info["ip"], found)
            self.records[tuya_id]["ip"] = found
            try:
                save_secrets(self.secrets)
            except OSError:
                pass
            return found
        return None

    def _sweep(self, tuya_id: str, info: dict) -> str | None:
        """Last resort: try the device's key against every host on its subnet with port 6668 open."""
        import tinytuya

        try:
            network = ipaddress.ip_network(info["ip"] + "/24", strict=False)
        except ValueError:
            return None
        taken = {record.get("ip") for other, record in self.records.items()
                 if other != tuya_id and other in self.links and self.links[other].up}

        def is_open(address: str) -> str | None:
            try:
                with socket.create_connection((address, 6668), timeout=0.5):
                    return address
            except OSError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            candidates = [a for a in pool.map(is_open, (str(host) for host in network.hosts())) if a and a not in taken]
        for address in candidates:
            probe = tinytuya.Device(tuya_id, address, info["local_key"], version=float(info["version"]),
                                    connection_timeout=2, connection_retry_limit=1, connection_retry_delay=0)
            try:
                reply = probe.status()
            except Exception:
                reply = None
            finally:
                probe.close()
            if isinstance(reply, dict) and "dps" in reply:
                return address
        return None
