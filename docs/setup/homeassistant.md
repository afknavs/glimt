# Home Assistant

If Home Assistant already controls your devices, this one bridge brings all of them
into Glimt: lights, switches, fans and climate devices, plus scenes, scripts and
buttons as actions.

```bash
pip install 'glimt[homeassistant]'   # included in glimt[all]
glimt setup homeassistant
```

Setup asks for your Home Assistant address and a **long-lived access token** (in Home
Assistant: your profile › Security › Long-lived access tokens › Create token), then
lists the entities it found so you can pick the ones you want. The token is stored in
`~/.glimt/secrets/homeassistant.json`, readable only by you, never in `config.toml`.

```toml
[bridges.homeassistant]
url = "http://homeassistant.local:8123"

[devices.desk]
name = "Desk lamp"
kind = "light"
bridge = "homeassistant"
entity = "light.desk"
capabilities = ["power", "brightness", "color"]   # what this entity can really do

[devices.desk.actions]
movie = "scene.movie"            # a scene, script or button entity: glimt do desk movie

[devices.ac]
name = "Air conditioner"
kind = "climate"
bridge = "homeassistant"
entity = "climate.bedroom"
capabilities = ["power", "temperature", "fan"]
fan_modes = ["auto", "low", "high"]
```

Defaults when `capabilities` is left out: lights get power and brightness; switches,
input booleans and fans get power; climate entities get power and target temperature.

## How it talks

One authenticated WebSocket carries everything: the first read of all states, every
later state change pushed by Home Assistant, and Glimt's own commands. A command
returns as soon as it is sent; the state change that follows is the confirmation.
It reconnects by itself, and devices show as unreachable while it is down.

## If something is off

- **Everything unreachable:** check the address, that the token is still valid, and
  that `websockets` is installed (`glimt doctor` says which).
- **One entity unreachable:** it is `unavailable` in Home Assistant itself, or the
  `entity` id in config is wrong.
- **HTTPS:** use an `https://` address with a certificate this computer trusts.

This bridge is tested against a simulated Home Assistant server. Reports from real
installations, good or bad, are welcome.
