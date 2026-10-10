"""The MCP tools. A thin client of the local daemon, built to cost an agent one tool call
and almost no context:

* five tools with short descriptions,
* the device list is baked into the tool description, so no "list first" round trip,
* replies are a few words, not JSON dumps.

`glimt mcp` serves them over stdio to an agent on this computer; remote.py serves the
same tools over HTTP to one that is not.
"""

from __future__ import annotations

from typing import Callable, Literal

from mcp.types import ToolAnnotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import client
from .model import Device

INSTRUCTIONS = """\
Glimt is the direct, local way to control the devices in the user's room (they are
listed in the `control` tool) and to get the user's attention through them: a light
changes colour, a tone plays.
For these devices use these tools first, rather than shell commands, SSH or any other
home-automation skill. Call them straight away: there is nothing to look up, list or
check first. A reply starting with "done" means the device acknowledged the command, so
do not call devices() afterwards to verify; just tell the user.
Use signal() when a long task finishes ("done"), when you are blocked and need the user
("attention"), or when something failed ("error"). It returns instantly and the lights
restore themselves afterwards."""

REMOTE_INSTRUCTIONS = """\
Glimt reaches the room the user is physically in: a light there changes colour and a
tone plays, even when they are nowhere near a screen.
Use signal() to get their attention: "done" when a long task finishes, "attention" when
you are blocked and need them (it stays lit until they respond or you call
clear_signal), "error" when something failed. Say what you need in `message`; it shows
on their computer. Cue for things worth looking up for, not for every reply in a
conversation they are already reading. It returns instantly and the lights restore
themselves afterwards."""

REMOTE_CONTROL = """
You can also switch and adjust the devices in that room; they are listed in the `control`
tool. Call it straight away, with nothing to look up first. A reply starting with "done"
means the device acknowledged the command, so do not call devices() to verify."""

# What a client may assume about each tool. Nothing here deletes or reaches outside the room.
READS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _device_lines(request: Callable[..., dict]) -> list[str]:
    try:
        listing = request("GET", "/v1/devices", timeout=4)["devices"]
    except Exception:
        return []
    lines = []
    for raw in listing:
        device = Device(id=raw["id"], name=raw["name"], kind=raw["kind"], bridge=raw["bridge"],
                        capabilities=raw["capabilities"], actions=raw["actions"])
        lines.append(device.summary())
    return lines


def _state_text(raw: dict) -> str:
    parts = []
    for key, value in raw["state"].items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "on" if value else "off"
        parts.append(f"{key}={value}")
    text = f"{raw['id']} ({raw['kind']}): " + (" ".join(parts) or "no readable state")
    if raw.get("assumed"):
        text += " [last sent, device cannot report]"
    if raw.get("online") is False:
        text += f" [offline: {raw.get('error') or 'unreachable'}]"
    return text


def _value(value) -> str:
    return ("on" if value else "off") if isinstance(value, bool) else str(value)


def _done(device: str, result: dict, what: str) -> str:
    # Say enough that the agent has no reason to spend another round trip checking.
    return f"done: {device} {what} (acknowledged in {result['ms']} ms)"


def build(*, request: Callable[..., dict] | None = None, lines: list[str] | None = None,
          source: Callable[[], str] = lambda: "mcp", control: bool = True,
          instructions: str = INSTRUCTIONS, **server) -> MCPServer:
    """`request` reaches the daemon, `lines` is the device catalogue if the caller already
    has it, `source` names who is cueing, and `control=False` leaves only the cue tools."""
    mcp = MCPServer("glimt", instructions=instructions, **server)
    ask = request or client.request
    lines = _device_lines(ask) if lines is None else lines

    def request(method: str, path: str, body: dict | None = None) -> dict:
        try:
            return ask(method, path, body)
        except client.DaemonError as exc:
            # Said on purpose, so the agent reads why ("Unknown device 'sofa'. Devices: ...")
            # instead of the bare "Error executing tool" a crash gets.
            raise ToolError(str(exc)) from None
    catalog = ("Devices:\n" + "\n".join("- " + line for line in lines)) if lines else \
        "No devices are configured yet (the user can run `glimt setup`)."

    def signal(name: str = "done", message: str | None = None, color: str | None = None,
               pattern: Literal["pulse", "flash", "breathe", "solid"] | None = None,
               count: int | None = None, sound: str | None = None,
               hold_seconds: float | None = None) -> str:
        body = {"name": name, "message": message, "color": color, "pattern": pattern,
                "count": count, "sound": sound, "hold": hold_seconds, "source": source()}
        if color and name in ("done", "custom", ""):
            body["name"] = None  # a bare colour means a custom signal, not a recoloured "done"
        result = request("POST", "/v1/signal", {k: v for k, v in body.items() if v is not None})
        if result.get("skipped"):
            return f"skipped ({result['skipped']})"
        return "done: signalled"

    def clear_signal() -> str:
        request("DELETE", "/v1/signal")
        return "done: cleared"

    def devices() -> str:
        listing = request("GET", "/v1/devices")["devices"]
        return "\n".join(_state_text(raw) for raw in listing) or "no devices configured"

    def control_device(device: str, power: bool | None = None, brightness: int | None = None,
                color: str | None = None, temperature: int | None = None,
                settings: dict | None = None) -> str:
        changes = {**(settings or {})}
        for key, value in (("power", power), ("brightness", brightness), ("color", color),
                           ("temperature", temperature)):
            if value is not None:
                changes[key] = value
        result = request("POST", f"/v1/devices/{device}", changes)
        now = result.get("state", {})
        return _done(device, result, " ".join(f"{key}={_value(now.get(key, value))}" for key, value in changes.items()))

    def action(device: str, action: str) -> str:
        result = request("POST", f"/v1/devices/{device}/actions/{action}", {})
        return _done(device, result, action)

    mcp.add_tool(signal, structured_output=False, annotations=CHANGES, description=(
        "Get the user's attention through their lights and speaker. Returns immediately; the "
        "lights restore themselves. name: done (green), attention (amber, stays until cleared), "
        "error (red), info (blue), working (dim glow until cleared). Or pass color (#rrggbb or a "
        "name) with pattern for a custom cue. message is a short note shown in the Glimt app."))
    mcp.add_tool(clear_signal, structured_output=False, annotations=CHANGES,
                 description="End a held signal (attention/working) and restore the lights.")
    if not control:
        return mcp
    mcp.add_tool(devices, structured_output=False, annotations=READS,
                 description="Current state of every device. Only needed when you must know state.")
    mcp.add_tool(control_device, name="control", structured_output=False, annotations=CHANGES, description=(
        "Switch or adjust a device in the user's room. This is the direct path: call it straight "
        "away, with no lookup or state check first, and trust a \"done\" reply. Pass only what "
        "should change. Use `settings` for anything beyond power/brightness/color/temperature, "
        "e.g. {\"fan\": \"low\"}.\n" + catalog))
    mcp.add_tool(action, structured_output=False, annotations=CHANGES,
                 description="Run a momentary action on a device (see the actions listed under control).")
    return mcp


def main() -> None:
    build().run()
