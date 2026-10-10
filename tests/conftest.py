"""Every test runs against a throwaway GLIMT_HOME and a throwaway HOME, so the suite can
never read or change a real ~/.glimt (config, credentials, HomeKit pairing), an agent's
settings in ~/.claude or ~/.codex, or anything in ~/Applications and ~/Library."""

import asyncio

import pytest

from glimt import config as config_module
from glimt.hub import Hub
from glimt.signals import SignalEngine


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GLIMT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.delenv("GLIMT_PORT", raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "user").mkdir()
    return tmp_path / "home"


def demo_config(**bridge_options) -> dict:
    import tomllib
    data = tomllib.loads(config_module.DEMO)
    data["bridges"]["virtual"].update(bridge_options)
    del data["bridges"]["sound"], data["devices"]["speaker"]  # tests stay silent
    return config_module.check(data)


@pytest.fixture
def run():
    """Run a coroutine to completion: keeps the suite free of an asyncio plugin."""
    return lambda coroutine: asyncio.run(coroutine)


async def started_hub(config: dict) -> tuple[Hub, SignalEngine]:
    hub = Hub(config)
    signals = SignalEngine(hub)
    await hub.start()
    if hub._tasks:
        await asyncio.wait(hub._tasks, timeout=5)
    return hub, signals
