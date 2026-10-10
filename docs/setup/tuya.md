# Tuya / Smart Life

Most inexpensive Wi-Fi bulbs, plugs, power strips and air conditioners are Tuya devices
under another brand, set up with the Smart Life or Tuya Smart app.

```bash
pip install 'glimt[tuya]'     # included in glimt[all]
glimt setup tuya
```

Setup asks for the **User Code** from the app (Me › Settings › Account and Security ›
User Code), shows a QR code to scan with the app, then imports your devices and looks
for each one on your network.

## What that sign-in is

It is Tuya's own QR sign-in for local-control software, the same one Home Assistant's
Tuya integration uses; the app may show "Home Assistant" on the approval screen for
that reason. No password is typed anywhere and no Tuya developer account is needed.
The result (a cloud token and each device's local key) is stored in
`~/.glimt/secrets/tuya.json`, readable only by you.

## Local first

Each device that speaks Tuya's LAN protocol gets one encrypted connection that stays
open. Commands are a single packet on it (30–150 ms) and the device pushes its state
back on the same connection. `glimt devices` shows how each device is reached:

| Route | Meaning |
| --- | --- |
| `lan` | direct, on your network. Works without internet. |
| `cloud` | through Tuya's servers: the device has no usable local protocol (many air conditioners), or was not found on the network |

```toml
[bridges.tuya]
# cloud = false                 # never use the cloud; cloud-only devices will be unavailable

[devices.desk]
route = "lan"                   # auto (default) | lan | cloud, per device
```

## Device tables

`setup` writes these for you. `map` connects a Glimt capability to the Tuya status
code that carries it:

```toml
[devices.cupboard]
name = "Cupboard light"
kind = "light"
bridge = "tuya"
tuya_id = "bf0123456789abcdef"
map = { power = "switch_led", brightness = "bright_value_v2", color = "colour_data_v2" }

[devices.extension-1]             # one socket of a multi-socket strip
kind = "outlet"
bridge = "tuya"
tuya_id = "bf00000000000000"
map = { power = "switch_1" }
read = { watts = "cur_power", volts = "cur_voltage" }   # read-only values shown in state
scale = { watts = 0.1, volts = 0.1 }

[devices.ac]
kind = "climate"
bridge = "tuya"
tuya_id = "bf11111111111111"
map = { power = "switch", temperature = "temp_set", fan = "fan_speed_enum" }
values = { fan = { medium = "middle" } }                 # rename the device's own option names
read = { current_temperature = "temp_current" }
```

## Infrared remotes, and anything without a simple switch

A Tuya IR blaster controlling a fan or TV has no state and no local protocol. Make a
**Tap-to-Run** scene in Smart Life for each button you want, then attach the scenes as
actions:

```toml
[devices.fan]
name = "Fan"
kind = "fan"
bridge = "tuya"

[devices.fan.actions]
faster = "Increase fan speed"     # the scene's name in Smart Life
slower = "Decrease fan speed"
```

`glimt do fan faster`, or expose them to Apple Home as switches (see
[homekit.md](../homekit.md)).

## Verified, and not yet

On/off bulbs, multi-socket power strips with energy monitoring, one air conditioner
(cloud route) and IR scenes are used daily. Brightness and colour for Tuya bulbs follow
Tuya's documented encoding but have not been checked on a real colour bulb yet; a
`glimt bridge test tuya --write` result from one is a welcome issue or pull request.
