"""Link a Smart Life / Tuya account and import its devices.

Uses Tuya's QR sign-in (the same flow Home Assistant's Tuya integrations use): you scan
a code with the Smart Life app, no password is typed anywhere and no developer account
is needed. It yields the per-device local keys that make LAN control possible.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import socket
import time

from ..bridges.tuya import CLIENT_ID, load_secrets, save_secrets

LIGHT_CATEGORIES = {"dj", "dd", "fwd", "xdd", "dc", "tgq", "tyndj", "gyd"}
VERSIONS = (3.4, 3.3, 3.5)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:32] or "device"


# ---- cloud: sign in and read the device list ------------------------------------------


def qr_login(user_code: str, show, wait_seconds: int = 180) -> dict:
    """Run the QR sign-in. ``show(text)`` displays the code. Returns the auth dict."""
    from tuya_sharing import LoginControl

    login = LoginControl()
    reply = login.qr_code(CLIENT_ID, "haauthorize", user_code)
    if not reply.get("success"):
        raise RuntimeError("Tuya rejected that User Code. In Smart Life: Me › Settings › Account and Security › User Code.")
    code = reply["result"]["qrcode"]
    show("tuyaSmart--qrLogin?token=" + code)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        ok, info = login.login_result(code, CLIENT_ID, user_code)
        if ok:
            return {"user_code": user_code, "terminal_id": info["terminal_id"], "endpoint": info["endpoint"],
                    "token_info": {key: info[key] for key in ("t", "uid", "expire_time", "access_token", "refresh_token")}}
        time.sleep(2)
    raise RuntimeError("Timed out waiting for approval in Smart Life.")


def record_from(raw: dict) -> dict:
    """Reduce Tuya's device description to what the bridge needs."""
    codes: dict[str, dict] = {}
    for dp, item in (raw.get("local_strategy") or {}).items():
        code = item.get("status_code")
        if code:
            codes.setdefault(code, {})["dp"] = str(dp)
    for source, writable in ((raw.get("status_range") or {}, False), (raw.get("function") or {}, True)):
        for code, spec in source.items():
            spec = spec if isinstance(spec, dict) else vars(spec)
            entry = codes.setdefault(code, {})
            entry["type"] = spec.get("type")
            entry["writable"] = entry.get("writable", False) or writable
            try:
                values = json.loads(spec.get("values") or "{}")
            except (TypeError, ValueError):
                values = {}
            if isinstance(values, dict):
                entry.update({key: values[key] for key in ("min", "max", "scale", "unit", "range") if key in values})
    for code in (raw.get("status") or {}):
        codes.setdefault(code, {})
    record = {"name": (raw.get("name") or "").strip(), "category": raw.get("category"),
              "product": (raw.get("product_name") or "").strip(), "codes": codes}
    if raw.get("local_key") and not raw.get("sub"):
        record["local_key"] = raw["local_key"]
    return record


def import_account(auth: dict) -> tuple[dict, list]:
    """Fetch devices and tap-to-run scenes for a signed-in account."""
    from tuya_sharing import Manager

    manager = Manager(CLIENT_ID, auth["user_code"], auth["terminal_id"], auth["endpoint"], auth["token_info"])
    manager.update_device_cache()
    records = {device.id: record_from(vars(device)) for device in manager.device_map.values()}
    scenes = [{"id": s.scene_id, "home_id": str(s.home_id), "name": s.name} for s in manager.query_scenes()]
    return records, scenes


# ---- LAN: where is each device, and which protocol version does it speak ----------------


def local_subnet() -> ipaddress.IPv4Network | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing interface
        return ipaddress.ip_network(probe.getsockname()[0] + "/24", strict=False)
    except OSError:
        return None
    finally:
        probe.close()


def _open(address: str) -> str | None:
    try:
        with socket.create_connection((address, 6668), timeout=0.6):
            return address
    except OSError:
        return None


def locate(records: dict, progress=lambda text: None) -> None:
    """Fill in ip and version for every device that has a local key, by trying each key
    against the hosts on this network that have Tuya's port open."""
    import tinytuya

    subnet = local_subnet()
    wanted = {tuya_id: record for tuya_id, record in records.items() if record.get("local_key")}
    if subnet is None or not wanted:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        hosts = [address for address in pool.map(_open, (str(host) for host in subnet.hosts())) if address]
    progress(f"{len(hosts)} host(s) on {subnet} have Tuya's local port open")

    def attempt(tuya_id: str, record: dict, address: str):
        for version in VERSIONS:
            device = tinytuya.Device(tuya_id, address, record["local_key"], version=version, connection_timeout=2,
                                     connection_retry_limit=1, connection_retry_delay=0)
            try:
                reply = device.status()
            except Exception:
                reply = None
            finally:
                device.close()
            if isinstance(reply, dict) and "dps" in reply:
                return version
        return None

    remaining = list(hosts)
    for tuya_id, record in wanted.items():
        ordered = ([record["ip"]] if record.get("ip") in remaining else []) + [a for a in remaining if a != record.get("ip")]
        for address in ordered:
            version = attempt(tuya_id, record, address)
            if version:
                record["ip"], record["version"] = address, str(version)
                remaining.remove(address)
                progress(f"  {record['name']}: {address} (protocol {version})")
                break
        else:
            record.pop("ip", None)
            progress(f"  {record['name']}: not reachable locally; it will use the cloud")


