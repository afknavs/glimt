"""`glimt doctor`: say what is wrong and exactly how to fix it."""

from __future__ import annotations

import json
import shutil
import sys
from urllib.parse import urlparse

from . import __version__, client, config as config_module, hooks, paths, service

HINTS = [
    (("no route to host", "errno 65", "host is down", "errno 64"),
     "The device did not answer on your network. Check it is powered and on the same Wi-Fi. On macOS, "
     "also allow local network access: System Settings › Privacy & Security › Local Network › enable "
     "Python (and your terminal app)."),
    (("bluetooth",),
     "Allow Bluetooth: System Settings › Privacy & Security › Bluetooth › enable Python (and your "
     "terminal app). Also close the strip's phone app; most strips accept one connection at a time."),
    (("needs the", "pip install"), "Install the missing extra shown above, then: glimt restart"),
    (("token", "sign in", "re-link", "authoriz"), "Sign in again with: glimt setup"),
]


def _hint(error: str) -> str | None:
    lowered = error.lower()
    for needles, hint in HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return None


def _reachable(url: str) -> bool | str:
    """Ask the public address what it is. True when it is this Glimt; otherwise what went wrong."""
    import httpx
    try:
        answer = httpx.get(url + "/.well-known/oauth-protected-resource", timeout=6)
        return True if answer.status_code == 200 and answer.json().get("resource") == url + "/mcp" \
            else f"it answered {answer.status_code}, not as Glimt"
    except (httpx.HTTPError, ValueError) as exc:
        return f"no answer ({type(exc).__name__})"


def _listed(host: str) -> bool | None:
    """Does the wider internet know this name? A tunnel's own network answers for it before anyone else can."""
    import ipaddress
    import socket
    import struct
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        if not host or host == "localhost":
            return None
    question = b"".join(bytes([len(part)]) + part for part in host.encode("idna").split(b".")) + b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as asking:
            asking.settimeout(3)
            asking.sendto(struct.pack(">HHHHHH", 0x4375, 0x0100, 1, 0, 0, 0) + question + struct.pack(">HH", 1, 1),
                          ("1.1.1.1", 53))
            answer = asking.recv(512)
    except OSError:
        return None  # no way to ask, so nothing to say
    return len(answer) >= 8 and answer[3] & 0x0F == 0 and struct.unpack(">H", answer[6:8])[0] > 0


def run(fire: bool = False) -> int:
    problems = 0

    def line(ok: bool | None, text: str, hint: str | None = None) -> None:
        nonlocal problems
        print(f"{'✓' if ok else '•' if ok is None else '✗'} {text}")
        if ok is False:
            problems += 1
        if hint and not ok:
            print(f"    → {hint}")

    print(f"glimt {__version__}  ·  python {sys.version.split()[0]}  ·  home {paths.home()}\n")
    try:
        config = config_module.load()
        count = len(config["devices"])
        line(count > 0, f"config: {count} device(s), {len(config['bridges'])} bridge(s) in {paths.config_file()}",
             "No devices yet. Run: glimt setup   (or: glimt demo)")
    except config_module.ConfigError as exc:
        line(False, f"config: {exc}", "Fix the line above in config.toml, then: glimt restart")
        return 1

    if not client.running():
        line(False, "daemon is not running", "glimt start")
        return 1
    health = client.request("GET", "/v1/health")
    line(True, f"daemon: running on 127.0.0.1:{paths.port()}, up {health['uptime']}s")
    line(service.installed() or None,
         "starts at login" if service.installed() else "not set to start at login (optional: glimt service install)")

    for name, bridge in health["bridges"].items():
        error = bridge["error"]
        line(not error, f"bridge {name} ({bridge['type']})" + (f": {error}" if error else ""),
             _hint(error) if error else None)

    listing = client.request("GET", "/v1/devices")["devices"]
    for raw in listing:
        if raw.get("online") is False:
            error = raw.get("error") or "unreachable"
            line(False, f"device {raw['id']}: {error}", _hint(error))
        else:
            route = raw.get("route") or "?"
            note = " (state is last-sent; this device cannot report)" if raw.get("assumed") else ""
            waiting = raw.get("online") is None and not raw.get("assumed")
            line(None if waiting else True,
                 f"device {raw['id']}: {'no state reported yet' if waiting else 'ok'} via {route}{note}")

    targets = [raw["id"] for raw in listing
               if raw.get("kind") == "light" and "color" in raw.get("capabilities", {})]
    wanted = config.get("signals", {}).get("targets") or targets
    line(bool(wanted) or None, f"cues will show on: {', '.join(wanted)}" if wanted else
         "no colour light to show cues on (cues will be sound only); set [signals] targets = [...]")
    speaker = next((raw["id"] for raw in listing if raw.get("kind") == "speaker"), None)
    line(bool(speaker) or None, f"cues will sound on: {speaker}" if speaker else
         "no speaker configured (optional: add a [bridges.sound] and a speaker device)")

    for agent, label in (("claude", "Claude Code"), ("codex", "Codex")):
        wired = hooks.installed(agent)
        line(wired or None, f"{label} hooks installed" if wired else
             f"{label} hooks not installed (optional: glimt connect {agent})")
    line(bool(shutil.which("glimt")) or None,
         "`glimt` is on your PATH" if shutil.which("glimt") else
         "`glimt` is not on your PATH; hooks use its full path, so this is only a convenience")

    remote = health.get("remote")
    if remote and remote.get("enabled"):
        reached = _reachable(remote["url"]) if not remote["error"] else None
        line(not remote["error"] and reached is True,
             f"remote access: {remote['endpoint']}, {len(remote['apps'])} app(s) connected"
             + (f": {remote['error']}" if remote["error"] else "" if reached is True else f": {reached}"),
             "The public address does not reach this computer. Is your tunnel running, and does it send "
             f"{remote['url']} to 127.0.0.1:{remote['port']}? See docs/remote.md")
        if reached is True and _listed(urlparse(remote["url"]).hostname or "") is False:
            line(None, "the internet does not list that address yet, so apps outside cannot find it",
                 "A new Tailscale Funnel address takes about ten minutes to appear. Leave it on and check again.")
    elif remote:
        line(False, f"remote access is off: {remote['error']}", "Fix [remote] in config.toml, then: glimt restart")

    if fire:
        result = client.request("POST", "/v1/signal", {"name": "info", "source": "doctor", "urgent": True})
        line(True, f"fired a test cue on {', '.join(result.get('targets') or ['(sound only)'])}; did you see it?")
    print("\nAll good." if not problems else f"\n{problems} problem(s) found.")
    return 1 if problems else 0
