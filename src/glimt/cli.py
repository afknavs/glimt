"""The `glimt` command. Imports stay lazy so the hot paths (signal, hook) are instant."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

from . import __version__, client, paths


def _value(text: str):
    lowered = text.lower()
    if lowered in ("on", "true", "yes"):
        return True
    if lowered in ("off", "false", "no"):
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _pairs(items: list[str]) -> dict:
    changes = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"Expected key=value, got '{item}'")
        key, value = item.split("=", 1)
        changes[key] = _value(value)
    return changes


def _state(raw: dict) -> str:
    parts = []
    for key, value in raw["state"].items():
        if value is not None:
            parts.append(f"{key}={'on' if value is True else 'off' if value is False else value}")
    return "  ".join(parts) or "-"


# ---- commands -----------------------------------------------------------------------


def cmd_daemon(args) -> None:
    from . import daemon
    daemon.run(port=args.port)


def cmd_start(args) -> None:
    from . import service
    if client.running():
        print("Glimt is already running.")
        return
    if service.installed():  # the login service for this setup exists but is stopped
        service.install()  # re-registering it starts it
        ok = _wait_running()
    else:
        ok = client.start_daemon()
    print("Glimt is running." if ok else f"Could not start. See {paths.log_file()}")
    sys.exit(0 if ok else 1)


def _wait_running(seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if client.running():
            return True
        time.sleep(0.1)
    return False


def cmd_stop(args) -> None:
    from . import service
    if service.stop():
        print("Stopped the Glimt service (it starts again at next login; `glimt start` to start now).")
        return
    print("Stopped." if client.stop_daemon() else "Glimt was not running.")


def _pid() -> int | None:
    try:
        return client.request("GET", "/v1/health", timeout=1.5, autostart=False)["pid"]
    except (client.DaemonError, OSError, KeyError):
        return None


def _restart(seconds: float = 25.0) -> bool:
    """Restart and wait for the *new* daemon: the old one keeps answering for a moment."""
    from . import service
    before = _pid()
    if not service.restart():
        client.stop_daemon()
        client.start_daemon()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _pid() not in (None, before):
            return True
        time.sleep(0.15)
    return False


def cmd_restart(args) -> None:
    print("Restarted." if _restart() else f"Did not come back up. See {paths.log_file()}")


def cmd_status(args) -> None:
    from . import service
    if not client.running():
        print("Glimt is not running. Start it with: glimt start")
        sys.exit(1)
    health = client.request("GET", "/v1/health")
    managed = "login service" if service.installed() else "started manually"
    print(f"Glimt {health['version']} is running on port {paths.port()} ({managed}), "
          f"{health['devices']} device(s).")
    for name, bridge in health["bridges"].items():
        print(f"  {'✗' if bridge['error'] else '✓'} {name} ({bridge['type']})"
              + (f": {bridge['error']}" if bridge["error"] else ""))
    if health.get("mute"):
        until = time.localtime(health["mute"])
        print("  ⏸ cues are muted " + ("until you unmute" if health["mute"] - time.time() > 365 * 86400
                                        else "until " + time.strftime("%H:%M", until)))
    home = health.get("homekit")
    if home:
        print(f"  {'✗' if home['error'] else '✓'} Apple Home: " + (home["error"] or (
            f"paired, {home['accessories']} accessories, {home['connected']} controller(s) connected, "
            f"{home['listening']} listening" if home["paired"] else "not paired yet (run: glimt homekit)")))


def cmd_devices(args) -> None:
    data = client.request("GET", "/v1/devices")
    if args.json:
        print(json.dumps(data, indent=2))
        return
    if not data["devices"]:
        print("No devices yet. Run: glimt setup")
        return
    width = max(len(raw["id"]) for raw in data["devices"])
    for raw in data["devices"]:
        flag = "✗" if raw.get("online") is False else "~" if raw.get("assumed") else "✓"
        print(f"{flag} {raw['id']:<{width}}  {raw['kind']:<8} {raw.get('route') or '-':<8} {_state(raw)}"
              + (f"   ! {raw['error']}" if raw.get("error") else ""))
    print("\n✓ live state   ~ last sent (device cannot report)   ✗ unreachable")


def cmd_set(args) -> None:
    result = client.request("POST", f"/v1/devices/{args.device}", _pairs(args.changes))
    print(f"ok ({result['ms']} ms)")


def cmd_power(args) -> None:
    result = client.request("POST", f"/v1/devices/{args.device}", {"power": args.command == "on"})
    print(f"ok ({result['ms']} ms)")


def cmd_do(args) -> None:
    result = client.request("POST", f"/v1/devices/{args.device}/actions/{args.action}", _pairs(args.args))
    print(f"ok ({result['ms']} ms)")


def cmd_signal(args) -> None:
    body = {"name": args.name, "message": args.message, "color": args.color, "pattern": args.pattern,
            "count": args.count, "sound": args.sound, "hold": args.hold, "source": "cli"}
    if args.name in (None, "custom"):
        body.pop("name")
    if args.urgent:
        body["urgent"] = True
    try:
        result = client.request("POST", "/v1/signal", {k: v for k, v in body.items() if v is not None},
                                timeout=3, autostart=not args.quiet)  # -q is for hooks: never stall one
    except client.DaemonError as exc:
        if args.quiet:
            return
        raise
    if not args.quiet:
        print(f"skipped ({result['skipped']})" if result.get("skipped") else "signalled")


def cmd_clear(args) -> None:
    client.request("DELETE", "/v1/signal")
    print("cleared")


def cmd_hook(args) -> None:
    # Called by agents. Must never fail or print: a broken cue should not break a session.
    from . import hooks
    try:
        hooks.handle(hooks.read_payload(args.payload), args.agent)
    except Exception:
        pass


def cmd_hooks(args) -> None:
    from . import hooks
    if args.action == "show":
        print(json.dumps({"hooks": {event: [{"hooks": [{"type": "command", "command": hooks.command(args.agent)}]}]
                                    for event in hooks.EVENTS[args.agent]}}, indent=2))
        return
    if args.action == "install":
        path, added = hooks.install(args.agent, args.file)
        print(f"Added {', '.join(added)} hooks to {path}" if added else f"Already installed in {path}")
        if added and args.agent == "codex":
            print("Codex asks you to trust new hooks the next time it starts.")
    else:
        path, removed = hooks.uninstall(args.agent, args.file)
        print(f"Removed {removed} hook(s) from {path}")


def cmd_connect(args) -> None:
    from . import agents
    chosen = [args.agent] if args.agent else [name for name in agents.AGENTS if shutil.which(name)]
    if not chosen:
        raise SystemExit("Neither `claude` nor `codex` is on your PATH. Name one: glimt connect claude")
    for name in chosen:
        print(f"{agents.AGENTS[name]}:")
        for line in (agents.connect(name) if args.cmd == "connect" else agents.disconnect(name)):
            print("  " + line)
    if args.cmd == "connect":
        print("\nFrom now on a finished turn pulses your lights green, and a blocked agent holds them amber.\n"
              "Short turns stay quiet ([hooks] min_seconds, default 20). Undo with: glimt disconnect")


def cmd_agents(args) -> None:
    from . import agents
    rows = agents.status()
    if args.json:
        print(json.dumps(rows))
        return
    for row in rows:
        if not row["found"] and not row["hooks"]:
            state = "not installed on this computer"
        else:
            state = ("cues you when it finishes or needs you" if row["hooks"] else "does not cue you yet") \
                + (", can use your devices" if row["tools"] else "")
        print(f"{'✓' if row['hooks'] else '·'} {row['name']:<12} {state}")
    if any(row["found"] and not row["hooks"] for row in rows):
        print("\nConnect with: glimt connect")


def cmd_mute(args) -> None:
    if args.cmd == "unmute":
        client.request("DELETE", "/v1/mute")
        print("Cues are back on.")
        return
    client.request("POST", "/v1/mute", {"minutes": args.minutes} if args.minutes else {})
    print((f"Cues are muted for {args.minutes:g} minutes." if args.minutes
           else "Cues are muted until you run: glimt unmute") + " Urgent ones still show.")


def _ago(when: float | None) -> str:
    if not when:
        return "never used"
    minutes = (time.time() - when) / 60
    return "used just now" if minutes < 2 else f"used {minutes:.0f} min ago" if minutes < 90 else \
        f"used {minutes / 60:.0f} h ago" if minutes < 48 * 60 else "used " + time.strftime("%d %b", time.localtime(when))


def _remote_lines(status: dict) -> list[str]:
    if not status.get("enabled"):
        return ["Remote access is off" + (f": {status['error']}" if status.get("error") else
                                          ". Agents on this computer do not need it.\n"
                                          "For ChatGPT, a dot or another cloud agent: glimt remote enable")]
    lines = [f"Remote access is on ({'cues and device control' if status['control'] else 'cues only'}).",
             f"  Address to give an app: {status['endpoint']}"]
    if status.get("error"):
        lines.append(f"  ✗ {status['error']}")
    lines += [f"  ✓ {app['name']:<14} {_ago(app['used'])}" for app in status["apps"]]
    lines += [f"  ? {item['name']} wants to connect (returns to {item['returns_to']}). "
              f"Allow with: glimt remote approve {item['code']}" for item in status["pending"]]
    if not status["apps"] and not status["pending"]:
        lines.append("  No app is connected yet.")
    return lines


def cmd_remote(args) -> None:
    from . import config as config_module
    value = " ".join(args.value)
    if args.action == "status":
        status = client.request("GET", "/v1/remote")
        print(json.dumps(status) if args.json else "\n".join(_remote_lines(status)))
    elif args.action in ("approve", "deny"):
        if not value:
            raise SystemExit(f"Which one? glimt remote {args.action} <the code on the sign-in page>")
        name = client.request("POST", f"/v1/remote/{args.action}", {"code": value})["name"]
        print(f"{name} is connected." if args.action == "approve" else f"Declined {name}.")
    elif args.action == "revoke":
        if not value:
            raise SystemExit("Which app? glimt remote revoke <name>   (names: glimt remote)")
        print("Disconnected " + ", ".join(client.request("DELETE", "/v1/remote/apps/" + value)["revoked"]) + ".")
    elif args.action == "disable":
        from .setup import tunnel
        settings = config_module.load().get("remote", {})
        config_module.write_table("remote", {**settings, "enabled": False})
        closed = tunnel.close(settings.get("url", ""))
        _restart()
        print("Remote access is off." + (" The Tailscale Funnel address is closed too." if closed else "")
              + " Connected apps are remembered; `glimt remote enable` brings them back.")
    else:
        from . import remote
        from .setup import tunnel
        settings = config_module.load().get("remote", {})
        port = int(settings.get("port") or paths.port() + 1)
        url = value or settings.get("url")
        funnelled = not value and not (url and not tunnel.tailscale())
        if funnelled:
            url = tunnel.funnel(port)  # says what is missing, and how to supply an address by hand
        new = {**settings, "enabled": True, "url": url.rstrip("/"), "control": not args.cues_only}
        try:
            remote.public_url(new)
        except ValueError as exc:
            raise SystemExit(f"glimt: {exc}") from None
        config_module.write_table("remote", new)
        if not _restart():
            raise SystemExit(f"glimt: the daemon did not come back up. See {paths.log_file()}")
        status = client.request("GET", "/v1/remote")
        print("\n".join(_remote_lines(status)))
        if not status.get("error"):
            print(f"\nTailscale Funnel sends {new['url']} to this computer. The first time, the address takes about\n"
                  "ten minutes to appear on the internet; until then an app cannot find it. Leave Funnel on meanwhile."
                  if funnelled else
                  f"\nThis computer answers there on port {port}; a tunnel must send {new['url']} to it.")
            print("When an app signs in, its page shows a code. Confirm it here: glimt remote approve <code>")


def cmd_app(args) -> None:
    from . import menuapp
    if args.action == "install":
        print("Building the menu bar app on this Mac (about 30 seconds)...")
        print(f"Installed {menuapp.install()}. Look for the lightbulb in your menu bar; it opens at login.")
    else:
        print("Removed the menu bar app." if menuapp.uninstall() else "The menu bar app was not installed.")


def cmd_run(args) -> None:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("Usage: glimt run -- <command>")
    started = time.time()
    try:
        code = subprocess.call(command)
    except FileNotFoundError:
        raise SystemExit(f"glimt: command not found: {command[0]}") from None
    name = "done" if code == 0 else "error"
    label = " ".join(command)[:60]
    try:
        client.request("POST", "/v1/signal", {
            "name": name, "source": "run",
            "message": f"{label}: {'ok' if code == 0 else f'exit {code}'} in {time.time() - started:.0f}s"}, timeout=3)
    except client.DaemonError:
        pass
    sys.exit(code)


def cmd_mcp(args) -> None:
    from . import mcp_server
    client.start_daemon(wait=4)
    mcp_server.main()


def cmd_open(args) -> None:
    import webbrowser
    client.request("GET", "/v1/health")
    url = f"http://127.0.0.1:{paths.port()}/#token={paths.token()}"
    print(f"Opening http://127.0.0.1:{paths.port()}/")
    webbrowser.open(url)


def cmd_demo(args) -> None:
    from . import config
    if paths.config_file().exists() and not args.force:
        print(f"{paths.config_file()} already exists; leaving it alone. Use --force to replace it.")
    else:
        paths.config_file().write_text(config.DEMO)
        print(f"Wrote a demo config with simulated devices to {paths.config_file()}")
        if client.running():
            cmd_restart(args)
    client.start_daemon()
    cmd_open(args)
    print("\nTry it:\n  glimt signal done\n  glimt signal attention -m 'Need a decision'\n"
          "  glimt set lamp power=on color=purple\n  glimt run -- sleep 3")


def cmd_doctor(args) -> None:
    from . import doctor
    sys.exit(doctor.run(fire=args.signal))


def cmd_setup(args) -> None:
    from .setup import wizard
    wizard.run(args.bridge)


def cmd_bridge(args) -> None:
    from . import bridges, scaffold
    if args.action == "list":
        for name, origin in sorted(bridges.available().items()):
            print(f"{name:<16} {origin}")
    elif args.action == "new":
        print(scaffold.new(args.name))
    else:
        sys.exit(scaffold.test(args.name, args.write))


def cmd_homekit(args) -> None:
    from . import homekit
    status = client.request("GET", "/v1/health").get("homekit")
    if not status:
        print("Apple Home export is off. Add this to config.toml, then `glimt restart`:\n\n"
              "[homekit]\nenabled = true")
    elif status["error"]:
        print(f"Apple Home export failed: {status['error']}")
    elif status["paired"]:
        print(f"Paired with Apple Home: {status['accessories']} accessories.")
    else:
        code = homekit.code_file().read_text().strip() if homekit.code_file().exists() else "(starting…)"
        print(f"Ready to pair, {status['accessories']} accessories.\n"
              f"Home app › + › Add Accessory › More Options › choose the bridge › enter code: {code}")


def cmd_service(args) -> None:
    from . import service
    if args.action == "install":
        print(f"Installed and started: {service.install()}")
    else:
        print("Removed." if service.uninstall() else "No service was installed.")


# ---- parser -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="glimt", description="Cue lights for AI agents.")
    parser.add_argument("--version", action="version", version=f"glimt {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    def add(name, handler, help_text, **kwargs):
        hidden = help_text is argparse.SUPPRESS  # internal commands stay out of --help
        command = sub.add_parser(name, **({} if hidden else {"help": help_text}), **kwargs)
        command.set_defaults(handler=handler)
        return command

    add("setup", cmd_setup, "connect your devices (interactive)").add_argument(
        "bridge", nargs="?", help="jump straight to one bridge, e.g. tuya, elk-ble, homeassistant")
    add("demo", cmd_demo, "try it with simulated devices, no hardware").add_argument("--force", action="store_true")
    add("doctor", cmd_doctor, "check that everything works").add_argument(
        "--signal", action="store_true", help="also fire a test cue")

    command = add("signal", cmd_signal, "fire a cue: done, attention, error, info, working, or custom")
    command.add_argument("name", nargs="?", default=None)
    command.add_argument("-m", "--message")
    command.add_argument("--color")
    command.add_argument("--pattern", choices=["pulse", "flash", "breathe", "solid"])
    command.add_argument("--count", type=int)
    command.add_argument("--sound")
    command.add_argument("--hold", type=float, help="seconds to stay lit; -1 until cleared")
    command.add_argument("--urgent", action="store_true", help="ignore quiet hours")
    command.add_argument("-q", "--quiet", action="store_true", help="never print or fail (for hooks)")
    add("clear", cmd_clear, "end a held cue and restore the lights")
    add("run", cmd_run, "run a command, then cue done or error").add_argument("command", nargs=argparse.REMAINDER)

    add("devices", cmd_devices, "list devices and their state", aliases=["ls"]).add_argument(
        "--json", action="store_true")
    command = add("set", cmd_set, "change a device: glimt set lamp power=on color=red")
    command.add_argument("device")
    command.add_argument("changes", nargs="+")
    for name in ("on", "off"):
        add(name, cmd_power, f"turn a device {name}").add_argument("device")
    command = add("do", cmd_do, "run a device action: glimt do fan faster")
    command.add_argument("device")
    command.add_argument("action")
    command.add_argument("args", nargs="*")

    add("start", cmd_start, "start the background daemon")
    add("stop", cmd_stop, "stop it")
    add("restart", cmd_restart, "restart it (after editing config.toml)")
    add("status", cmd_status, "is it running, and are the bridges healthy")
    add("open", cmd_open, "open the dashboard in your browser")
    command = add("service", cmd_service, "run at login: install | uninstall")
    command.add_argument("action", choices=["install", "uninstall"])

    add("homekit", cmd_homekit, "Apple Home: pairing code and status")
    add("mcp", cmd_mcp, "run the MCP server (stdio) for an agent")
    for name, text in (("connect", "connect Claude Code / Codex: hooks + tools, in one step"),
                       ("disconnect", "undo `connect`")):
        add(name, cmd_connect, text).add_argument("agent", nargs="?", choices=["claude", "codex"])
    command = add("hooks", cmd_hooks, "just the lifecycle hooks: install | uninstall | show")
    command.add_argument("action", choices=["install", "uninstall", "show"])
    command.add_argument("agent", choices=["claude", "codex"])
    command.add_argument("--file", type=lambda value: __import__("pathlib").Path(value).expanduser(),
                         help="settings file to edit (default: the agent's own)")
    command = add("hook", cmd_hook, argparse.SUPPRESS)
    command.add_argument("payload", nargs="?")
    command.add_argument("--agent")
    add("agents", cmd_agents, "which agents are connected").add_argument("--json", action="store_true")
    add("mute", cmd_mute, "silence cues for a while: glimt mute 60 (minutes; none = until unmute)").add_argument(
        "minutes", nargs="?", type=float)
    add("unmute", cmd_mute, "turn cues back on")
    add("app", cmd_app, "the macOS menu bar app: install | uninstall").add_argument(
        "action", choices=["install", "uninstall"])

    command = add("remote", cmd_remote, "ChatGPT, dots, cloud agents: enable [url] | approve <code> | revoke <app> | disable")
    command.add_argument("action", nargs="?", default="status",
                         choices=["status", "enable", "disable", "approve", "deny", "revoke"])
    command.add_argument("value", nargs="*", help="enable: your public https address (default: Tailscale Funnel); "
                                                  "approve/deny: the code; revoke: the app's name")
    command.add_argument("--cues-only", action="store_true", help="enable: apps may cue you but not control devices")
    command.add_argument("--json", action="store_true")

    command = add("bridge", cmd_bridge, "bridges: list | new <type> | test <type>")
    command.add_argument("action", choices=["list", "new", "test"])
    command.add_argument("name", nargs="?")
    command.add_argument("--write", action="store_true", help="test: also re-send current values to time writes")

    command = add("daemon", cmd_daemon, argparse.SUPPRESS)
    command.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return
    try:
        args.handler(args)
    except (client.DaemonError, RuntimeError) as exc:
        raise SystemExit(f"glimt: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
