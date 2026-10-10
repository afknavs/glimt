"""Play a short tone (or speak a sentence) on the computer running the daemon.

    [bridges.sound]

    [devices.speaker]
    bridge = "sound"
    kind = "speaker"

Sounds are named by meaning ("done", "attention", "error", "info") so a config works
on any OS. You can also pass a system sound name ("Glass") or a path to an audio file.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from ..model import Device
from .base import Bridge, BridgeError

MAC = {"done": "Glass", "attention": "Ping", "error": "Basso", "info": "Tink"}
LINUX = {"done": "complete", "attention": "message-new-instant", "error": "dialog-error", "info": "bell"}


class SoundBridge(Bridge):
    type = "sound"

    def devices(self) -> list[Device]:
        made = []
        for device_id, cfg in self.device_configs.items():
            device = self.make_device(device_id, cfg, actions=["play", "say"], assumed=True)
            device.online, device.route = True, "local"
            made.append(device)
        return made

    async def apply(self, device: Device, changes: dict) -> None:
        raise BridgeError("A speaker has no settings; use the play or say action")

    async def act(self, device: Device, action: str, args: dict):
        if action == "say":
            text = str(args.get("text", "")).strip()[:300]
            if not text:
                raise BridgeError("say needs text")
            command = self._say_command(text)
        else:
            command = self._play_command(str(args.get("sound") or "done"))
        if command is None:
            raise BridgeError("No audio player found on this system")
        # Start it and return: a tone must never delay the light.
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        asyncio.create_task(process.wait())
        return None

    def _play_command(self, sound: str) -> list[str] | None:
        if Path(sound).expanduser().is_file():
            path = str(Path(sound).expanduser())
        elif sys.platform == "darwin":
            path = f"/System/Library/Sounds/{MAC.get(sound, sound)}.aiff"
            if not Path(path).exists():
                raise BridgeError(f"No sound '{sound}'. Try: {', '.join(MAC)}, a system sound name, or a file path")
        elif sys.platform.startswith("linux"):
            path = f"/usr/share/sounds/freedesktop/stereo/{LINUX.get(sound, sound)}.oga"
        else:
            return ["powershell", "-c", "[console]::beep(880,180)"] if shutil.which("powershell") else None
        volume = self.config.get("volume")
        if sys.platform == "darwin":
            return ["afplay", path] + (["-v", str(volume)] if volume is not None else [])
        for player in ("paplay", "pw-play", "aplay"):
            if shutil.which(player):
                return [player, path]
        return None

    def _say_command(self, text: str) -> list[str] | None:
        if sys.platform == "darwin":
            return ["say", text]
        for speaker in ("spd-say", "espeak"):
            if shutil.which(speaker):
                return [speaker, text]
        return None
