"""A public address for remote access.

Glimt runs no tunnel of its own; it asks one you already have. That is Tailscale
Funnel here, because it gives a stable https address for free and keeps it across
restarts with nothing extra left running. Any other tunnel works too: point it at the
remote port and pass its address to `glimt remote enable <url>`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.parse import urlparse

APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
FUNNEL_PORTS = (443, 8443, 10000)  # the only ports Funnel serves


def tailscale() -> str | None:
    return shutil.which("tailscale") or (APP if Path(APP).exists() else None)


def _run(*args: str, timeout: float = 20) -> tuple[int, str]:
    # Its own process group: on macOS `tailscale` is a script around the real binary, and
    # a command left waiting for you must not outlive us.
    process = subprocess.Popen([tailscale(), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout), process.stdout.read()
    except subprocess.TimeoutExpired:  # it is waiting for something only you can do
        os.killpg(process.pid, signal.SIGTERM)
        return 124, process.communicate()[0]


def _json(*args: str) -> dict:
    code, text = _run(*args)
    try:
        return json.loads(text) if code == 0 else {}
    except ValueError:
        return {}


def funnel(port: int) -> str:
    """Send a public https address to 127.0.0.1:<port> and return that address."""
    if not tailscale():
        raise RuntimeError("No tunnel found. Install Tailscale (https://tailscale.com/download) and sign in, or "
                           "use any tunnel you like and run: glimt remote enable https://your-address")
    status = _json("status", "--json")
    if status.get("BackendState") != "Running":
        raise RuntimeError("Tailscale is installed but not connected. Turn it on from its menu (or run "
                           "`tailscale up`), then run this again.")
    name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name:
        raise RuntimeError("This computer has no Tailscale name. Turn on MagicDNS in the Tailscale admin console.")
    target = f"http://127.0.0.1:{port}"
    web = _json("serve", "status", "--json").get("Web") or {}
    mine = lambda p: ((web.get(f"{name}:{p}") or {}).get("Handlers") or {}).get("/", {}).get("Proxy") == target
    public = next((p for p in FUNNEL_PORTS if mine(p)), None) or \
        next((p for p in FUNNEL_PORTS if f"{name}:{p}" not in web), None)  # never take over something of yours
    if public is None:
        raise RuntimeError("Tailscale is already serving something else on every Funnel port (443, 8443, 10000).")
    code, said = _run("funnel", "--bg", f"--https={public}", target, timeout=15)
    if code != 0:
        link = re.search(r"https://login\.tailscale\.com/\S+", said)
        if link:
            raise RuntimeError("Tailscale needs you to allow Funnel once. Open this, approve, then run the "
                               f"command again:\n  {link.group(0)}")
        raise RuntimeError("Tailscale could not start Funnel:\n" + said.strip())
    return f"https://{name}" + ("" if public == 443 else f":{public}")


def close(url: str) -> bool:
    """Stop sending a Funnel address here. True if there was one to stop."""
    parsed = urlparse(url)
    if not (tailscale() and (parsed.hostname or "").endswith(".ts.net")):
        return False
    return _run("funnel", f"--https={parsed.port or 443}", "off")[0] == 0