# ---- turn records into [devices.*] suggestions ------------------------------------------


def suggest(records: dict, taken: set[str] | None = None) -> tuple[dict, list[str]]:
    """Propose [devices.*] tables for imported devices. Returns (devices, notes)."""
    taken = set(taken or ())
    devices: dict[str, dict] = {}
    notes: list[str] = []

    def add(base: str, table: dict) -> None:
        device_id, n = base, 2
        while device_id in taken:
            device_id, n = f"{base}-{n}", n + 1
        taken.add(device_id)
        devices[device_id] = table

    for tuya_id, record in records.items():
        codes, name = record.get("codes", {}), record["name"] or record.get("product") or "Device"
        base = {"bridge": "tuya", "tuya_id": tuya_id}
        writable = {code for code, info in codes.items() if info.get("writable")}
        switches = sorted(code for code in writable if re.fullmatch(r"switch(_\d+)?", code))
        if record.get("category") in LIGHT_CATEGORIES or "switch_led" in writable:
            mapping = {"power": "switch_led" if "switch_led" in writable else (switches or ["switch"])[0]}
            for cap, options in (("brightness", ("bright_value_v2", "bright_value")),
                                 ("color", ("colour_data_v2", "colour_data"))):
                chosen = next((code for code in options if code in writable), None)
                if chosen:
                    mapping[cap] = chosen
            add(slug(name), {**base, "name": name, "kind": "light", "map": mapping})
        elif "temp_set" in writable:
            mapping = {"power": "switch", "temperature": "temp_set"}
            table = {**base, "name": name, "kind": "climate", "map": mapping}
            if codes.get("fan_speed_enum", {}).get("range"):
                mapping["fan"] = "fan_speed_enum"
                if "middle" in codes["fan_speed_enum"]["range"]:
                    table["values"] = {"fan": {"medium": "middle"}}
            for cap, code in (("eco", "mode_eco"), ("sleep", "sleep")):
                if code in writable:
                    mapping[cap] = code
            read = {ours: code for ours, code in (("current_temperature", "temp_current"), ("mode", "mode")) if code in codes}
            if read:
                table["read"] = read
            add(slug(name), table)
        elif len(switches) > 1:
            for index, code in enumerate(switches, 1):
                add(f"{slug(name)}-{index}", {**base, "name": f"{name} {index}", "kind": "outlet", "map": {"power": code}})
        elif switches:
            add(slug(name), {**base, "name": name, "kind": "switch", "map": {"power": switches[0]}})
        else:
            notes.append(f"{name}: no simple on/off control found (infrared remotes and sensors are like this). "
                         "To control it, make Tap-to-Run scenes in Smart Life and attach them as actions; "
                         "see docs/setup/tuya.md.")
    return devices, notes


def run(config: dict, ask, say) -> None:
    """Interactive linking. ``ask(prompt) -> str`` and ``say(text)`` come from the wizard."""
    try:
        secrets = load_secrets()
    except Exception:
        secrets = {}
    if secrets.get("auth") and ask("Smart Life is already linked. Re-link? [y/N] ").strip().lower() != "y":
        auth = secrets["auth"]
    else:
        say("\nOn your phone: Smart Life › Me › ⚙ Settings › Account and Security › User Code.")
        user_code = ask("User Code: ").strip()

        def show(payload: str) -> None:
            import qrcode
            code = qrcode.QRCode(border=1)
            code.add_data(payload)
            say("\nIn Smart Life tap + › Scan, scan this, and approve. (It may say “Home Assistant”; that is "
                "the name of Tuya's sign-in for local-control apps.)\n")
            code.print_ascii(invert=True)

        auth = qr_login(user_code, show)
        say("Linked.")
    say("Reading your devices…")
    records, scenes = import_account(auth)
    for tuya_id, old in secrets.get("devices", {}).items():  # keep known addresses as a head start
        if tuya_id in records and old.get("ip"):
            records[tuya_id].setdefault("ip", old["ip"])
    say("Looking for them on your network (this is what makes control instant)…")
    locate(records, say)
    save_secrets({"auth": auth, "devices": records, "scenes": scenes})

    existing = {table.get("tuya_id") for table in config["devices"].values()}
    fresh = {tuya_id: record for tuya_id, record in records.items() if tuya_id not in existing}
    devices, notes = suggest(fresh, set(config["devices"]))
    config["bridges"].setdefault("tuya", {})
    config["devices"].update(devices)
    say(f"\nAdded {len(devices)} device(s): {', '.join(devices) or 'none new'}")
    for note in notes:
        say("  note: " + note)
    if scenes:
        say("Smart Life scenes you can attach as actions: " + ", ".join(sorted(s["name"].strip() for s in scenes)))
