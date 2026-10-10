"""Setup paths that must work without any hardware: the wizard, Tuya import mapping, the
login-service guard, and what the MCP server tells an agent."""

import plistlib

import pytest

from glimt import paths, service
from glimt.setup.wizard import Wizard


def scripted(answers):
    answers = iter(answers)
    said = []
    wizard = Wizard(ask=lambda prompt: next(answers), say=said.append, secret=lambda prompt: next(answers))
    wizard.finish = lambda: None      # starting the daemon is covered by the CLI smoke test
    wizard.extras = lambda: None      # agents and the menu bar app have their own tests
    return wizard, said


def test_wizard_appends_devices_and_leaves_your_file_alone(home):
    original = '# my notes stay\n[bridges.virtual]\n\n[devices.lamp]\nbridge = "virtual"\nkind = "light"\n'
    paths.config_file().write_text(original)
    wizard, said = scripted(["6", "", "", "y"])      # simulated devices › add all › done › yes to a speaker
    wizard.run()
    text = paths.config_file().read_text()
    assert text.startswith(original.rstrip("\n"))
    assert "[devices.strip]" in text and "[devices.computer]" in text and text.count("[devices.lamp") == 1
    assert "[bridges.sound]" in text and text.count("[bridges.virtual]") == 1
    assert any("Added 1 device(s)." in line for line in said)   # the lamp was already there

    again, said = scripted(["", "n"])
    again.run()
    assert "\nNothing was changed." in said and paths.config_file().read_text() == text


def test_wizard_offer_can_pick_filter_or_skip(home):
    found = [{"id": "desk", "name": "Desk lamp", "kind": "light", "entity": "light.desk"},
             {"id": "hall", "name": "Hall light", "kind": "light", "entity": "light.hall"},
             {"id": "kettle", "name": "Kettle", "kind": "switch", "entity": "switch.kettle"}]
    wizard, _ = scripted(["light", "1"])             # filter to "light", then take the first shown
    wizard.config["bridges"]["ha"] = {"type": "homeassistant"}
    assert wizard.offer("ha", found) == 1
    assert wizard.config["devices"]["hall"] == {"name": "Hall light", "kind": "light", "bridge": "ha",
                                                "entity": "light.hall"}
    wizard.ask = lambda prompt: "none"
    assert wizard.offer("ha", found) == 0
    wizard.ask = lambda prompt: ""
    assert wizard.offer("ha", found) == 2            # the one already added is not offered twice
    assert set(wizard.config["devices"]) == {"hall", "desk", "kettle"}


def test_agent_status_and_the_offers_at_the_end_of_setup(home, monkeypatch):
    from pathlib import Path

    from glimt import agents, hooks, menuapp
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)             # a computer with no agents on it
    assert agents.status() == [
        {"id": "claude", "name": "Claude Code", "found": False, "hooks": False, "tools": False},
        {"id": "codex", "name": "Codex", "found": False, "hooks": False, "tools": False}]

    monkeypatch.setattr(agents.shutil, "which", lambda name: f"/usr/local/bin/{name}" if name == "claude" else None)
    ran = []
    monkeypatch.setattr(agents, "_run", lambda command: ran.append(command) or (True, ""))
    built = []
    monkeypatch.setattr(menuapp, "can_build", lambda: True)
    monkeypatch.setattr(menuapp, "install", lambda: built.append(True))
    (Path.home() / ".codex").mkdir()
    (Path.home() / ".codex" / "config.toml").write_text('[mcp_servers.glimt]\ncommand = "glimt"\n')

    asked = []
    wizard = Wizard(ask=lambda prompt: asked.append(prompt) or "y", say=lambda line: None)
    wizard.extras()
    assert len(asked) == 2 and "Connect Claude Code?" in asked[0] and "menu bar" in asked[1]   # Codex is not installed
    assert hooks.installed("claude") and ran[0][:3] == ["claude", "mcp", "add"] and built == [True]
    assert [(row["found"], row["hooks"], row["tools"]) for row in agents.status()] == [(True, True, False),
                                                                                         (False, False, True)]
    asked.clear()
    monkeypatch.setattr(menuapp, "installed", lambda: True)
    wizard.extras()
    assert asked == []                                           # nothing left to offer, nothing asked


def test_credentials_go_to_private_files_never_to_config(home):
    wizard, _ = scripted([])
    wizard.save_secret("hue.json", "hue", {"key": "abc"})
    wizard.save_secret("hue.json", "upstairs", {"key": "def"})
    path = paths.secrets_dir() / "hue.json"
    assert path.stat().st_mode & 0o777 == 0o600
    from glimt.bridges.hue import load_key
    assert load_key("hue") == "abc" and load_key("upstairs") == "def"


