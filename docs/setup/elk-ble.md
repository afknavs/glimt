# Bluetooth LED strips (ELK-BLEDOM family)

The inexpensive Bluetooth strips sold with apps such as **Lotus Lamp X**, **duoCo
Strip** or **LED Lamp** nearly all speak the same protocol. They advertise names
starting with `ELK-`, `MELK-` or `LEDBLE`.

```bash
pip install 'glimt[ble]'      # included in glimt[all]
glimt setup elk-ble
```

Power the strip on and **close its phone app first**: these strips accept one
connection at a time. On macOS, allow Python to use Bluetooth when asked.

```toml
[bridges.ble]
type = "elk-ble"

[devices.strip]
name = "LED strip"
kind = "light"
bridge = "ble"
address = "AA:BB:CC:DD:EE:FF"       # on macOS this is a UUID that is specific to your Mac
ble_name = "MELK-OA10 A0"           # optional: refuse to talk to anything else at that address
# keepalive = false                 # release the connection when idle (phone app can connect; first command is slower)
# effects = { "color-flow" = 133 }  # optional: name the effect numbers your strip has
```

It can set `power`, `brightness`, `color`, `effect` and `speed`.

## Why it is fast here and slow elsewhere

The usual approach scans, connects, writes and disconnects for every command, which
takes about three seconds. Glimt connects once and stays connected, so a colour
change is one small write on a link that is already up: about 95 ms.

## Good to know

- The strip **cannot report its state**. What Glimt shows is the last thing it
  sent, marked `~`. If you change the strip with its remote, Glimt cannot know.
- A cue restores the strip to that last-sent state, which is remembered across restarts.
- If the strip is switched off at the plug, Glimt keeps retrying quietly and
  reconnects when it returns.
- `MELK` controllers need a short handshake after each connection; that is handled.
