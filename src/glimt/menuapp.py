"""The macOS menu bar app. It is built on your own Mac from the Swift sources that ship
with Glimt, so there is nothing to download, notarise or trust beyond this package."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "dev.glimt.menu"


def app_path() -> Path:
    return Path.home() / "Applications" / "Glimt.app"


def _login_item() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def sources() -> Path | None:
    """The Swift sources: inside an installed package, or in a source checkout."""
    for folder in (Path(__file__).parent / "_menuapp", Path(__file__).resolve().parents[2] / "app" / "macos"):
        if (folder / "build.sh").is_file() and (folder / "Sources").is_dir():
            return folder
    return None


def installed() -> bool:
    return app_path().exists()


def can_build() -> bool:
    """macOS, with the sources and Apple's command line tools present."""
    if sys.platform != "darwin" or sources() is None:
        return False
    try:  # /usr/bin/swiftc exists even without the tools; this is what says they are installed
        return subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install() -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("The menu bar app is for macOS. On other systems use: glimt open")
    folder = sources()
    if folder is None:
        raise RuntimeError("The app's sources are missing from this install.")
    if not can_build():
        raise RuntimeError("Building the app needs Apple's command line tools. Install them with:\n"
                           "  xcode-select --install\nthen run: glimt app install")
    with tempfile.TemporaryDirectory() as work:
        result = subprocess.run(["/bin/zsh", str(folder / "build.sh"), "--install"], capture_output=True, text=True,
                                env={**os.environ, "GLIMT_APP_BUILD": work})
    if result.returncode != 0:
        raise RuntimeError("Could not build the menu bar app:\n" + (result.stderr or result.stdout).strip()[-1200:])
    return app_path()


def uninstall() -> bool:
    import shutil
    found = installed() or _login_item().exists()
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        subprocess.run(["pkill", "-x", "Glimt"], capture_output=True)
    _login_item().unlink(missing_ok=True)
    shutil.rmtree(app_path(), ignore_errors=True)
    return found
