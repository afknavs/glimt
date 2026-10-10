# Security

Glimt controls physical devices, so please report problems privately first:
open a [security advisory](../../security/advisories/new) on GitHub rather than a public issue.

What the design relies on:

- The daemon listens on `127.0.0.1` only, rejects requests whose `Host` header is not
  local (which defeats DNS rebinding from a web page), and requires a bearer token
  stored in `~/.glimt/token` with owner-only permissions.
- Credentials (cloud tokens, device keys, Apple Home pairing keys) live only in
  `~/.glimt/secrets/`, owner-only, and are never written to `config.toml` or logs.
- Bridges you install or write run with your user's privileges, like any Python you
  run. Read a third-party bridge before installing it.
- Any program running as your user can read the token and control your devices. That is
  the same trust boundary as your shell, and it is intentional: it is what lets a
  one-line script fire a cue.
