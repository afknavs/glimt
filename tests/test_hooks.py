import json
import time

import pytest

from glimt import hooks


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.client, "request", lambda method, path, body=None, **options: calls.append(
        (method, (body or {}).get("name"), (body or {}).get("message"), options)) or {})
    return calls


def event(name: str, **extra) -> dict:
    return {"hook_event_name": name, "session_id": "s1", "cwd": "/work/shop-api", **extra}


def rewind(home, seconds: float) -> None:
    (home / "sessions" / "s1").write_text(str(time.time() - seconds))


def test_a_quick_turn_stays_quiet_and_a_long_one_cues_done(sent, home):
    assert hooks.handle(event("UserPromptSubmit")) == "clear"
    assert hooks.handle(event("Stop")) is None                     # you were watching
    rewind(home, 45)
    assert hooks.handle(event("Stop")) == "done"
    assert [call[:3] for call in sent] == [("DELETE", None, None), ("POST", "done", "Finished in shop-api")]
    assert all(call[3] == {"timeout": 2, "autostart": False} for call in sent)   # a hook never stalls an agent


def test_a_blocked_agent_holds_attention_until_you_deal_with_it(sent, home):
    hooks.handle(event("UserPromptSubmit"))
    rewind(home, 45)
    assert hooks.handle(event("PostToolUse")) is None              # nothing waiting: no I/O at all
    assert len(sent) == 1
    assert hooks.handle(event("Notification", message="Claude needs your permission to use Bash")) == "attention"
    assert sent[-1][:3] == ("POST", "attention", "Claude needs your permission to use Bash")
    assert hooks.handle(event("PostToolUse")) == "clear"           # you approved; the tool ran
    assert hooks.handle(event("PostToolUse")) is None
    assert hooks.handle(event("PermissionRequest")) == "attention"  # Codex's name for the same thing
    assert hooks.handle(event("UserPromptSubmit")) == "clear"
    assert not (home / "sessions" / "s1.waiting").exists()


def test_idle_reminders_and_watched_prompts_do_not_cue(sent, home):
    hooks.handle(event("UserPromptSubmit"))
    assert hooks.handle(event("Notification", message="Claude needs your permission")) is None   # 0 s in: watching
    rewind(home, 45)
    assert hooks.handle(event("Notification", notification_type="idle_prompt")) is None
    assert hooks.handle(event("Notification", message="Claude is waiting for your input")) is None
    (home / "config.toml").write_text("[hooks]\nidle = true\nmin_seconds = 5\n")
    assert hooks.handle(event("Notification", notification_type="idle_prompt")) == "attention"


def test_unknown_events_and_bad_payloads_are_ignored(sent):
    assert hooks.handle({"hook_event_name": "SessionStart"}) is None
    assert hooks.handle({}) is None
    assert hooks.read_payload("not json") == {} and hooks.read_payload('["list"]') == {}
    assert hooks.read_payload('{"type": "agent-turn-complete"}') == {"type": "agent-turn-complete"}
    assert sent == []


def test_install_keeps_existing_hooks_is_idempotent_and_uninstalls_cleanly(tmp_path):
    path = tmp_path / "settings.json"
    mine = {"type": "command", "command": "say done"}
    path.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [mine]}]}}))

    _, added = hooks.install("claude", path)
    assert added == ["Stop", "Notification", "PostToolUse", "UserPromptSubmit"]
    assert hooks.install("claude", path)[1] == [] and hooks.installed("claude", path)
    data = json.loads(path.read_text())
    assert data["model"] == "x" and data["hooks"]["Stop"][0]["hooks"] == [mine] and len(data["hooks"]["Stop"]) == 2
    assert json.loads((tmp_path / "settings.json.before-glimt").read_text())["hooks"] == {"Stop": [{"hooks": [mine]}]}

    assert hooks.uninstall("claude", path)[1] == 4
    assert json.loads(path.read_text()) == {"model": "x", "hooks": {"Stop": [{"hooks": [mine]}]}}
    assert not hooks.installed("claude", path)


def test_a_quoted_install_path_is_still_recognised(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "command", lambda agent=None: '"/Users/me/My Tools/glimt" hook --agent codex')
    path = tmp_path / "hooks.json"
    assert hooks.install("codex", path)[1] == ["Stop", "PermissionRequest", "PostToolUse", "UserPromptSubmit"]
    assert hooks.install("codex", path)[1] == []
    assert hooks.uninstall("codex", path)[1] == 4 and json.loads(path.read_text()) == {}


def test_agent_settings_are_replaced_whole_backed_up_and_never_clobbered(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "x"}))
    path.chmod(0o600)
    hooks.install("claude", path)
    assert path.stat().st_mode & 0o777 == 0o600                  # permissions survive the swap
    assert not list(tmp_path.glob(".*"))                         # no half-written temporary file left behind

    installed = path.read_text()
    hooks.uninstall("claude", path)                              # removing keeps a way back too
    assert (tmp_path / "settings.json.before-glimt").read_text() == installed

    # Someone (the agent itself, an editor) saved the file while we were working on it.
    current = path.read_text()
    with pytest.raises(RuntimeError, match="changed while it was being edited"):
        hooks._write(path, "what we read a moment ago", {"hooks": {}})
    assert path.read_text() == current


def test_a_cue_says_which_agent_is_asking(home, monkeypatch, tmp_path):
    bodies = []
    monkeypatch.setattr(hooks.client, "request", lambda method, path, body=None, **options: bodies.append(body) or {})
    rewind_to = home / "sessions"
    rewind_to.mkdir()
    (rewind_to / "s1").write_text(str(time.time() - 60))
    hooks.handle(event("Stop"), "claude")
    hooks.handle(event("PermissionRequest", message="Run the migration?"), "codex")
    hooks.handle(event("Stop"))                                    # an install from before agents were named
    assert [body["source"] for body in bodies] == ["Claude Code", "Codex", "agent"]

    path = tmp_path / "settings.json"
    hooks.install("claude", path)
    installed = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert installed.endswith("hook --agent claude") and hooks.installed("claude", path)
