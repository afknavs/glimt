# Philips Hue

```bash
glimt setup hue
```

Setup finds your Hue bridge (or asks for its address), asks you to press the round
button on top of it, and then lists your lights. The app key it receives is stored in
`~/.glimt/secrets/hue.json`, readable only by you.

```toml
[bridges.hue]
host = "192.168.1.71"

[devices.desk]
name = "Desk lamp"
kind = "light"
bridge = "hue"
light_id = "0f1e2d3c-…"                             # the light's id on the bridge
capabilities = ["power", "brightness", "color"]     # white-only bulbs: leave out "color"
# transition_ms = 100                               # how quickly changes fade
```

## How it talks

Hue API v2 on your local network. State is not polled: the bridge pushes every change
(including ones made with the Hue app or a wall dimmer) over an event stream, and
Glimt reconnects to it by itself.

The bridge uses a self-signed certificate, so the connection is encrypted but the
certificate is not verified. Traffic stays on your own network.

## If something is off

- **"Press the round button…"** again: the button press is only valid for about thirty
  seconds.
- **A light shows as not found:** it was removed or re-paired on the bridge and has a
  new id. Run `glimt setup hue` again and add it.
- **Everything unreachable:** check the bridge has power and the same address
  (give it a fixed address in your router if it keeps changing).

This bridge is tested against a simulated Hue bridge. Reports from real hardware, good
or bad, are welcome.
