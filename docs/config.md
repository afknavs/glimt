# Configuration

Everything lives in `~/.glimt/` (set `GLIMT_HOME` to move it):

| Path | What |
| --- | --- |
| `config.toml` | names, mappings, preferences. No credentials, so it is safe to share. |
| `secrets/` | credentials, one file per bridge, readable only by you |
| `token` | the local API token |
| `bridges/` | bridges you (or your agent) wrote |
| `daemon.log` | the daemon's log |
| `state.json` | last-sent state of devices that cannot report |
| `mute` | present while cues are muted (`glimt mute`) |

`glimt setup` writes `config.toml` for you by appending new tables; it never
reorders or reformats what you wrote. After editing by hand: `glimt restart`.

## A complete example

```toml
[daemon]
port = 18770                      # default; GLIMT_PORT overrides it
# debug = ["glimt", "pyhap"]   # verbose logs for just these loggers

# ---- bridges: one table per connection to some hardware ---------------------------

[bridges.tuya]                    # the table name is the bridge's name; type defaults to it
# cloud = false                   # never use the Tuya cloud

[bridges.ble]
type = "elk-ble"                  # name and type differ here

[bridges.sound]

# ---- devices -----------------------------------------------------------------------

[devices.desk]                    # the id you type: glimt on desk
name = "Desk light"               # shown in the apps and to agents
kind = "light"                    # light | outlet | switch | climate | fan | speaker | other
bridge = "tuya"                   # which [bridges.*] table reaches it
icon = "lamp.desk.fill"           # optional SF Symbol for the macOS app
signal = false                    # optional: may cues use it? default: colour lights only
tuya_id = "bf0123456789abcdef"    # everything else is specific to the bridge
map = { power = "switch_1" }

[devices.strip]
name = "LED strip"
kind = "light"
bridge = "ble"
address = "AA:BB:CC:DD:EE:FF"

[devices.computer]
name = "This computer"
kind = "speaker"
bridge = "sound"

# ---- cues ---------------------------------------------------------------------------

[signals]
# targets = ["strip"]             # use exactly these lights for cues
# speaker = false                 # silent cues

[signals.quiet]
from = "23:00"
to = "07:30"

[signals.deploy]                  # a custom cue: glimt signal deploy
color = "purple"
pattern = "flash"
count = 3

# ---- agent hooks --------------------------------------------------------------------

[hooks]
min_seconds = 20                  # turns shorter than this never cue
idle = false

# ---- Apple Home ---------------------------------------------------------------------

[homekit]
enabled = true
name = "Glimt"

[homekit.devices]
computer = false                  # hide a device from Home
ac = { require = { mode = "cool" } }

[[homekit.switches]]              # a device action as a Home switch
name = "Fan faster"
device = "fan"
on = "faster"

# ---- Remote access (ChatGPT, dots, cloud agents) --------------------------------------
# Written by `glimt remote enable`; off unless present and enabled.

[remote]
enabled = true
url = "https://your-computer.your-tailnet.ts.net"   # the public https address, nothing after it
control = true                    # false: apps may cue you but not control devices
```

Per-bridge device keys are documented at the top of each bridge's source file and in
the [device guides](setup/). Cue options are in [signals.md](signals.md), Apple Home in
[homekit.md](homekit.md), remote access in [remote.md](remote.md).

## Environment variables

| Variable | Effect |
| --- | --- |
| `GLIMT_HOME` | use this directory instead of `~/.glimt` |
| `GLIMT_PORT` | daemon port, overriding `[daemon] port` |
| `GLIMT_NO_AUTOSTART` | CLI commands fail instead of starting the daemon |

A second `GLIMT_HOME` (for trying something out) runs happily next to your main
one on another port. It cannot replace or stop your login service.