def test_tuya_devices_become_sensible_tables():
    pytest.importorskip("tinytuya")
    from glimt.setup.tuya import record_from, suggest

    def codes(*writable, **extra):
        return {**{code: {"writable": True} for code in writable}, **extra}

    records = {
        "b1": {"name": "Cupboard", "category": "dj", "codes": codes("switch_led", "bright_value_v2", "colour_data_v2")},
        "p1": {"name": "Extension", "category": "pc", "codes": codes("switch_1", "switch_2", "countdown_1")},
        "a1": {"name": "Bedroom AC", "category": "kt", "codes": codes(
            "switch", "temp_set", "mode_eco", fan_speed_enum={"writable": True, "range": ["low", "middle", "high"]},
            temp_current={"writable": False})},
        "r1": {"name": "Fan remote", "category": "qt", "codes": {}},
    }
    devices, notes = suggest(records, taken={"cupboard"})
    assert devices["cupboard-2"] == {"bridge": "tuya", "tuya_id": "b1", "name": "Cupboard", "kind": "light", "map": {
        "power": "switch_led", "brightness": "bright_value_v2", "color": "colour_data_v2"}}
    assert devices["extension-2"]["map"] == {"power": "switch_2"} and devices["extension-1"]["kind"] == "outlet"
    ac = devices["bedroom-ac"]
    assert ac["kind"] == "climate" and ac["map"]["fan"] == "fan_speed_enum" and ac["map"]["eco"] == "mode_eco"
    assert ac["values"] == {"fan": {"medium": "middle"}} and ac["read"] == {"current_temperature": "temp_current"}
    assert len(notes) == 1 and "Fan remote" in notes[0] and "Tap-to-Run" in notes[0]

    record = record_from({"name": " Plug ", "category": "cz", "local_key": "k", "status": {"cur_power": 1},
                          "local_strategy": {1: {"status_code": "switch_1"}},
                          "function": {"switch_1": {"type": "Boolean", "values": "{}"}},
                          "status_range": {"cur_power": {"type": "Integer", "values": '{"scale": 1, "max": 5000}'}}})
    assert record["name"] == "Plug" and record["local_key"] == "k"
    assert record["codes"]["switch_1"] == {"dp": "1", "type": "Boolean", "writable": True}
    assert record["codes"]["cur_power"] == {"type": "Integer", "writable": False, "scale": 1, "max": 5000}


def test_a_second_setup_can_never_take_over_the_login_service(home, tmp_path, monkeypatch):
    plist = tmp_path / "dev.glimt.daemon.plist"
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_plist", lambda: plist)
    assert not service.installed() and service.elsewhere() is None

    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"GLIMT_HOME": str(tmp_path / "other-home")}}))
    assert not service.installed() and service.elsewhere() == tmp_path / "other-home"
    with pytest.raises(RuntimeError, match="already runs from"):
        service.install()
    assert service.uninstall() is False and service.restart() is False and service.stop() is False
    assert plist.exists()

    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"GLIMT_HOME": str(home)}}))
    assert service.installed() and service.elsewhere() is None


def test_the_mcp_server_tells_an_agent_what_exists_and_what_happened(monkeypatch):
    from glimt import mcp_server

    listing = {"devices": [
        {"id": "desk", "name": "Desk lamp", "kind": "light", "bridge": "b", "actions": [], "assumed": False,
         "online": True, "state": {"power": True, "brightness": 40, "effect": None},
         "capabilities": {"power": {"type": "bool"}, "brightness": {"type": "int", "min": 0, "max": 100}}},
        {"id": "fan", "name": "Fan", "kind": "fan", "bridge": "b", "actions": ["faster"], "assumed": True,
         "online": False, "error": "remote unplugged", "state": {}, "capabilities": {}}]}
    monkeypatch.setattr(mcp_server.client, "request", lambda *args, **options: listing)
    assert mcp_server._device_lines(mcp_server.client.request) == ['desk (light, "Desk lamp"): sets power, brightness 0-100',
                                          'fan (fan, "Fan"): actions faster']
    assert mcp_server._state_text(listing["devices"][0]) == "desk (light): power=on brightness=40"
    assert mcp_server._state_text(listing["devices"][1]) == \
        "fan (fan): no readable state [last sent, device cannot report] [offline: remote unplugged]"
    assert mcp_server._done("desk", {"ms": 31}, "power=off") == "done: desk power=off (acknowledged in 31 ms)"
    assert mcp_server.build() is not None            # registers all five tools
