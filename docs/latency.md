# Why it was slow, and what changed

Glimt started as a question about my own setup: the phone app switches a light
instantly, Home Assistant used to as well, so why does asking an agent take ten seconds?

The answer was two separate problems stacked on top of each other. This page records
how each was found, because the method is reusable and the conclusions apply to almost
any "agent controls a device" project.

## Measure the layers separately

One real agent turn, timestamped event by event:

| | Time |
| --- | --- |
| Whole agent turn ("turn the desk light off") | 19.75 s |
| … of which the device call | 0.46 s |
| … of which model round trips (3 of them, ~79k input tokens) | the other 19 s |

So 98% of the wait was not the light. But the device call was still far slower than it
should be, and for cues (many small writes in a row) that matters, so both were fixed.

## Problem 1: the device path did everything from scratch, every time

| Device | What the first version did per command | Cost |
| --- | --- | --- |
| Bluetooth strip | scan for the device, connect, send a wake-up handshake with two one-second pauses, write, disconnect | 3.0–3.2 s |
| Wi-Fi bulb (Tuya) | open TCP, negotiate a session key, read current state, write, read back up to four times to confirm | 1.5–1.8 s |
| Read all devices | one fresh connection per device, in turn | 0.3–1.0 s (6.8 s when one was asleep) |

None of that work is needed per command. What Glimt does instead:

1. **One long-lived connection per device.** A socket per Wi-Fi device kept warm with a
   heartbeat; one BLE connection per strip, with the handshake done once per connection
   rather than once per command.
2. **No read-before-write.** State is already in memory.
3. **No polling for confirmation.** Devices push their state on the open connection, so
   "what is the lamp doing?" is a dictionary lookup, and a change made at the wall
   switch shows up by itself.
4. **One acknowledgement per command, not per packet.** A three-frame Bluetooth command
   waits only for the last frame.

Same room, same hardware, measured with `glimt bridge test --write` and repeated
manual timings:

| | Before | After |
| --- | --- | --- |
| Read every device's state | 300–1000 ms | 2–5 ms |
| Wi-Fi bulb on/off | 1.5–1.8 s | 50–150 ms |
| Smart plug on/off | 180–270 ms | ~30 ms |
| Bluetooth strip colour | 3.0–3.2 s | ~95 ms |
| Air conditioner (cloud only, no LAN protocol) | ~500 ms | ~60 ms |
| `glimt signal done`, process start to exit | n/a | 40–70 ms |

The air conditioner number is a cached, already-authenticated cloud session instead of
a new one per command.

## Problem 2: the agent path

Three model round trips for one switch:

1. read the skill file that explained the tools,
2. call the tool,
3. read the result and answer.

Each one re-sends the whole context. With many tools and plugins installed that was
tens of thousands of tokens and four to eight seconds per trip.

What Glimt changes:

- **No skill file.** Everything an agent needs is in five short tool descriptions.
- **No "list devices" call.** The device catalogue is part of the `control` tool's
  description.
- **No "verify" call.** The reply states what the device acknowledged.

Re-measured after the rebuild, on the same agent: the device call is 40 to 90 ms. The
turn is still roughly 15 to 20 seconds on that particular setup, because the agent
consults its own memory notes before acting and each of its round trips is slow. That
part belongs to the agent and its configuration, not to the device layer, and no
smart-home code can remove it.

## The conclusion that shaped the project

If the model is the slow part, the fast path is the one with no model in it.

- For **cues**, the agent's harness fires them through lifecycle hooks: zero round
  trips, about 50 ms from "turn ended" to "lamp is green". See [connecting-agents.md](connecting-agents.md).
- For **control**, an agent tool call costs what the model costs. The menu bar app, the
  dashboard, Apple Home and the CLI all go straight to the daemon and feel instant.

That is also why "tell a chatbot to switch on the lamp" is the least interesting thing
Glimt can do, and "the room tells you your agent needs you" is the point.

## Check your own setup

```bash
glimt bridge test tuya --write     # re-sends each device's current value and times it
glimt devices                      # state, and whether each device is on lan / cloud / bluetooth
time glimt signal info             # the whole CLI round trip
```

A bridge whose `apply()` takes over a second fails the conformance check. If you write
a bridge, [bridges.md](bridges.md) lists the four rules above as requirements.
