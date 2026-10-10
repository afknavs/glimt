# Where to run it

Glimt is one small program (about 100 MB of memory) that has to be running whenever you
want your room to respond. Put it on a computer that is on when you are.

| Where | State | What you get |
| --- | --- | --- |
| **The computer your agents run on** (a Mac you keep on) | Used every day by the author | Everything: cues from Claude Code and Codex, device control, the menu bar app, Apple Home, remote access |
| **A Linux desktop or laptop** | Written for it, tests are set to run on Linux, **not yet used day to day** | Everything except the menu bar app |
| **A Raspberry Pi or other always-on Linux box** | **Untested.** If you try it, please [open an issue](https://github.com/afknavs/glimt/issues) and say how it went | Device control, Apple Home, remote access for ChatGPT and other cloud agents. See the catch below |
| **Next to Home Assistant** | The bridge is tested against a simulated Home Assistant only | Glimt drives whatever Home Assistant already controls, wherever Home Assistant runs |
| **Windows** | Not supported | There is no background service for it yet |

## The catch with a separate box

The cue that fires when Claude Code or Codex finishes a task comes from a hook on the
computer the agent runs on, and that hook talks to a Glimt **on the same computer**. So:

- If your agents and Glimt share a computer, everything works with `glimt connect`.
- If Glimt lives on a Pi and your agents on a laptop, the automatic "task finished" cue
  does not reach the Pi today. The agents can still call Glimt's tools through
  [remote access](remote.md), the same way ChatGPT does.

This is the reason the recommended home for Glimt is the computer you already keep on,
not a spare one.

## A Mac

```bash
pipx install "glimt[all] @ git+https://github.com/afknavs/glimt"
glimt setup
glimt service install     # start at login and stay running
glimt app install         # the menu bar app
```

A laptop with its lid shut is asleep, and a sleeping computer answers nobody. Leave the
lid open, and plugged in if you will be away for a while.

## Linux, including a Raspberry Pi

Needs Python 3.11 or newer (Raspberry Pi OS "Bookworm" ships 3.11).

```bash
sudo apt install pipx
pipx install "glimt[all] @ git+https://github.com/afknavs/glimt"
glimt setup
glimt service install     # a systemd user service
```

A user service stops when you log out. On a box with no screen, let it run from boot:

```bash
sudo loginctl enable-linger "$USER"
```

Things to know before you try:

- **No menu bar app.** Use the command line, Apple Home, or your agents. The dashboard
  only answers on the machine itself (`127.0.0.1`), so on a box with no screen it is not
  much use yet.
- **Bluetooth strips** go through BlueZ. The strip has to be within Bluetooth range of
  the box, and the user running Glimt needs permission to use Bluetooth.
- **Sound cues** need `paplay`, `pw-play` or `aplay`, and a speaker.
- `glimt doctor` tells you what is missing. Paste its output into an issue if you get
  stuck: it never contains keys or tokens.

## Apple Home, at home and away

Glimt appears in Apple Home as a bridge, like a Hue bridge does. That makes your devices
show up in the Home app and Siri while your phone is on the same Wi-Fi as the computer
running Glimt.

It is not Apple's home hub. Away from home, the Home app reaches any accessory only
through a HomePod or Apple TV in the house; that is Apple's rule for every HomeKit
device. If you have one, Glimt's devices should be reachable from anywhere like the
rest of your home (the author has no hub, so this is untested). If you do not, use an
agent with [remote access](remote.md) when you are out.
