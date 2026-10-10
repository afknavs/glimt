"""The checks behind `glimt bridge test`: the simulated bridge must pass them all, and
a sloppy bridge must be told exactly what is wrong."""

from glimt import config as config_module, conformance

from conftest import demo_config

SLOPPY = '''
import time
from glimt.bridges.base import Bridge
from glimt.model import Device

class SloppyBridge(Bridge):
    type = "sloppy"

    def devices(self):
        time.sleep(0.6)                                             # I/O in devices()
        return [Device(id="wrong-id", name="x", kind="toaster", bridge=self.name,
                       capabilities={"heat": {"type": "int"}})]     # unknown kind, spec without min/max

    async def apply(self, device, changes):
        pass
'''


def test_the_simulated_bridge_conforms(run):
    results = run(conformance.check("virtual", demo_config(), write=True, settle=0.05))
    assert [str(result) for result in results if not result.ok] == []
    names = " | ".join(result.name for result in results)
    for expected in ("loads as a Bridge subclass", "returns exactly the configured devices", "start() succeeds",
                     "lamp: reports state after start()", "lamp: apply() returns quickly",
                     "invalid settings are rejected before reaching the bridge", "stop() returns promptly"):
        assert expected in names


def test_a_sloppy_bridge_is_told_what_to_fix(run, home):
    (home / "bridges").mkdir()
    (home / "bridges" / "sloppy.py").write_text(SLOPPY)
    config = config_module.check({"bridges": {"sloppy": {}}, "devices": {"toaster": {"bridge": "sloppy"}}})
    failed = {result.name: result.detail for result in run(conformance.check("sloppy", config, settle=0.05))
              if not result.ok}
    assert "configured ['toaster'], returned ['wrong-id']" in failed["devices() returns exactly the configured devices"]
    assert "devices() does no slow I/O" in failed
    problems = failed["wrong-id: device model is valid"]
    assert "kind 'toaster'" in problems and "capability 'heat' is missing ['max', 'min']" in problems
    assert "no report() call seen" in failed["wrong-id: reports state after start()"]


def test_asking_for_a_bridge_that_is_not_configured_says_so(run):
    results = run(conformance.check("hue", demo_config()))
    assert not results[0].ok and "add one" in results[0].detail
