import pytest
from starlette.testclient import TestClient

from glimt.daemon import create_app

from conftest import demo_config

AUTH = {"Authorization": "Bearer secret"}


@pytest.fixture
def api():
    with TestClient(create_app(demo_config(), "secret"), base_url="http://127.0.0.1:18770") as client:
        yield client


def test_every_request_needs_the_token_and_a_local_host_header(api):
    assert api.get("/v1/devices").status_code == 401
    assert api.get("/v1/devices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    rebinding = api.get("/v1/devices", headers={**AUTH, "Host": "evil.example"})
    assert rebinding.status_code == 403
    assert api.get("/v1/health", headers=AUTH).json()["devices"] == 3


def test_devices_can_be_listed_changed_and_errors_are_plain(api):
    listing = api.get("/v1/devices", headers=AUTH).json()
    assert [device["id"] for device in listing["devices"]] == ["lamp", "strip", "fan"] and listing["signal"] is None
    changed = api.post("/v1/devices/lamp", headers=AUTH, json={"power": True, "color": "teal"}).json()
    assert changed["ok"] and changed["state"]["color"] == "#40c8e0"
    assert api.get("/v1/devices/lamp", headers=AUTH).json()["state"]["power"] is True

    missing = api.post("/v1/devices/sofa", headers=AUTH, json={"power": True})
    assert missing.status_code == 404 and "Devices: lamp, strip, fan" in missing.json()["error"]
    invalid = api.post("/v1/devices/lamp", headers=AUTH, json={"brightness": 400})
    assert invalid.status_code == 422 and "between 0 and 100" in invalid.json()["error"]
    assert api.post("/v1/devices/lamp/actions/dance", headers=AUTH).status_code == 422


def test_signals_fire_and_clear_over_http(api):
    fired = api.post("/v1/signal", headers=AUTH, json={"name": "working", "message": "Indexing"}).json()
    assert fired["targets"] == ["lamp", "strip"]
    status = api.get("/v1/signals", headers=AUTH).json()
    assert status["active"]["name"] == "working" and status["active"]["message"] == "Indexing"
    assert {"done", "attention", "error", "info", "working"} <= status["signals"].keys()
    assert api.delete("/v1/signal", headers=AUTH).json()["cleared"] is True
    assert api.get("/v1/signals", headers=AUTH).json()["active"] is None
    assert api.post("/v1/signal", headers=AUTH, json={"name": "nope"}).status_code == 422


def test_cues_can_be_muted_over_http_and_health_says_how_to_reach_the_cli(api):
    api.post("/v1/signal", headers=AUTH, json={"name": "attention"})
    muted = api.post("/v1/mute", headers=AUTH, json={"minutes": 45}).json()
    assert muted["ok"] and muted["mute"] > 0
    status = api.get("/v1/signals", headers=AUTH).json()
    assert status["active"] is None and status["mute"] == muted["mute"]      # muting also ends a held cue
    assert api.post("/v1/signal", headers=AUTH, json={"name": "done"}).json()["skipped"] == "muted"
    for wrong in ({"minutes": -5}, {"minutes": "soon"}, {"minutes": True}):
        assert api.post("/v1/mute", headers=AUTH, json=wrong).status_code == 422
    assert api.delete("/v1/mute", headers=AUTH).json() == {"ok": True, "mute": None}

    health = api.get("/v1/health", headers=AUTH).json()
    assert health["mute"] is None and isinstance(health["pid"], int)
    assert isinstance(health["cli"], list) and health["cli"]                  # the apps run this for setup and agents


def test_the_dashboard_is_served_with_a_strict_content_policy(api):
    page = api.get("/")
    assert page.status_code == 200 and "Glimt" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
