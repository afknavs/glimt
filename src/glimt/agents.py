"""Connect coding agents to Glimt in one step.

Two things get wired up, both removable with `glimt disconnect`:

* lifecycle hooks: a cue when the agent finishes or is blocked on you, with no model
  round trip at all (see hooks.py);
* the MCP server: tools the agent can call itself to cue you or control a device.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import hooks

AGENTS = hooks.LABELS


def mcp_command(agent: str, remove: bool = False) -> list[str]:
    scope = ["--scope", "user"] if agent == "claude" else []
    if remove:
        return [agent, "mcp", "remove", *scope, "glimt"]
    return [agent, "mcp", "add", *scope, "glimt", "--", *hooks.launcher(), "mcp"]


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def connect(agent: str) -> list[str]:
    """Install hooks and register the MCP server. Returns lines to show the user."""
    path, added = hooks.install(agent)
    lines = [f"hooks  {'added to' if added else 'already in'} {path}"]
    if agent == "codex" and added:
        lines.append("       Codex will ask you to trust the new hooks the next time it starts.")
    command = mcp_command(agent)
    if not shutil.which(agent):
        lines.append(f"tools  `{agent}` is not on your PATH; once it is, run: {' '.join(command)}")
        return lines
    ok, output = _run(command)
    if ok:
        lines.append("tools  MCP server registered")
    elif "already" in output.lower():
        lines.append("tools  MCP server already registered")
    else:
        lines.append(f"tools  could not register the MCP server ({output.splitlines()[-1] if output else 'no output'})")
        lines.append(f"       try: {' '.join(command)}")
    return lines


def disconnect(agent: str) -> list[str]:
    path, removed = hooks.uninstall(agent)
    lines = [f"hooks  removed {removed} from {path}" if removed else "hooks  none installed"]
    if shutil.which(agent):
        ok, _ = _run(mcp_command(agent, remove=True))
        lines.append("tools  MCP server removed" if ok else "tools  no MCP server registered")
    return lines


def connected(agent: str) -> bool:
    return hooks.installed(agent)


def _has_tools(agent: str) -> bool:
    """Is our MCP server registered? Read from the agent's own config: asking the agent
    itself (`claude mcp list`) takes seconds and starts every other server it knows."""
    try:
        if agent == "claude":
            servers = json.loads((Path.home() / ".claude.json").read_text()).get("mcpServers", {})
        else:
            import tomllib
            servers = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text()).get("mcp_servers", {})
    except (OSError, ValueError):
        return False
    return "glimt" in servers


def status() -> list[dict]:
    """For each agent we know: is it installed, does it cue you, can it use your devices?"""
    return [{"id": agent, "name": label, "found": shutil.which(agent) is not None,
             "hooks": hooks.installed(agent), "tools": _has_tools(agent)} for agent, label in AGENTS.items()]
