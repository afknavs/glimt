"""Run the daemon at login and keep it alive: launchd on macOS, systemd on Linux."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from . import paths

LABEL = "dev.glimt.daemon"


def _plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "glimt.service"


def supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _service_home() -> Path | None:
    """The GLIMT_HOME the login service runs with, or None when there is no service."""
    path = _plist() if sys.platform == "darwin" else _unit()
    if not path.exists():
        return None
    home = None
    try:
        if sys.platform == "darwin":
            with open(path, "rb") as handle:
                home = plistlib.load(handle).get("EnvironmentVariables", {}).get("GLIMT_HOME")
        else:
            found = re.search(r"^Environment=GLIMT_HOME=(.+)$", path.read_text(), re.M)
            home = found.group(1) if found else None
    except Exception:
        pass
    return Path(home).expanduser() if home else Path.home() / ".glimt"


def installed() -> bool:
    """Is there a login service for *this* GLIMT_HOME? There is one service per user,
    so a second home (a test setup, say) must never stop or replace it."""
    home = _service_home()
    return home is not None and home.resolve() == paths.home().resolve()


def elsewhere() -> Path | None:
    """The other home a login service already belongs to, if any."""
    home = _service_home()
    return home if home is not None and home.resolve() != paths.home().resolve() else None


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install() -> Path:
    other = elsewhere()
    if other:
        raise RuntimeError(f"A Glimt login service already runs from {other}. Leave it be and use "
                           "`glimt start` here, or run `glimt service uninstall` from that setup first.")
    environment = {key: os.environ[key] for key in ("GLIMT_HOME", "GLIMT_PORT") if key in os.environ}
    os.close(os.open(paths.log_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))  # logs stay private
    if sys.platform == "darwin":
        path = _plist()
        path.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        _launchctl("bootout", f"{domain}/{LABEL}")
        with open(path, "wb") as handle:
            plistlib.dump({
                "Label": LABEL,
                "ProgramArguments": [sys.executable, "-m", "glimt", "daemon"],
                "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Interactive",
                "ThrottleInterval": 10,
                "StandardOutPath": str(paths.log_file()), "StandardErrorPath": str(paths.log_file()),
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **environment},
            }, handle)
        result = _launchctl("bootstrap", domain, str(path))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed")
        return path
    if sys.platform.startswith("linux"):
        path = _unit()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["[Unit]", "Description=Glimt daemon", "After=network-online.target", "",
                 "[Service]", f"ExecStart={sys.executable} -m glimt daemon", "Restart=always",
                 "RestartSec=5", "Environment=PYTHONUNBUFFERED=1"]
        lines += [f"Environment={key}={value}" for key, value in environment.items()]
        lines += ["", "[Install]", "WantedBy=default.target", ""]
        path.write_text("\n".join(lines))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "glimt"], check=True)
        return path
    raise RuntimeError("Background service is supported on macOS and Linux. Use `glimt start` instead.")


def uninstall() -> bool:
    if not installed():
        return False
    if sys.platform == "darwin" and _plist().exists():
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        _plist().unlink()
        return True
    if sys.platform.startswith("linux") and _unit().exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", "glimt"], check=False)
        _unit().unlink()
        return True
    return False


def restart() -> bool:
    """Restart the managed service. Returns False when no service is installed."""
    if not installed():
        return False
    if sys.platform == "darwin":
        _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
    else:
        subprocess.run(["systemctl", "--user", "restart", "glimt"], check=False)
    return True


def stop() -> bool:
    if not installed():
        return False
    if sys.platform == "darwin":
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    else:
        subprocess.run(["systemctl", "--user", "stop", "glimt"], check=False)
    return True
