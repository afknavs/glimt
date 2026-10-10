# Remote access: ChatGPT, dots and other cloud agents

Claude Code and Codex run on your computer, so they reach Glimt directly and need
none of this. ChatGPT, an OpenAI dot, or Claude on the web run in someone else's cloud:
they cannot see `127.0.0.1`. Remote access gives them a way in, so a long-running agent
that finishes at 3 pm can turn your desk light green while you are nowhere near the desk.

The computer running Glimt has to be awake for that. A laptop with its lid shut is
asleep and nothing can reach it; leave it open, and plugged in if you will be gone a while.

```bash
glimt remote enable        # turn it on (uses Tailscale Funnel if you have Tailscale)
glimt remote               # the address to give an app, and who is connected
glimt remote approve 612645   # let in the app whose sign-in page shows this code
glimt remote revoke ChatGPT   # disconnect one
glimt remote disable       # close the door
```

It is off until you turn it on.

## What is exposed, and what is not

Remote access is a second listener on its own local port (the daemon's port plus one,
`18771` by default). It serves the five agent tools and a sign-in page. Nothing else:
the dashboard, the local API and its token stay on the daemon's own port, which no tunnel
points at.

Finding the address is not enough to use it:

- **An app signs in** with OAuth 2.1 and PKCE. Its sign-in page shows a six-digit code.
- **You confirm that code on your computer**, with `glimt remote approve` or Allow in
  the menu bar app, which shows the request and its code while one is waiting. The page has no field to type into, so there is nothing for a
  stranger to guess or brute-force: access is only ever granted from your side.
- **Tokens are stored hashed**, are tied to the address they were issued for, and renew
  themselves hourly. `glimt remote revoke` ends an app's access at once.
- **A stranger who finds the address** can ask to sign in, which changes nothing that
  lasts: requests wait in memory, at most eight at a time, for ten minutes.

By default a connected app can cue you and control your devices. To allow cues only:

```bash
glimt remote enable --cues-only
```

## 1. Get a public address

Glimt runs no tunnel of its own. Any tunnel that gives you a stable `https` address
and forwards it to `127.0.0.1:18771` works.

**Tailscale Funnel** (free, no domain needed, survives restarts). With Tailscale
installed and connected, `glimt remote enable` sets it up and finds the address
itself. The first time, Tailscale asks you to allow Funnel for your tailnet: open the
link it prints, approve, and run the command again. To do it by hand:

```bash
tailscale funnel --bg 18771
glimt remote enable https://your-computer.your-tailnet.ts.net
```

The first time, the address takes about ten minutes to appear on the internet. Until it
does, an app says it cannot find the server, while `curl` from your own computer works
(your tailnet already knows the name). `glimt doctor` tells you which it is. Leave
Funnel on while you wait: turning it off and on starts the wait again.

Two things keep it maintenance-free: let Tailscale start at login, and turn off key
expiry for this computer in the Tailscale admin console, or it signs out after 180 days.

**Cloudflare Tunnel** (if you have a domain on Cloudflare):

```bash
cloudflared tunnel login
cloudflared tunnel create glimt
cloudflared tunnel route dns glimt cue.example.com
cloudflared tunnel run --url http://127.0.0.1:18771 glimt   # or install it as a service
glimt remote enable https://cue.example.com
```

A quick tunnel (`cloudflared tunnel --url ...`, ngrok's free tier) is fine for trying it
out, but its address changes on every start, and every app has to be connected again.

## 2. Connect ChatGPT

1. Open [chatgpt.com/plugins](https://chatgpt.com/plugins) › **Add** › **Add custom MCP
   server**. (If the option is missing, turn on developer mode in ChatGPT's settings.)
2. Name it `Glimt`. Under **Connection**, paste the address from `glimt remote`:
   it ends in `/mcp`. Leave **Authentication** on OAuth.
3. Tick the acknowledgement and choose **Create as a plugin**, then **Continue to
   Glimt**.
4. A Glimt page shows a code. On your computer, run `glimt remote approve <code>`.
   The page returns you to ChatGPT, connected.

In a chat, type `@Glimt` and ask: *"cue me when you've finished"*, *"turn the desk
lamp teal"*. Cues from ChatGPT carry its name, so the menu bar app says who is asking.

To take it out again: the plugin's page › **⋯** › **Uninstall**, then
`glimt remote revoke ChatGPT` so its tokens stop working too.

## 3. Your dot

A dot works with the plugins on your ChatGPT account, with the same permissions, so once
step 2 is done the room is within its reach. Two things decide whether it uses it:

- **Tell it when.** A dot cues you only if it knows you want that. Say it once, in your
  own words, for example:

  > When you finish something I asked for, or you need a decision from me, also signal
  > me through Glimt: `done` when finished, `attention` when you are waiting on me,
  > with one line saying what it is about. Do not signal for routine check-ins.

- **Let it act.** Firing a cue changes something in the world, so it counts as an action,
  not as reading. A dot reviews actions against your instructions and rules before it
  takes them, and its unprompted background research is read-only. When you give it the
  instruction above, the dot offers to save it as a custom rule ("take action without
  asking", for signalling only): confirm that card, and it will cue you unprompted.

A dot shares your ChatGPT connection, so its cues arrive under the name ChatGPT.

A dot can instead be given your computer (its profile › Computers › Allow access), where
it can run `glimt signal` like any local agent. That needs the ChatGPT desktop app
open; the plugin does not.

## Other MCP clients

Anything that speaks MCP over HTTP with OAuth works the same way: give it the `/mcp`
address and approve the code. Clients on your own computer are better served by
`glimt mcp` (see [connecting agents](connecting-agents.md)): no tunnel, no sign-in.

## Settings

```toml
[remote]
enabled = true
url = "https://your-computer.your-tailnet.ts.net"   # the public address, nothing after it
control = true        # false: apps may cue you but not control devices
port = 18771          # where the listener answers locally (default: daemon port + 1)
token_minutes = 60    # how long an access token lasts before the app renews it
```

To see what apps are asking, add `debug = ["glimt.remote"]` under `[daemon]` and
restart: each request from outside is logged as one line (method, path, status), with no
tokens or query strings.

## If something is wrong

| What you see | Why |
| --- | --- |
| `glimt remote` says it could not listen | Something else has the port. Set `port` under `[remote]`, and point your tunnel at the new one. |
| The app says the server is unreachable | The tunnel is down, or points at the wrong port. `curl -i https://your-address/mcp` should answer `401`. |
| ChatGPT says it "couldn't discover OAuth settings" | It cannot find the address at all. With a new Tailscale Funnel address, wait ten minutes and try again; `glimt doctor` says when the internet lists it. |
| One call fails with "connection failed", the next works | Seen in the first minutes of a new address, while it is still spreading. It settles by itself. |
| The sign-in page says the request expired | A code lasts ten minutes. Start again from the app. |
| The app asks to reconnect | Its tokens were lost or revoked, or the address changed. Reconnect and approve the new code. |
