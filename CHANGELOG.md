# Changelog

## 0.1.0

First public release.

- Daemon with persistent device connections, a local HTTP API and a live event stream.
- Cues: `done`, `attention`, `error`, `info`, `working`, custom cues, quiet hours, and
  snapshot-and-restore of every borrowed light.
- Agents: lifecycle hooks for Claude Code and Codex (`glimt connect`), an MCP server
  with five tools, `glimt run`.
- Remote access for agents that run in the cloud (ChatGPT, dots): `glimt remote`, an
  OAuth-protected MCP endpoint on its own port, with every sign-in confirmed on your
  computer.
- Bridges: Tuya / Smart Life (LAN first), ELK-BLEDOM Bluetooth strips, Home Assistant,
  Philips Hue, WLED, the computer's speaker, simulated devices.
- Bridge kit: one-file bridges in `~/.glimt/bridges/`, `glimt bridge new`,
  conformance checks with `glimt bridge test`.
- `glimt setup` (devices, login service, agents and the menu bar app in one run) and
  `glimt doctor`.
- `glimt mute` / `unmute`, and quiet hours.
- Apple Home export, web dashboard, and a macOS menu bar app installed with
  `glimt app install`.
