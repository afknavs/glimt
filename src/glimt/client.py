"""Tiny client for the local daemon. Stdlib only, so `glimt signal done` starts in
tens of milliseconds and can be called from any hook or script."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time

from . import paths


class DaemonError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _once(method: str, path: str, body: dict | None, timeout: float) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", paths.port(), timeout=timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=payload, headers={
            "Authorization": "Bearer " + paths.token(), "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
        if response.status >= 400:
            raise DaemonError(data.get("error", f"HTTP {response.status}"), response.status)
        return data
    finally:
        connection.close()


def running() -> bool:
    try:
        _once("GET", "/v1/health", None, 1.5)
        return True
    except (OSError, DaemonError, ValueError):
        return False


def start_daemon(wait: float = 8.0) -> bool:
    """Start the daemon in the background if it is not already up."""
    if running():
        return True
    with open(os.open(paths.log_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "ab") as log:
        subprocess.Popen([sys.executable, "-m", "glimt", "daemon"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "PYTHONUNBUFFERED": "1"})
    deadline = time.time() + wait
    while time.time() < deadline:
        if running():
            return True
        time.sleep(0.1)
    return False


def stop_daemon(wait: float = 15.0) -> bool:
    """Stop the daemon that answers on our port with our token, and wait until its process
    has gone. Returns False if none was running.

    The process id comes from that authenticated answer, never from a file on disk: a pid
    written last week may belong to something else by now. We wait for the process, not
    for the port to fall silent, because the daemon stops listening first and only then
    puts borrowed lights back and lets go of its devices; starting a replacement before
    that finishes would have two daemons talking to the same hardware."""
    try:
        pid = int(_once("GET", "/v1/health", None, 1.5)["pid"])
    except (OSError, DaemonError, ValueError, KeyError, TypeError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            os.kill(pid, 0)  # signal 0 only asks whether the process still exists
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    raise DaemonError(f"The daemon (process {pid}) has not stopped after {wait:.0f} s. See {paths.log_file()}")


def request(method: str, path: str, body: dict | None = None, *, timeout: float = 12.0,
            autostart: bool = True) -> dict:
    try:
        return _once(method, path, body, timeout)
    except ConnectionRefusedError:
        if not autostart or os.environ.get("GLIMT_NO_AUTOSTART"):
            raise DaemonError("Glimt is not running. Start it with: glimt start") from None
        if not start_daemon():
            raise DaemonError(f"Could not start the daemon. See {paths.log_file()}") from None
        return _once(method, path, body, timeout)
    except TimeoutError:
        raise DaemonError("The daemon did not answer in time") from None
