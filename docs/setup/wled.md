# WLED

Addressable LED strips running [WLED](https://kno.wled.ge) firmware.

```bash
glimt setup wled
```

Setup looks for strips announcing themselves on your network, and asks for an address
if it finds none.

```toml
[bridges.wled]
# poll_seconds = 10               # how often to check for changes made elsewhere

[devices.strip]
name = "LED strip"
kind = "light"
bridge = "wled"
host = "192.168.1.70"
# transition_ms = 100             # short, so cue flashes stay crisp
# effects = { rainbow = 9 }       # optional: name the effect numbers you use
```

It can set `power`, `brightness` and `color`, plus `effect` when you list some.
Colour applies to the first segment.

## How it talks

WLED's local JSON API over one reused HTTP connection. Commands do not read first.
Changes made elsewhere (the WLED app, a button) are picked up by a background poll.

## If something is off

- **Unreachable:** check power and Wi-Fi, and open `http://<host>/` in a browser. Give
  the strip a fixed address in your router if it keeps changing.
- **Cues look smeared:** lower `transition_ms`.

This bridge is tested against a simulated strip. Reports from real hardware, good or
bad, are welcome.
