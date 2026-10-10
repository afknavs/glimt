"""What Apple Home is told about each accessory. Nothing here opens a socket or pairs."""

import asyncio
import re
import types

import pytest

pytest.importorskip("pyhap")

from glimt import homekit
from glimt.model import Device


def build(tmp_path):
    """A bridge holding a light and an air conditioner, on a driver that is never started."""
    from pyhap.accessory import Bridge
    from pyhap.accessory_driver import AccessoryDriver

    driver = AccessoryDriver(port=51999, persist_file=str(tmp_path / "homekit.state"),
                             loop=asyncio.get_running_loop(), async_zeroconf_instance=types.SimpleNamespace())
    export = homekit.HomeKitExport(hub=None, config={})
    export.driver = driver
    lamp = Device("lamp", "Lamp", "light", "virtual", capabilities={"power": {}, "brightness": {}})
    ac = Device("ac", "Air conditioner", "climate", "virtual",
                capabilities={"power": {}, "temperature": {"min": 16, "max": 30}})
    bridge = Bridge(driver, "Glimt")
    bridge.set_info_service(firmware_revision=homekit.FIRMWARE, manufacturer="Glimt", model="Bridge",
                            serial_number="glimt-bridge")
    thermostat = homekit.Thermostat(export, ac, 3, {})
    for accessory in (homekit.PowerAccessory(export, lamp, 2, {}), thermostat,
                      homekit.ActionSwitch(export, {"name": "Fan faster", "device": "fan", "on": "faster"}, 4)):
        bridge.add_accessory(accessory)
    return bridge, thermostat


def test_every_accessory_states_who_made_it_and_a_firmware_version(tmp_path, run):
    # Apple devices re-read a blank firmware version on every connection and then hang up.
    async def scenario():
        bridge, _ = build(tmp_path)
        return [{c["type"]: c.get("value") for c in accessory["services"][0]["characteristics"]}
                for accessory in bridge.to_HAP()]

    described = run(scenario())
    assert len(described) == 4
    for info in described:
        manufacturer, model, serial, firmware = info["20"], info["21"], info["30"], info["52"]
        assert manufacturer == "Glimt" and len(model) > 1 and len(serial) > 1
        assert re.fullmatch(r"\d+\.\d+\.\d+", firmware)


@pytest.mark.parametrize("sent, power", [(0, False), (1, True), (2, True), (3, True)])
def test_the_air_conditioner_takes_heat_or_auto_from_home_as_on(tmp_path, run, sent, power):
    # Home sends Heat for a target above the room temperature even though only Off and Cool
    # are offered; refusing it made "turn on the AC" fail.
    async def scenario():
        _, thermostat = build(tmp_path)
        asked = []
        thermostat.queue = asked.append
        thermostat.mode.client_update_value(sent)
        return asked

    assert run(scenario()) == [{"power": power}]
