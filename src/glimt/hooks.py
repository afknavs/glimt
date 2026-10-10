"""Agent hooks: fire cues from an agent's lifecycle with no model round trip at all.

Claude Code and Codex both run a command on events like "the agent stopped" and pass
a JSON description on stdin. `glimt hook` reads that and decides what to show:

    Stop               -> "done", but only if the turn ran long enough that you
                          probably looked away (hooks.min_seconds, default 20)
    Notification /
    PermissionRequest  -> "attention": the agent is blocked on you. The lights stay
                          amber until you deal with it.
    PostToolUse        -> you dealt with it: end the "attention" cue
    UserPromptSubmit   -> you are back; clear any held cue and start the turn clock

    [hooks]
    min_seconds = 20     # no cue for turns shorter than this: you were watching
    idle = false         # also cue when the agent has merely been idle for a while
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import client, paths

MARK = re.compile(r"""(glimt(\.exe)?['"]?|-m glimt) hook\b""")
EVENTS = {
    "claude": ["Stop", "Notification", "PostToolUse", "UserPromptSubmit"],
    "codex": ["Stop", "PermissionRequest", "PostToolUse", "UserPromptSubmit"],
}
LABELS = {"claude": "Claude Code", "codex": "Codex"}
TARGETS = {
    "claude": lambda: Path.home() / ".claude" / "settings.json",
    "codex": lambda: Path.home() / ".codex" / "hooks.json",
}


def _marker(session_id: str, suffix: str = "") -> Path:
    folder = paths.home() / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:80] + suffix)


def _options() -> dict:
    try:
        import tomllib
        return tomllib.loads(paths.config_file().read_text()).get("hooks", {})
    except Exception:
        return {}


def _send(method: str, body: dict | None = None) -> None:
    # Never start the daemon from a hook and never wait long: a cue is not worth a stall.
    client.request(method, "/v1/signal", body, timeout=2, autostart=False)


def _prune(folder: Path) -> None:
    week_ago = time.time() - 7 * 86400
    for path in folder.iterdir():
        if path.stat().st_mtime < week_ago:
            path.unlink(missing_ok=True)


def handle(payload: dict, agent: str | None = None) -> str | None:
    """Act on one hook event. Returns the name of the signal sent, 'clear', or None."""
    source = LABELS.get(agent or "", "agent")  # shown next to the cue: who is asking for you
    event = payload.get("hook_event_name") or payload.get("type")
    session = str(payload.get("session_id") or payload.get("thread-id") or "default")
    started_file, waiting_file = _marker(session), _marker(session, ".waiting")

    if event == "PostToolUse":  # runs after every tool call, so the common path does no I/O
        if not waiting_file.exists():
            return None
        waiting_file.unlink(missing_ok=True)
        _send("DELETE")
        return "clear"

    options = _options()
    started = float(started_file.read_text()) if started_file.exists() else None
    watching = started is not None and time.time() - started < float(options.get("min_seconds", 20))
    project = Path(payload.get("cwd") or "").name

    if event == "UserPromptSubmit":
        _prune(started_file.parent)
        started_file.write_text(str(time.time()))
        waiting_file.unlink(missing_ok=True)
        _send("DELETE")
        return "clear"
    if event in ("Stop", "agent-turn-complete"):
        was_waiting = waiting_file.exists()
        waiting_file.unlink(missing_ok=True)
        if watching:  # a quick reply while you were watching needs no cue
            if was_waiting:
                _send("DELETE")
            return None
        _send("POST", {"name": "done", "message": f"Finished in {project}" if project else "Finished",
                       "source": source})
        return "done"
    if event in ("Notification", "PermissionRequest"):
        message = str(payload.get("message") or "Waiting for you")
        idle = payload.get("notification_type") == "idle_prompt" or "waiting for your input" in message.lower()
        if watching or (idle and not options.get("idle", False)):
            return None
        waiting_file.write_text(str(time.time()))
        _send("POST", {"name": "attention", "message": message[:140], "source": source})
        return "attention"
    return None


def read_payload(argument: str | None) -> dict:
    """Hooks pass JSON on stdin; Codex's older `notify` passes it as the last argument."""
    raw = argument if argument else ("" if sys.stdin.isatty() else sys.stdin.read())
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


# ---- install / uninstall ------------------------------------------------------------


def launcher() -> list[str]:
    """How to start *this* Glimt from another program: its console script when we were
    started from one, otherwise `python -m glimt`."""
    script = Path(sys.argv[0])
    if script.name.startswith("glimt") and script.is_file():
        return [str(script.resolve())]
    beside = Path(sys.executable).with_name("glimt")  # same environment, however we were started
    if beside.is_file():
        return [str(beside)]
    found = shutil.which("glimt")
    return [found] if found else [sys.executable, "-m", "glimt"]


def command(agent: str | None = None) -> str:
    return " ".join(shlex.quote(part) for part in [*launcher(), "hook", *(["--agent", agent] if agent else [])])


def _read(path: Path) -> tuple[str, dict]:
    text = path.read_text() if path.exists() else ""
    return text, (json.loads(text) if text.strip() else {})


def _load(path: Path) -> dict:
    return _read(path)[1]


def _write(path: Path, before: str, data: dict) -> None:
    """Replace an agent's settings file safely: keep a copy of what was there, refuse to
    overwrite an edit someone made while we were working, and never leave half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".glimt-tmp")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(json.dumps(data, indent=2) + "\n")
        if path.exists():
            shutil.copymode(path, temporary)
            # Checked as late as possible. The agent does not lock this file, so a save in the
            # instant between this line and the swap cannot be ruled out, only made unlikely.
            if path.read_text() != before:
                raise RuntimeError(f"{path} changed while it was being edited. Nothing was written; "
                                   "run the command again.")
            shutil.copy2(path, path.with_name(path.name + ".before-glimt"))
        elif before:
            raise RuntimeError(f"{path} was removed while it was being edited. Nothing was written.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def installed(agent: str, path: Path | None = None) -> bool:
    try:
        groups = _load(path or TARGETS[agent]()).get("hooks", {}).values()
    except (json.JSONDecodeError, OSError):
        return False
    return any(MARK.search(handler.get("command", "")) for event in groups for group in event
               for handler in group.get("hooks", []))


def install(agent: str, path: Path | None = None) -> tuple[Path, list[str]]:
    """Add Glimt to an agent's hooks, leaving every existing hook in place."""
    path = path or TARGETS[agent]()
    before, data = _read(path)
    hooks = data.setdefault("hooks", {})
    added = []
    for event in EVENTS[agent]:
        groups = hooks.setdefault(event, [])
        if any(MARK.search(handler.get("command", "")) for group in groups for handler in group.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": command(agent), "timeout": 5}]})
        added.append(event)
    if added:
        _write(path, before, data)
    return path, added


def uninstall(agent: str, path: Path | None = None) -> tuple[Path, int]:
    path = path or TARGETS[agent]()
    before, data = _read(path)
    removed = 0
    hooks = data.get("hooks", {})
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            handlers = [h for h in group.get("hooks", []) if not MARK.search(h.get("command", ""))]
            removed += len(group.get("hooks", [])) - len(handlers)
            if handlers:
                kept.append({**group, "hooks": handlers})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if removed:
        if not hooks:
            data.pop("hooks", None)
        _write(path, before, data)
    return path, removed
