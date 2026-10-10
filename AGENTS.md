# Notes for AI agents working in this repository

Glimt is a local daemon that lets AI agents get a person's attention through the
lights and speakers in their room, and control those devices quickly.

## If you were asked to set Glimt up for someone

1. Install it: `pipx install "glimt[all] @ git+https://github.com/afknavs/glimt"` (Python
   3.11+; `pip install --user` works if there is no pipx).
2. Prove it works with no hardware: `glimt demo` starts simulated devices and prints a
   dashboard address. Fire `glimt signal done` and tell the person what they should see.
3. Real devices need the person. `glimt setup` asks a few questions and some steps cannot
   be done for them: scanning a QR code in the Smart Life app, pressing the button on a
   Hue bridge, allowing Bluetooth. Ask them to run `glimt setup` in their own terminal
   (or `glimt setup tuya`, `hue`, `wled`, `elk-ble`, `homeassistant` to go straight to
   one), then carry on when they say it is done. Do not ask for, read or print passwords,
   keys or tokens; the wizard stores what it needs under `~/.glimt/secrets/` itself.
4. Check: `glimt doctor` says exactly what is wrong and how to fix it. Fix what it lists.
5. Connect agents: `glimt connect` (Claude Code and Codex). On macOS, `glimt app install`
   adds the menu bar app. For ChatGPT or another cloud agent, see `docs/remote.md`.
6. If their device family is not supported, read `docs/bridges.md` and write the bridge.

Do not switch a real device on or off to test unless they said you may; a cue
(`glimt signal info`) restores the lights afterwards and is the safe way to show it works.

## If you were asked to add support for a device

Read `docs/bridges.md` first. It is the complete contract and is written for you.
A bridge is one file; you do not need to change anything else. Verify with
`glimt bridge test <type>`. Do not switch a real device on or off to "see if it
works" unless the person said you may; the test proves the bridge without changing
anything visible.

## Map

| Path | What |
| --- | --- |
| `src/glimt/model.py` | the common device model: kinds, capabilities, validation |
| `src/glimt/bridges/base.py` | the bridge contract |
| `src/glimt/bridges/*.py` | one file per hardware family; `virtual.py` is the smallest complete example |
| `src/glimt/hub.py` | device registry, the one command path, events |
| `src/glimt/signals.py` | cues: patterns, snapshot and restore |
| `src/glimt/daemon.py` | the local HTTP API |
| `src/glimt/hooks.py`, `agents.py`, `mcp_server.py` | how agents on this computer reach it |
| `src/glimt/remote.py`, `setup/tunnel.py` | how agents in the cloud reach it: the MCP tools over HTTP, sign-in, tokens |
| `src/glimt/homekit.py` | Apple Home export |
| `src/glimt/setup/` | the `glimt setup` wizard |
| `src/glimt/conformance.py` | the checks behind `glimt bridge test` |
| `app/macos/` | SwiftUI menu bar app (`build.sh`, no Xcode project); shipped in the wheel and built by `glimt app install` (`menuapp.py`) |
| `docs/` | user and bridge documentation |

## Commands

```bash
pip install -e ".[all,dev]"
pytest -q                              # must stay green; ~15 s, no hardware, no network
GLIMT_HOME=/tmp/cl GLIMT_PORT=18799 glimt demo   # a throwaway instance to try things
```

## Rules

- Never read, print or commit anything from `~/.glimt/secrets/`, a `token` file, or
  a `*.state` file. Never put credentials in `config.toml`, logs, tests or error text.
- Tests run against a temporary `GLIMT_HOME` (see `tests/conftest.py`). Do not make
  a test depend on a real setup, on hardware, or on the network.
- Everything above a bridge speaks only the common model. If you find yourself writing
  a brand name outside `bridges/` or `setup/`, the design is being bypassed.
- Bridges must not block the event loop, read before writing, or wait for confirmation
  in `apply()`.
- When you change behaviour, update the page in `docs/` that describes it.
- Match the surrounding style: typed, compact, 120 columns, comments that say why.
