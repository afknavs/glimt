# Apple Home

Glimt can present every device to the Home app, which also gives you Siri, Control
Centre, Shortcuts and automations. It runs inside the daemon, so Home is told about a
change the moment Glimt learns of it instead of on a polling timer.

```bash
pip install 'glimt[homekit]'      # already included in glimt[all]
```

```toml
[homekit]
enabled = true
name = "Glimt"        # the bridge's name in Home
# port = 51826
```

```bash
glimt restart
glimt homekit         # prints the pairing code, or the pairing status
```

In the Home app: **+ › Add Accessory › More Options**, choose the bridge, enter the code.

## At home and away

Glimt is a HomeKit *bridge*, not a home hub. The Home app reaches it directly while your
phone is on the same Wi-Fi as the computer running Glimt. On mobile data, or on another
network, Home shows **No Response** unless there is a HomePod or Apple TV in the house:
that is how Apple reaches every HomeKit accessory from outside, and nothing in Glimt can
replace it. Without one, use an agent with [remote access](remote.md) when you are out.

## What appears

| Device | In Home |
| --- | --- |
| a light | Lightbulb, with brightness and colour if it has them |
| an outlet / switch / fan | Outlet / Switch / Fan |
| a climate device with `power` and `temperature` | Thermostat (Off / Cool) |
| a device with only actions | nothing by default; expose actions as switches (below) |

## Options

```toml
[homekit.devices]
computer = false                       # hide a device
ac = { require = { mode = "cool" } }   # refuse "on" unless this state holds, and show a fault instead
desk = { aid = 4 }                     # pin the accessory id (see below)

[[homekit.switches]]                   # a momentary button: springs back off by itself
name = "Fan faster"
device = "fan"
on = "faster"

[[homekit.switches]]                   # a two-way switch
name = "AC rapid cool"
device = "ac"
on = "turbo_on"
off = "turbo_off"
```

## Things worth knowing

- **Accessory ids are permanent.** Home remembers rooms, names and automations by
  accessory id. Glimt assigns each device an id once and stores it in
  `~/.glimt/homekit-aids.json`, so adding or removing devices never shuffles the
  others. Pin an id with `aid` only when moving an existing pairing over from another
  bridge.
- **The pairing lives in `~/.glimt/secrets/homekit.state`.** Back it up with the
  rest of `~/.glimt` and a reinstall keeps your Home setup.
- **Home only listens while it is looking.** The Home app on a Mac or phone holds a
  live subscription while it is open in front of you; in the background it reconnects
  on demand, so a tile can be a few seconds stale until you look at it. A home hub
  (Apple TV, HomePod) keeps a permanent connection. `glimt status` shows how many
  controllers are connected and listening.
- **Devices that cannot report** (infrared, cheap Bluetooth) show what Glimt last
  sent them.
- A command from Home that fails marks the tile with a fault and snaps it back to the
  real state instead of pretending it worked.
