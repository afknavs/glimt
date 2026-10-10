# HTTP API

The daemon serves a small JSON API on `http://127.0.0.1:18770`. It is the only
interface: the CLI, the MCP server, the apps and Apple Home all use it or the same
in-process calls.

- **Local only.** It binds to `127.0.0.1` and rejects any other `Host` header.
- **Token required.** Send `Authorization: Bearer <contents of ~/.glimt/token>`.

```bash
TOKEN=$(cat ~/.glimt/token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18770/v1/devices
```

## Endpoints

| Method and path | Body | Returns |
| --- | --- | --- |
| `GET /v1/health` | | version, process id, uptime, each bridge's error if any, Apple Home status, mute, remote access, and `cli`: the command that runs this Glimt |
| `GET /v1/devices` | | `{"devices": [...], "signal": <active cue or null>}` |
| `GET /v1/devices/{id}` | | one device |
| `POST /v1/devices/{id}` | changes, e.g. `{"power": true, "color": "red"}` | `{"ok": true, "state": {...}, "ms": 31}` |
| `POST /v1/devices/{id}/actions/{action}` | optional arguments | `{"ok": true, "ms": 12}` |
| `POST /v1/refresh` | | forces a fresh read of every device |
| `POST /v1/signal` | a cue (below) | `{"ok": true, "id": 7, "targets": ["strip"]}`; returns at once |
| `DELETE /v1/signal` | | `{"ok": true, "cleared": true}` |
| `GET /v1/signals` | | the active cue, the last ten (including any skipped while muted), every defined cue, and `mute` |
| `POST /v1/mute` | `{"minutes": 60}`, or nothing for "until unmuted" | `{"ok": true, "mute": <unix time>}`; also ends a held cue |
| `DELETE /v1/mute` | | `{"ok": true, "mute": null}` |
| `GET /v1/remote` | | [remote access](remote.md): `enabled`, the public `endpoint`, connected `apps`, and sign-ins `pending` your approval |
| `POST /v1/remote/approve` | `{"code": "612645"}` | lets in the app whose sign-in page shows that code |
| `POST /v1/remote/deny` | `{"code": "612645"}` | declines it |
| `DELETE /v1/remote/apps/{name or id}` | | disconnects an app and discards its tokens |
| `GET /v1/events` | | a server-sent event stream (below) |
| `GET /` | | the dashboard |

### A device

```json
{
  "id": "strip", "name": "LED strip", "kind": "light", "bridge": "ble",
  "capabilities": {"power": {"type": "bool"}, "brightness": {"type": "int", "min": 0, "max": 100},
                   "color": {"type": "color"}},
  "actions": [],
  "state": {"power": true, "brightness": 50, "color": "#ff375f"},
  "assumed": true, "online": true, "route": "bluetooth", "updated": 1791559709.5
}
```

`assumed` means the device cannot report, so `state` is what was last sent. Values are
always in the common vocabulary: booleans, 0–100, `#rrggbb`.

### A cue

```json
{"name": "attention", "message": "Need a decision", "source": "ci"}
```

or fully custom:

```json
{"color": "#00c7be", "pattern": "flash", "count": 2, "sound": "info", "hold": 0, "urgent": false}
```

`pattern` is `pulse`, `flash`, `breathe` or `solid`. `hold` is seconds to stay lit;
`-1` means until cleared. `targets` may list device ids to use instead of the default.

### Events

`GET /v1/events` stays open and sends one JSON object per event:

```
data: {"type": "hello", "devices": [...], "signal": null, "mute": null, "remote": {...}}

data: {"type": "device", "device": {...}}

data: {"type": "signal", "signal": {"id": 7, "name": "done", ...}}

data: {"type": "signal", "signal": null}

data: {"type": "mute", "mute": 1791566400.0}

data: {"type": "remote", "remote": {"enabled": true, "apps": [...], "pending": [...]}}
```

A `device` event is sent whenever a device's state, reachability or error changes,
whoever caused it. That is how the menu bar app and dashboard stay live without polling.
A `remote` event is sent when an app asks to sign in, is approved or is disconnected.

These endpoints are the local API. The remote listener is a different port with a
different, much smaller surface: see [remote access](remote.md).

## Errors

| Status | Meaning |
| --- | --- |
| 401 | missing or wrong token |
| 403 | the `Host` header was not `127.0.0.1` or `localhost` |
| 404 | no such device; the message lists the ones that exist |
| 422 | invalid setting or cue; the message says what is allowed |
| 502 | the device or its bridge could not carry out the command |

Every error body is `{"error": "a sentence a person can act on"}`.

## From Python

```python
from glimt import client

client.request("POST", "/v1/signal", {"name": "done", "message": "Export finished"})
client.request("POST", "/v1/devices/desk", {"power": True})
```

`glimt.client` uses only the standard library, reads the token and port for you,
and starts the daemon if it is not running.
