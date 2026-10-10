# Writing a bridge

A **bridge** connects one family of hardware (or one hub, or one cloud) to Glimt.
This page is the whole contract. It is written so that a person *or an AI coding agent*
can add support for a device Glimt has never heard of, and have it show up everywhere
(CLI, MCP tools, signals, the menu bar app, the dashboard, Apple Home) without touching
any of those.

> **If you are an AI agent** asked to "make Glimt work with X": read this page top to
> bottom, then follow [The ten-minute path](#the-ten-minute-path). Do not edit files
> outside your new bridge. Do not print credentials. Do not switch devices on or off to
> "see if it works" unless the user said you may: `glimt bridge test` proves a bridge
> without changing anything in the room.

## The ten-minute path

```bash
glimt bridge new acme        # writes ~/.glimt/bridges/acme.py from an annotated template
$EDITOR ~/.glimt/bridges/acme.py
$EDITOR ~/.glimt/config.toml # add [bridges.acme] and one [devices.*] that uses it
glimt bridge test acme       # read-only conformance checks
glimt bridge test acme --write   # also re-sends each device's current value, and times it
glimt restart                # the daemon picks it up; the device is now everywhere
```

A bridge is one Python file. No packaging, no registration: a file named
`~/.glimt/bridges/<type>.py` containing a `Bridge` subclass whose `type` matches is
enough. (`my-thing` lives in `my_thing.py`.)

## What you write

```python
from glimt.bridges.base import Bridge, BridgeError
from glimt.model import Cap, Device

class AcmeBridge(Bridge):
    type = "acme"                       # what users write in [bridges.*] type = "..."

    def devices(self) -> list[Device]: ...          # required: declare devices, no I/O
    async def apply(self, device, changes): ...     # required: send a change, return fast
    async def start(self): ...                      # optional: connect, report first state
    async def stop(self): ...                       # optional: disconnect
    async def act(self, device, action, args): ...  # optional: momentary actions
    async def refresh(self, device): ...            # optional: force a fresh read
    @classmethod
    async def discover(cls, config): ...            # optional: find devices for `glimt setup`
```

Three things matter; the hub does everything else (validation, optimistic state,
per-device ordering, timeouts, signals, the API, MCP, HomeKit, the apps):

| You | When | Rule |
| --- | --- | --- |
| `devices()` | once, before `start()` | Build devices from config. **No network, no disk waits.** |
| `apply(device, changes)` | every command | Send it and return. **Do not wait for or poll for confirmation.** |
| `self.report(id, state, online=…, route=…)` | whenever real state is known | Call from anywhere, any thread. |

## The device model

Every device is described in one vocabulary so that every surface can render it and
every agent can drive it without knowing the brand.

```python
self.make_device(device_id, cfg,
    capabilities={"power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()},
    actions=["faster", "slower"],   # momentary commands that have no state
    assumed=False)                  # True if the hardware can never report its state
```

### Kinds

`light`, `outlet`, `switch`, `climate`, `fan`, `speaker`, `other`. The user sets `kind`
in config; it picks the icon and the layout in the apps. Do not invent new kinds.

### Capabilities

A capability is a value that can be **set**, and that appears in `state` under the same
name. Use these names and units whenever the hardware has the concept. This is what
makes the experience identical across brands:

| Name | Spec | Value in `changes` / `state` | Rendered as |
| --- | --- | --- | --- |
| `power` | `Cap.bool()` | `True` / `False` | the tile's on/off toggle |
| `brightness` | `Cap.percent()` | `0`–`100` (convert from 0–255, 10–1000, …) | slider |
| `color` | `Cap.color()` | `"#rrggbb"`, lower case | swatches + colour wheel |
| `temperature` | `Cap.integer(16, 30, "°C")` | target, integer | − / + stepper |
| `fan` | `Cap.enum(["auto", "low", "medium", "high"])` | one option | segmented bars |
| `mode` | `Cap.enum([...])` | one option | menu |
| `effect` | `Cap.enum([...])` | one option | menu |
| anything else | `Cap.bool()` / `Cap.integer(a, b, unit)` / `Cap.enum([...])` | — | generic toggle / stepper / menu |

Rules:

- **Declare only what the hardware can really do.** A plain on/off bulb has `power`
  and nothing else. Lying here breaks signals (which pick lights that have `color`).
- **Translate, do not leak.** If the hardware calls medium fan speed `"middle"`, your
  bridge maps it. `changes` and `state` only ever contain the common vocabulary.
- **Read-only values** (power draw, room temperature, battery) are not capabilities:
  just report them in state under a clear name such as `watts`, `current_temperature`,
  `battery`. They are displayed, and agents can read them, but nothing can set them.
- `changes` reaching `apply()` is **already validated** against your specs and
  normalised (`"on"` → `True`, `"red"` → `"#ff453a"`, numbers range-checked).

### Actions

Momentary commands with no state: `["faster", "slower"]` for an infrared fan,
`["play", "say"]` for a speaker. Implement `act(device, action, args)`. Use an action
instead of a capability when the hardware gives you a button, not a value.

### `assumed`

Infrared remotes and cheap Bluetooth strips cannot tell you their state. Declare
`assumed=True`: Glimt then shows "last sent" (marked `~`), remembers it across
restarts, and uses it to restore the device after a signal.

## The rules that make it fast

Glimt exists because "tell the lamp to turn red" should take 50 ms, not 3 seconds.
Almost every slow smart-home integration is slow for the same four reasons. Avoid them:

1. **Keep the connection open.** Connect in `start()`, keep it, reconnect in the
   background when it drops. Never connect-send-disconnect per command. (This alone took
   the Bluetooth strip from 3.1 s to 0.09 s and Tuya bulbs from 1.6 s to 0.05 s.)
2. **Never read before writing.** State is already in `device.state`; you do not need
   to ask the device what it is doing before telling it what to do.
3. **Never wait for confirmation in `apply()`.** Return when the command is sent (or
   acknowledged by the transport, if that is one cheap round trip). When the device
   later confirms, or pushes a change someone made at the wall switch, call
   `self.report(...)`. If the device has a push channel (WebSocket, MQTT, SSE,
   notifications), subscribe to it in `start()`; if it does not, poll slowly (30–60 s)
   in a background task.
4. **Never block the event loop.** For a blocking library use
   `await asyncio.to_thread(fn, ...)` or your own worker thread; `self.report()` is
   thread-safe.

The hub enforces the outer limits: `start()` has 30 s, `apply()` and `act()` have 8 s,
and commands to one device are run strictly in order. `glimt bridge test --write`
fails a bridge whose `apply()` takes more than one second.

## Reporting state

```python
self.report(device_id, {"power": True, "brightness": 40})   # values, common vocabulary
self.report(device_id, online=False, error="No reply on the LAN; is it plugged in?")
self.report(device_id, online=True, route="lan")            # route is a short free label
```

- Report at least once per device from `start()` (or from the listener it starts), so
  the device has real state. A device that is never reported shows as unknown.
- `route` tells the user how the device is being reached: `lan`, `cloud`, `bluetooth`,
  `hub`… Useful when you fall back from a fast path to a slow one.
- `error` is shown to people and agents. Write it for a person standing in the room:
  what is wrong, and what to try.
- After a successful `apply()` the hub records `changes` as the new state for you.
  Only `report()` afterwards if the device answered with something different.

## Errors

Raise `BridgeError("short human reason")` when a command cannot be sent. Anything else
you raise is caught, logged and shown as `TypeName: message`, which is uglier. Config
mistakes should name the table and key: `[devices.desk] needs remote_id = "..."`.

## Config and secrets

```toml
[bridges.acme]            # bridge-wide settings; type defaults to the table name
host = "192.168.1.50"

[devices.desk]            # one table per device
name = "Desk lamp"
kind = "light"
bridge = "acme"
remote_id = "3"           # whatever your bridge needs to address this device
```

- `self.config` is your `[bridges.*]` table; `self.device_configs` is
  `{device_id: cfg}` for the devices that name your bridge.
- **Credentials never go in `config.toml`.** Store them with
  `paths.write_private(paths.secrets_dir() / "acme.json", text)` (mode 0600) and read
  them back from there. A config file must always be safe to paste into a bug report.
- Never log tokens, keys or pairing codes, and never put them in an error message.

## Discovery (optional, but it is what makes setup feel seamless)

```python
@classmethod
async def discover(cls, config: dict) -> list[dict]:
    return [{"id": "desk", "name": "Desk lamp", "kind": "light", "remote_id": "3"}]
```

Return ready-to-write `[devices.*]` tables with a suggested `id`. `glimt setup <type>`
shows them to the user and appends the ones they keep to `config.toml`.

`config` is your `[bridges.*]` table as it stands (host, URL…) plus one extra key,
`name`: the name of that table. Use it to look up credentials you stored per bridge
(`secrets["<name>"]`), since a `classmethod` has no `self.name`. Return `[]` when you
cannot discover yet (no host, not linked); raise `BridgeError` when discovery itself fails.

## A complete small bridge

A light with a local HTTP API and no push channel, so changes made elsewhere are picked
up by a slow background poll:

```python
"""Acme bulbs over their local HTTP API.

    [bridges.acme]

    [devices.desk]
    bridge = "acme"
    kind = "light"
    host = "192.168.1.60"
"""
import asyncio
import httpx
from glimt.bridges.base import Bridge, BridgeError
from glimt.model import Cap, Device


class AcmeBridge(Bridge):
    type = "acme"

    def devices(self) -> list[Device]:
        return [self.make_device(device_id, cfg, capabilities={
            "power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color()})
            for device_id, cfg in self.device_configs.items()]

    async def start(self) -> None:
        self.http = httpx.AsyncClient(timeout=3)          # one client: connections stay open
        self.poller = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        self.poller.cancel()
        await asyncio.gather(self.poller, return_exceptions=True)   # let it finish before closing
        await self.http.aclose()

    async def _poll(self) -> None:
        while True:
            for device_id, cfg in self.device_configs.items():
                try:
                    raw = (await self.http.get(f"http://{cfg['host']}/state")).json()
                    self.report(device_id, {"power": raw["on"], "brightness": round(raw["bri"] / 2.55),
                                            "color": "#%02x%02x%02x" % tuple(raw["rgb"])},
                                online=True, route="lan")
                except (httpx.HTTPError, KeyError, ValueError):
                    self.report(device_id, online=False, error="No reply; is it plugged in and on Wi-Fi?")
            await asyncio.sleep(30)

    async def apply(self, device: Device, changes: dict) -> None:
        body = {}
        if "power" in changes:
            body["on"] = changes["power"]
        if "brightness" in changes:
            body["bri"] = round(changes["brightness"] * 2.55)
        if "color" in changes:
            body["rgb"] = [int(changes["color"][i:i + 2], 16) for i in (1, 3, 5)]
        try:
            (await self.http.post(f"http://{device.config['host']}/state", json=body)).raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(f"{device.name} did not accept the command ({type(exc).__name__})") from None
```

`httpx` is always available (Glimt depends on it). If your bridge needs another
package, import it inside the method that uses it and set `extra = "name"` on the class
so the error tells the user what to install.

The built-in bridges are worked examples at increasing difficulty:

| File | Shows |
| --- | --- |
| `bridges/virtual.py` | the minimum: 50 lines |
| `bridges/sound.py` | an action-only device |
| `bridges/elk_ble.py` | a persistent Bluetooth link, an `assumed` device, `discover()` |
| `bridges/wled.py` | local HTTP with a background poll |
| `bridges/hue.py` | HTTPS, state pushed over an event stream, pairing, `discover()` |
| `bridges/homeassistant.py` | one bridge, many device kinds, commands and push on one WebSocket |
| `bridges/tuya.py` | LAN first with cloud fallback, worker threads, IP rediscovery |

## What `glimt bridge test` checks

Read-only:

- the bridge loads and is a `Bridge` subclass with the right `type`
- `devices()` returns exactly the configured devices, in under 500 ms
- every device has a valid kind and well-formed capability specs, and can be controlled
  somehow (a capability or an action)
- `start()` succeeds
- every non-assumed device reports state within a few seconds of `start()`
- `stop()` returns promptly

With `--write` (re-sends the value each device already has, so nothing visibly changes):

- `apply()` works and returns in under one second
- invalid settings never reach the bridge

The same checks run in CI against the simulated bridge (`tests/test_conformance.py`),
and you can call them from your own tests:

```python
from glimt import conformance
results = await conformance.check("acme", config, write=True)
assert all(result.ok for result in results), [str(r) for r in results if not r.ok]
```

## Sharing a bridge

- **Just for you:** leave it in `~/.glimt/bridges/`.
- **In Glimt itself:** add `src/glimt/bridges/<type>.py`, register it in
  `BUILTIN` in `bridges/__init__.py`, add a test with a fake device, add a short page
  under `docs/setup/`, open a pull request.
- **As your own package:** expose an entry point and `pip install` it next to Glimt:

  ```toml
  [project.entry-points."glimt.bridges"]
  acme = "glimt_acme:AcmeBridge"
  ```

## Checklist before you call it done

- [ ] `glimt bridge test <type> --write` passes
- [ ] connection opened once in `start()`, reused by every `apply()`
- [ ] no read-before-write, no sleep, no polling inside `apply()`
- [ ] state changes made outside Glimt (wall switch, phone app) show up within a
      minute, or instantly if the hardware can push
- [ ] capabilities use the common names and units; nothing brand-specific leaks out
- [ ] unplugging the device produces `online=False` and a helpful `error`, not a crash
- [ ] no credentials in `config.toml`, logs or error messages
- [ ] the module docstring shows the config a user needs
