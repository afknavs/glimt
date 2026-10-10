# Cues

A cue (the code calls it a *signal*) borrows your lights for a moment to tell you
something, then puts them back exactly as they were.

```bash
glimt signal done
glimt signal attention -m "Need a decision on the migration"
glimt signal --color pink --pattern breathe --count 3
glimt clear                      # end a held cue now
glimt run -- pytest -q           # cue done or error when a command exits
```

## Built in

| Name | Colour | Pattern | Sound | Stays lit |
| --- | --- | --- | --- | --- |
| `done` | green `#30d158` | 2 pulses | done | no |
| `attention` | amber `#ff9f0a` | 2 breaths, then solid | attention | 5 minutes, or until cleared |
| `error` | red `#ff453a` | 3 flashes | error | no |
| `info` | blue `#0a84ff` | 1 pulse | none | no |
| `working` | indigo `#5e5ce6` at 25% | solid | none | until cleared (2 hours at most) |

A newer cue replaces the one that is showing. `glimt clear`, or any cue that does not
hold, ends a held one.

## What "puts them back" means

- Before the first cue touches a light, its power, colour, brightness and effect are
  saved. When the cue ends, that state is restored. A light that was off turns off
  again, with its old colour put back first so the next manual "on" is not cue-coloured.
- If a second cue interrupts the first, the room still returns to how it looked before
  the *first* one.
- **Your hand wins.** If you change a light through Glimt while a cue is showing
  (the menu bar app, the dashboard, the command line, Apple Home and Siri, or an agent),
  Glimt lets go of that light and does not restore over your choice. A change made
  somewhere Glimt cannot see while the cue is on screen (the manufacturer's own app,
  or a remote for a device that cannot report) is not noticed, and the light goes back
  to how it was before the cue.
- A light that was out of reach for the whole cue never showed it, so nothing is done
  to it afterwards.
- If a light cannot be reached when it is time to put it back, Glimt keeps trying
  for about an hour (after 5 seconds, then at growing intervals), then gives up and
  says so in the log.
- Devices that cannot report their state (infrared, most cheap Bluetooth strips) are
  restored to the last thing Glimt sent them. That is remembered across restarts.

## Your own cues

One-off, from the command line or an agent:

```bash
glimt signal --color "#00c7be" --pattern flash --count 2 --sound info -m "Deploy is live"
```

Named, in `~/.glimt/config.toml`:

```toml
[signals.deploy]
color = "purple"          # a name or #rrggbb
pattern = "flash"         # pulse | flash | breathe | solid
count = 3                 # 1 to 10
sound = "done"            # done | attention | error | info | a system sound name | a file path
hold = 0                  # seconds to stay lit afterwards; -1 = until cleared
brightness = 100          # 1 to 100

[signals.done]            # built-in cues can be adjusted the same way
count = 1
sound = false
```

Then `glimt signal deploy`.

## Which lights are used

By default: every device of kind `light` that has a `color` capability. Change that
per device or globally:

```toml
[devices.bedside]
signal = false            # never use this one

[devices.desk-plug]
signal = true             # use it even though it is on/off only: it blinks instead

[signals]
targets = ["strip"]       # or: use exactly these, and nothing else
speaker = "computer"      # which speaker plays the tone; false = silent
```

On/off-only devices get a slow blink (three at most, to spare the relay) and end in
the state they started in.

## Quiet hours

```toml
[signals.quiet]
from = "23:00"
to = "07:30"
```

Cues in that window are skipped and logged. `--urgent` (or `"urgent": true` over the
API) overrides it.

## Mute

For quiet right now rather than on a schedule:

```bash
glimt mute 60          # minutes
glimt mute             # until you say otherwise
glimt unmute
```

The bell in the menu bar app does the same. Muting also ends a cue that is holding the
lights, and it survives a restart. Cues that arrive while muted are still listed (in
`GET /v1/signals` and the menu bar app), so you can see what you missed. `--urgent`
cues ignore both mute and quiet hours.

## Recipes

```bash
# Any long command
glimt run -- npm run build

# CI: go do something else, come back when the lamp turns green
gh run watch && glimt signal done -m "CI passed" || glimt signal error -m "CI failed"

# A git hook (.git/hooks/post-merge)
glimt signal info -q -m "Merged"

# A long job with a "busy" glow
glimt signal working -m "Training"; python train.py; glimt signal done
```

`-q` makes the command print nothing and never fail, which is what you want inside
hooks and scripts: a missing lamp should not break a build.
