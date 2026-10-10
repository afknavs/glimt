# Connecting agents

```bash
glimt connect            # every supported agent found on your PATH
glimt connect claude     # or just one
glimt disconnect         # undo
glimt agents             # which agents are connected
```

`glimt setup` offers the same thing at the end, and the macOS menu bar app lists
them under ⋯ › Agents That Cue You.

`connect` does two independent things. You can use either without the other. Agents that
do not run on this computer (ChatGPT, a dot) connect differently: see
[remote access](remote.md).

## 1. Lifecycle hooks: cues with no model in the loop

Claude Code and Codex run a command on events such as "the turn ended". Glimt
registers `glimt hook` for four of them:

| Event | What Glimt does |
| --- | --- |
| `UserPromptSubmit` | You are back: clear any held cue, start the turn clock. |
| `Stop` | `done`, but only if the turn ran longer than `min_seconds`. A quick answer while you were watching stays quiet. |
| `Notification` (Claude Code), `PermissionRequest` (Codex) | `attention`: the agent is blocked on you. The lights stay amber. |
| `PostToolUse` | You approved and the tool ran: end the `attention` cue. Does nothing, and costs nothing, when no cue is waiting. |

Each cue carries the agent's name, so the menu bar app can say "Codex needs you" rather
than just turning amber. The agent does not choose to cue you, spends no tokens on it, and cannot forget. The
hook takes about 50 ms and never fails or prints, so it cannot break a session; if the
daemon is not running it does nothing.

```toml
[hooks]
min_seconds = 20     # turns shorter than this never cue
idle = false         # true: also cue when the agent has merely been idle for a while
```

Where it is written, and how to do it by hand:

| Agent | File | By hand |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `glimt hooks show claude` prints the JSON to merge |
| Codex | `~/.codex/hooks.json` (or `<project>/.codex/hooks.json`) | `glimt hooks show codex` |

Existing hooks are left in place. Before each change the file is copied next to itself
as `*.before-glimt`, and the new version replaces the old in one step, so an
interrupted install cannot leave half a settings file. Codex asks you to trust new hooks
the next time it starts.

## 2. MCP tools: when the agent should decide

```bash
claude mcp add --scope user glimt -- glimt mcp
codex mcp add glimt -- glimt mcp
```

For other MCP clients (Claude Desktop, Cursor, …):

```json
{ "mcpServers": { "glimt": { "command": "glimt", "args": ["mcp"] } } }
```

Use the full path from `which glimt` if the client does not inherit your shell's PATH.

| Tool | What it does |
| --- | --- |
| `signal(name, message, color, pattern, count, sound, hold_seconds)` | Fire a cue. Returns immediately. |
| `clear_signal()` | End a held cue and restore the lights. |
| `control(device, power, brightness, color, temperature, settings)` | Change a device. |
| `action(device, action)` | Run a momentary action, such as `fan` → `faster`. |
| `devices()` | Current state of everything. Rarely needed. |

The server is built to cost as little as possible:

- **No discovery round trip.** Your devices, with what each can do, are written into the
  `control` tool's description when the server starts. The agent already knows the room.
- **No verification round trip.** Replies read `done: desk power=off (acknowledged in
  31 ms)`, so the agent has no reason to call `devices()` to check.
- **Five tools, short descriptions.** Every tool definition is paid for on every
  request the agent makes.

Restart the agent after adding or renaming devices so it sees the new list.

### Prompts that work well

With hooks installed you need no prompt at all for "tell me when you are done". For
cues *inside* a task:

- "Run the full test suite. Signal `error` with the failing test name if anything fails."
- "This migration needs my sign-off before the last step. When you get there, signal
  `attention` and wait."
- "Set a `working` signal while the benchmark runs and clear it afterwards."

## Any other agent, script or tool

Anything that can run a shell command or make an HTTP request can cue you:

```bash
glimt signal done -q -m "Backup finished"
```

See [signals.md](signals.md) for recipes and [api.md](api.md) for the HTTP API.

## Why an agent turn still takes seconds

The MCP call itself takes 40 to 90 ms. An agent turn that uses it takes as long as the
model's round trips: one to decide to call the tool, one to read the result and answer,
often more if the agent consults its notes first. With a large context that is several
seconds each. Glimt removes its own share (see the three points above); the rest is
the model. For cues, hooks avoid the question entirely. Details in [latency.md](latency.md).
