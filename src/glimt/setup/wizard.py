"""`glimt setup`: connect devices by answering a few questions.

Every step only *adds* to config.toml (new tables are appended, so your comments and
hand edits survive) and stores credentials under ~/.glimt/secrets/.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import re
import shutil
import sys

from .. import bridges, client, config as config_module, paths
from ..bridges.base import BridgeError

MENU = [
    ("tuya", "Tuya / Smart Life      Wi-Fi bulbs, plugs, air conditioners"),
    ("homeassistant", "Home Assistant         anything it already controls"),
    ("hue", "Philips Hue"),
    ("wled", "WLED                   addressable LED strips"),
    ("elk-ble", "Bluetooth LED strip    ELK-BLEDOM / MELK / Lotus Lamp"),
    ("demo", "Nothing yet            simulated devices, to try Glimt out"),
]
# Keys that describe a device to people rather than identify it to its bridge.
COSMETIC = {"id", "name", "kind", "bridge", "icon", "signal", "capabilities", "actions"}


class Wizard:
    def __init__(self, ask=input, say=print, secret=getpass.getpass) -> None:
        self.ask, self.say, self.secret = ask, say, secret
        self.config = config_module.load()
        self.before = {section: set(self.config[section]) for section in ("bridges", "devices")}
        self._original = json.loads(json.dumps(self.config["bridges"]))

    # ---- helpers ------------------------------------------------------------------

    def yes(self, question: str, default: bool = True) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer.startswith("y")

    def bridge(self, bridge_type: str, name: str | None = None) -> tuple[str, dict]:
        """The [bridges.*] table for this type, created if needed."""
        for existing, table in self.config["bridges"].items():
            if table.get("type", existing) == bridge_type:
                return existing, table
        name = name or bridge_type
        table = self.config["bridges"].setdefault(name, {} if name == bridge_type else {"type": bridge_type})
        return name, table

    def save_secret(self, filename: str, name: str, value: dict) -> None:
        path = paths.secrets_dir() / filename
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            data = {}
        data[name] = value
        paths.write_private(path, json.dumps(data, indent=2))

    def offer(self, bridge_name: str, found: list[dict]) -> int:
        """Show discovered devices and add the ones the user keeps."""
        def identity(device_id: str, table: dict) -> str:
            # What the bridge addresses it by (an entity, a host, a light id…); failing that, its id.
            return json.dumps({k: v for k, v in table.items() if k not in COSMETIC}, sort_keys=True) \
                .replace("{}", device_id)

        known = {identity(device_id, table) for device_id, table in self.config["devices"].items()
                 if table["bridge"] == bridge_name}
        fresh = [device for device in found if identity(str(device.get("id")), device) not in known]
        if not fresh:
            self.say("Nothing new found." if found else "No devices found.")
            return 0
        shown = fresh
        while True:
            for number, device in enumerate(shown, 1):
                self.say(f"  {number:>2}. {device['name']}  ({device['kind']})")
            answer = self.ask("Add which? [Enter = all, numbers like 1 3 5, none, or a word to filter] ").strip().lower()
            if answer in ("", "all", "a", "y", "yes"):
                chosen = shown
            elif answer in ("none", "n", "no"):
                chosen = []
            elif re.fullmatch(r"[\d\s,]+", answer):
                picks = {int(number) for number in re.findall(r"\d+", answer)}
                chosen = [device for number, device in enumerate(shown, 1) if number in picks]
            else:
                shown = [device for device in fresh if answer in device["name"].lower()] or fresh
                continue
            break
        for device in chosen:
            table = {key: value for key, value in device.items() if key != "id"}
            base = device_id = re.sub(r"[^a-z0-9_-]+", "-", str(device.get("id") or device["name"]).lower()).strip("-") or "device"
            number = 2
            while device_id in self.config["devices"]:
                device_id, number = f"{base}-{number}", number + 1
            self.config["devices"][device_id] = {"name": table.pop("name"), "kind": table.pop("kind"),
                                                 "bridge": bridge_name, **table}
        self.say(f"Added {len(chosen)} device(s).")
        return len(chosen)

    def discover(self, bridge_type: str, bridge_name: str, table: dict) -> list[dict]:
        cls = bridges.load(bridge_type)
        return asyncio.run(cls.discover({**table, "name": bridge_name}))

    # ---- one step per kind of hardware ----------------------------------------------

    def tuya(self) -> None:
        from . import tuya
        tuya.run(self.config, self.ask, self.say)

    def elk_ble(self) -> None:
        self.say("\nPower the strip on and close its phone app (these strips accept one connection at a time).")
        if sys.platform == "darwin":
            self.say("macOS may ask to let Python use Bluetooth: allow it.")
        self.say("Scanning for six seconds…")
        name, table = self.bridge("elk-ble", "ble")
        self.offer(name, self.discover("elk-ble", name, table))

    def wled(self) -> None:
        name, table = self.bridge("wled")
        self.say("\nLooking for WLED strips on your network…")
        found = self.discover("wled", name, table)
        if not found:
            hosts = self.ask("None announced themselves. Type the strip's IP address (or several, space separated): ")
            found = [{"id": "strip" if index == 0 else f"strip-{index + 1}", "name": "LED strip", "kind": "light",
                      "host": host} for index, host in enumerate(hosts.replace(",", " ").split())]
        self.offer(name, found)

    def hue(self) -> None:
        from ..bridges.hue import HueBridge
        name, table = self.bridge("hue")
        if not table.get("host"):
            guess = ""
            try:
                import httpx
                guess = httpx.get("https://discovery.meethue.com/", timeout=4).json()[0]["internalipaddress"]
            except Exception:
                pass
            table["host"] = self.ask(f"Hue bridge address{f' [{guess}]' if guess else ''}: ").strip() or guess
        for _ in range(4):
            self.ask("Press the round button on top of the Hue bridge, then press Enter here. ")
            try:
                self.save_secret("hue.json", name, {"key": asyncio.run(HueBridge.pair(table["host"]))})
                self.say("Linked.")
                break
            except BridgeError as exc:
                self.say(f"  {exc}")
        else:
            return
        self.offer(name, self.discover("hue", name, table))

    def homeassistant(self) -> None:
        name, table = self.bridge("homeassistant")
        default = table.get("url") or "http://homeassistant.local:8123"
        table["url"] = (self.ask(f"Home Assistant address [{default}]: ").strip() or default).rstrip("/")
        self.say("\nIn Home Assistant: click your name (bottom left) › Security › Long-lived access tokens › "
                 "Create token.\nPaste it here (it will not be shown, and is stored only in ~/.glimt/secrets/).")
        token = self.secret("Token: ").strip()
        if token:
            self.save_secret("homeassistant.json", name, {"token": token})
        try:
            import websockets  # noqa: F401
        except ModuleNotFoundError:
            self.say("\nOne more package is needed for the live connection:\n"
                     f"  {sys.executable} -m pip install 'glimt[homeassistant]'")
        self.say("\nThese are the lights, switches, fans and climate devices it has:")
        self.offer(name, self.discover("homeassistant", name, table))

    def demo(self) -> None:
        name, _ = self.bridge("virtual")
        self.offer(name, [
            {"id": "lamp", "name": "Desk lamp", "kind": "light", "capabilities": ["power", "brightness", "color"]},
            {"id": "strip", "name": "LED strip", "kind": "light", "capabilities": ["power", "brightness", "color"]}])

    def other(self, bridge_type: str) -> None:
        """Any bridge that implements discover(), including ones you wrote yourself."""
        name, table = self.bridge(bridge_type)
        self.offer(name, self.discover(bridge_type, name, table))

    def step(self, bridge_type: str) -> None:
        handler = getattr(self, bridge_type.replace("-", "_"), None) if bridge_type in dict(MENU) else None
        try:
            handler() if handler else self.other(bridge_type)
        except ModuleNotFoundError as exc:
            self.say(f"\nThis needs the '{exc.name}' package. Install it and run setup again:\n"
                     f"  {sys.executable} -m pip install 'glimt[all]'")
        except (BridgeError, RuntimeError, OSError) as exc:
            self.say(f"\nThat did not work: {exc}")

    # ---- finishing ------------------------------------------------------------------

    def add_speaker(self) -> None:
        if any(table["kind"] == "speaker" for table in self.config["devices"].values()):
            return
        if self.yes("\nPlay a short tone on this computer with each cue?"):
            name, _ = self.bridge("sound")
            self.config["devices"]["computer"] = {"name": "This computer", "kind": "speaker", "bridge": name}

    def save(self) -> bool:
        """Append what was added. Returns False when nothing changed."""
        import tomli_w

        added = {section: {key: table for key, table in self.config[section].items()
                           if key not in self.before[section]} for section in ("bridges", "devices")}
        changed = {key: table for key, table in self.config["bridges"].items()
                   if key in self.before["bridges"] and table != self._original.get(key)}
        if not any(added.values()) and not changed:
            return False
        path = paths.config_file()
        if changed:  # an existing table gained a key (a host, a URL): rewrite, keeping a backup
            if path.exists():
                shutil.copy2(path, path.with_name("config.toml.bak"))
                self.say(f"(Previous config kept as {path.with_name('config.toml.bak')})")
            config_module.save(self.config)
        else:
            text = path.read_text() if path.exists() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text.rstrip("\n") + ("\n\n" if text.strip() else "") + tomli_w.dumps(
                {section: tables for section, tables in added.items() if tables}))
        return True

    def run(self, only: str | None = None) -> None:
        if only:
            self.step(only)
        else:
            self.say("What would you like to connect? You can come back and add more at any time.\n")
            while True:
                for number, (_, label) in enumerate(MENU, 1):
                    self.say(f"  {number}. {label}")
                answer = self.ask("\nNumber (or Enter when you are done): ").strip()
                if not answer:
                    break
                if answer.isdigit() and 1 <= int(answer) <= len(MENU):
                    self.step(MENU[int(answer) - 1][0])
                    self.say("")
            self.add_speaker()
        if self.save():
            self.say(f"\nSaved {paths.config_file()}")
            self.finish()
        else:
            self.say("\nNothing was changed.")
            if not self.config["devices"]:
                return
        self.extras()

    def finish(self) -> None:
        from .. import doctor, service

        if client.running():
            if not service.restart():  # started by hand: restart it the same way
                self._restart_manual()
        elif service.installed():
            service.install()
        elif service.supported() and not service.elsewhere() \
                and self.yes("Start Glimt automatically when you log in?"):
            service.install()
        else:
            client.start_daemon()
        self._wait(seconds=15)
        self.say("")
        doctor.run()
        if self.yes("\nSend a test cue now (your lights will blink blue once)?"):
            try:
                client.request("POST", "/v1/signal", {"name": "info", "source": "setup", "urgent": True})
            except client.DaemonError as exc:
                self.say(f"  {exc}")

    def extras(self) -> None:
        """What makes it useful day to day: your agents, and something to click. Offered on
        every run, so `glimt setup` is also how you add these later."""
        self.connect_agents()
        self.menu_app()
        self.say("\nDone. Try:  glimt signal done    ·    glimt devices    ·    glimt open")

    def connect_agents(self) -> None:
        from .. import agents
        connected = False
        for agent in agents.status():
            if not agent["found"] or agent["hooks"]:
                continue
            if self.yes(f"\nConnect {agent['name']}? It will cue you when it finishes or needs you, "
                        "and can use your devices."):
                for line in agents.connect(agent["id"]):
                    self.say("  " + line)
                connected = True
        if connected:
            self.say("  Restart the agent to pick this up. When you need quiet: glimt mute")

    def menu_app(self) -> None:
        from .. import menuapp
        if menuapp.installed() or not menuapp.can_build():
            return
        if self.yes("\nAdd Glimt to your menu bar? It is built on this Mac and takes about 30 seconds."):
            try:
                menuapp.install()
                self.say("  Added. Look for the lightbulb in your menu bar.")
            except RuntimeError as exc:
                self.say(f"  {exc}")

    def _restart_manual(self) -> None:
        client.stop_daemon()
        client.start_daemon()

    def _wait(self, seconds: float = 10.0) -> None:
        import time
        deadline = time.time() + seconds
        while time.time() < deadline and not client.running():
            time.sleep(0.2)


def run(only: str | None = None) -> None:
    try:
        Wizard().run(only)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Nothing was saved.")
