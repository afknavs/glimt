# Troubleshooting

Start here:

```bash
glimt doctor            # checks config, daemon, every bridge and device, and says how to fix what is wrong
glimt doctor --signal   # the same, then fires a test cue
glimt status            # is it running, are the bridges healthy
tail -50 ~/.glimt/daemon.log
```

## macOS permissions

macOS asks for permission the first time a program talks to devices on your network or
over Bluetooth. The permission belongs to **the Python that runs the daemon**, so:

- **A device shows "No route to host" or "host is down"** although the phone app can
  reach it: allow local network access. System Settings › Privacy & Security ›
  Local Network › enable *Python* (and your terminal app), then `glimt restart`.
- **A Bluetooth strip never connects:** System Settings › Privacy & Security ›
  Bluetooth › enable *Python*, then `glimt restart`.
- If no prompt ever appeared, run `glimt start` once from Terminal rather than from
  inside another app, so the prompt has somewhere to show.
- Reinstalling Glimt into a different Python (a new virtual environment built from a
  different interpreter) counts as a new program: macOS will ask again.

## A device is unreachable (✗ in `glimt devices`)

- Is it powered? A bulb switched off at the wall is off the network.
- **Wi-Fi devices:** the address may have changed. Glimt notices and finds the new
  one by itself within a minute or two; `glimt restart` forces it.
- **Tuya devices on `cloud`:** some (many air conditioners, anything infrared) have no
  local protocol and always go through Tuya's cloud. That is expected.
- **"Sign in again":** the cloud link expired. `glimt setup tuya` and re-link.

## The Bluetooth strip works in Glimt but not in its phone app (or the reverse)

These strips accept one connection at a time, and Glimt keeps its connection open
(that is what makes it fast). Either close the phone app, or release the strip between
commands at the cost of a slower first command:

```toml
[devices.strip]
keepalive = false
```

## Cues fire but nothing lights up

`glimt doctor` prints "cues will show on: …". If that list is empty you have no
colour-capable light; name the devices to use:

```toml
[signals]
targets = ["desk-plug"]
```

If the lights are right but nothing happens at night, check `[signals.quiet]`.

## A light stayed in a cue's colour

`glimt clear` ends any cue and puts every borrowed light back. If a light could not
be reached at the moment it was due to go back (switched off at the wall, out of
Bluetooth range), Glimt keeps trying for about an hour and then stops; the log says
which light. Once the light is reachable, set it how you want it.

Glimt also puts the lights back when the daemon stops, so `glimt restart` during
a held cue does not leave the room amber.

## Hooks do not fire

- `glimt doctor` shows whether hooks are installed for each agent.
- Turns shorter than `[hooks] min_seconds` (20 by default) are quiet on purpose.
- Codex needs you to trust new hooks once; start it interactively and accept.
- Restart the agent after `glimt connect`: running sessions keep their old settings.

## The agent does not see my new device

The MCP server reads the device list when it starts. Restart the agent session.

## "A Glimt login service already runs from …"

There is one login service per user account. A second `GLIMT_HOME` runs with
`glimt start` instead and leaves the service alone.

## More detail in the log

```toml
[daemon]
debug = ["glimt"]       # or a specific logger: "glimt.bridge.tuya", "pyhap"
```

Bridge debug logs can include device payloads. Read them before pasting them anywhere.

## Start again from nothing

```bash
glimt disconnect && glimt service uninstall
mv ~/.glimt ~/.glimt.old
```
