"""Remote access: what a cloud agent goes through to get in, and what a stranger cannot do."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from glimt import config as config_module, remote
from glimt.daemon import create_app

URL = "https://cue.example.test"
VERIFIER = "v" * 48
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
CALLBACK = "https://chatgpt.example/callback"
MCP = {"Accept": "application/json, text/event-stream"}


class Room:
    """Stands in for the daemon: records what the tools asked it to do."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None, **options):
        self.calls.append((method, path, body))
        if path.endswith("/sofa"):
            raise remote.daemon_client.DaemonError("Unknown device 'sofa'. Devices: lamp", 404)
        return {"ms": 12, "state": body or {}, "devices": []}


@pytest.fixture
def door():
    room = Room()
    app, access = remote.build_app({"url": URL}, ['lamp (light, "Desk lamp"): sets power'], request=room)
    with TestClient(app, base_url=URL, follow_redirects=False) as web:
        web.room, web.access = room, access
        yield web


def register(web, name="ChatGPT", callback=CALLBACK):
    return web.post("/register", json={"client_name": name, "redirect_uris": [callback],
                                       "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"], "token_endpoint_auth_method": "none"})


def begin(web, client_id):
    """Start a sign-in the way an app does; returns the page it lands on and its transaction."""
    answer = web.get("/authorize", params={
        "client_id": client_id, "response_type": "code", "redirect_uri": CALLBACK, "state": "s1",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": URL + "/mcp"})
    assert answer.status_code == 302 and answer.headers["location"].startswith(URL + "/pair?tx=")
    return answer.headers["location"], parse_qs(urlparse(answer.headers["location"]).query)["tx"][0]


def sign_in(web, name="ChatGPT"):
    client_id = register(web, name).json()["client_id"]
    _, tx = begin(web, client_id)
    web.access.approve(web.access.pending(tx).code)
    back = urlparse(web.get("/pair/status", params={"tx": tx}).json()["redirect"])
    tokens = web.post("/token", data={"grant_type": "authorization_code", "client_id": client_id,
                                      "code": parse_qs(back.query)["code"][0], "redirect_uri": CALLBACK,
                                      "code_verifier": VERIFIER})
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def call(web, token, method, params=None):
    return web.post("/mcp", headers={**MCP, "Authorization": "Bearer " + token},
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_an_app_gets_in_only_after_its_code_is_confirmed_on_this_computer(door):
    client_id = register(door).json()["client_id"]
    page_url, tx = begin(door, client_id)
    waiting = door.access.status()["pending"]
    assert [(item["name"], item["returns_to"]) for item in waiting] == [("ChatGPT", "chatgpt.example")]
    code = waiting[0]["code"]

    page = door.get(page_url)
    assert f"{code[:3]} {code[3:]}" in page.text and "Allow ChatGPT to use Glimt?" in page.text
    assert "<form" not in page.text and "<input" not in page.text          # nothing to type, so nothing to guess
    assert "script-src 'nonce-" in page.headers["content-security-policy"]
    assert door.get("/pair/status", params={"tx": tx}).json() == {"state": "waiting", "redirect": None}
    assert door.access.status()["apps"] == []                              # showing up is not being let in

    with pytest.raises(ValueError, match="No sign-in is waiting"):
        door.access.approve("000000" if code != "000000" else "111111")
    assert door.access.approve(f"{code[:3]} {code[3:]}") == {"ok": True, "name": "ChatGPT"}
    answer = door.get("/pair/status", params={"tx": tx}).json()
    back = urlparse(answer["redirect"])
    assert answer["state"] == "approved" and back.netloc == "chatgpt.example" and parse_qs(back.query)["state"] == ["s1"]
    with pytest.raises(ValueError):
        door.access.approve(code)                                          # a yes is used once

    grant = parse_qs(back.query)["code"][0]
    exchange = {"grant_type": "authorization_code", "client_id": client_id, "code": grant, "redirect_uri": CALLBACK}
    assert door.post("/token", data={**exchange, "code_verifier": "w" * 48}).status_code == 400   # PKCE
    tokens = door.post("/token", data={**exchange, "code_verifier": VERIFIER}).json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == remote.ACCESS_TTL and tokens["refresh_token"]
    assert door.post("/token", data={**exchange, "code_verifier": VERIFIER}).status_code == 400   # the grant too


def test_a_signed_in_app_can_cue_and_control_and_its_name_is_on_the_cue(door):
    _, tokens = sign_in(door)
    started = call(door, tokens["access_token"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert started.status_code == 200 and "room the user is physically in" in started.json()["result"]["instructions"]

    tools = {tool["name"]: tool for tool in call(door, tokens["access_token"], "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"signal", "clear_signal", "devices", "control", "action"}
    assert 'lamp (light, "Desk lamp")' in tools["control"]["description"]
    assert tools["devices"]["annotations"]["readOnlyHint"] is True
    assert tools["signal"]["annotations"]["destructiveHint"] is False

    said = call(door, tokens["access_token"], "tools/call", {
        "name": "signal", "arguments": {"name": "attention", "message": "Need a decision"}}).json()["result"]
    assert said["content"][0]["text"] == "done: signalled"
    assert door.room.calls[-1] == ("POST", "/v1/signal", {"name": "attention", "message": "Need a decision",
                                                          "source": "ChatGPT"})
    call(door, tokens["access_token"], "tools/call", {"name": "control", "arguments": {"device": "lamp", "power": True}})
    assert door.room.calls[-1] == ("POST", "/v1/devices/lamp", {"power": True})
    wrong = call(door, tokens["access_token"], "tools/call", {
        "name": "control", "arguments": {"device": "sofa", "power": True}}).json()["result"]
    assert wrong["isError"] and "Unknown device 'sofa'. Devices: lamp" in wrong["content"][0]["text"]   # says why
    assert door.access.status()["apps"][0]["used"]


def test_a_stranger_who_finds_the_address_gets_nothing(door):
    refused = door.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert refused.status_code == 401
    assert f'resource_metadata="{URL}/.well-known/oauth-protected-resource/mcp"' in refused.headers["www-authenticate"]
    assert call(door, "made-up", "tools/list").status_code == 401

    # It can register and ask, as any app must be able to, but that changes nothing that lasts.
    client_id = register(door, "Totally ChatGPT\x1b[2J").json()["client_id"]
    for _ in range(remote.MAX_PENDING + 5):
        begin(door, client_id)
    status = door.access.status()
    assert len(status["pending"]) == remote.MAX_PENDING and status["apps"] == []
    assert status["pending"][0]["name"] == "Totally ChatGPT[2J"            # cannot paint on your terminal
    assert not (remote.paths.secrets_dir() / "remote.json").exists()
    assert door.room.calls == []

    assert register(door, callback="javascript:alert(1)").status_code == 400
    assert register(door, callback="http://elsewhere.example/cb").status_code == 400
    assert register(door, callback="http://127.0.0.1:7777/cb").status_code == 201    # a desktop app on this machine

    _, tokens = sign_in(door)
    elsewhere = door.post("/mcp", headers={**MCP, "Authorization": "Bearer " + tokens["access_token"],
                                            "Host": "evil.example"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert elsewhere.status_code in (400, 403, 421)                         # only the address you chose is served


def test_tokens_renew_are_kept_hashed_and_die_with_the_app(door):
    client_id, tokens = sign_in(door)
    saved = (remote.paths.secrets_dir() / "remote.json").read_text()
    assert tokens["access_token"] not in saved and tokens["refresh_token"] not in saved
    assert (remote.paths.secrets_dir() / "remote.json").stat().st_mode & 0o777 == 0o600

    renew = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]}
    lost = door.post("/token", data=renew).json()                          # the computer slept before this arrived
    old = door.access._tokens[remote._digest(tokens["refresh_token"])]
    assert "expires" not in old                                            # so the app's own token is still good
    fresh = door.post("/token", data=renew).json()
    assert len({tokens["refresh_token"], lost["refresh_token"], fresh["refresh_token"]}) == 3
    assert call(door, fresh["access_token"], "tools/list").status_code == 200
    assert old["expires"] > 0                                              # the renewal arrived: the old one retires,
    assert door.post("/token", data=renew).status_code == 200              # after a moment's grace
    old["expires"] = 1
    assert door.post("/token", data=renew).status_code == 400

    _, tx = begin(door, client_id)                                         # signing in afresh drops what it held before
    door.access.approve(door.access.pending(tx).code)
    back = urlparse(door.get("/pair/status", params={"tx": tx}).json()["redirect"])
    again = door.post("/token", data={"grant_type": "authorization_code", "client_id": client_id,
                                      "code": parse_qs(back.query)["code"][0], "redirect_uri": CALLBACK,
                                      "code_verifier": VERIFIER}).json()
    assert call(door, fresh["access_token"], "tools/list").status_code == 401
    assert len(door.access._tokens) == 2 and len(door.access.status()["apps"]) == 1
    fresh = again

    # A restart loses nothing: the app is still known and its tokens still work.
    again, access = remote.build_app({"url": URL}, [], request=door.room)
    with TestClient(again, base_url=URL) as later:
        assert call(later, fresh["access_token"], "tools/list").status_code == 200
        assert [app["name"] for app in access.status()["apps"]] == ["ChatGPT"]
        assert access.revoke("chatgpt") == {"ok": True, "revoked": ["ChatGPT"]}
        assert call(later, fresh["access_token"], "tools/list").status_code == 401
        renew["refresh_token"] = fresh["refresh_token"]
        assert later.post("/token", data=renew).status_code in (400, 401)
        with pytest.raises(ValueError, match="No connected app"):
            access.revoke("chatgpt")

    # Tokens belong to the address they were issued for.
    client_id, tokens = sign_in(door)
    moved, _ = remote.build_app({"url": "https://other.example.test"}, [], request=door.room)
    with TestClient(moved, base_url="https://other.example.test") as other:
        assert call(other, tokens["access_token"], "tools/list").status_code == 401


def test_a_request_can_be_declined_and_cues_only_hides_the_controls(door):
    client_id = register(door).json()["client_id"]
    _, tx = begin(door, client_id)
    door.access.deny(door.access.status()["pending"][0]["code"])
    answer = door.get("/pair/status", params={"tx": tx}).json()
    assert answer["state"] == "denied" and "error=access_denied" in answer["redirect"]
    assert door.access.status() == {"apps": [], "pending": []}
    assert door.get("/pair", params={"tx": "nope"}).status_code == 410

    app, access = remote.build_app({"url": URL, "control": False}, ["lamp"], request=door.room)
    with TestClient(app, base_url=URL, follow_redirects=False) as quiet:
        quiet.room, quiet.access = door.room, access
        _, tokens = sign_in(quiet)
        names = [tool["name"] for tool in call(quiet, tokens["access_token"], "tools/list").json()["result"]["tools"]]
        assert names == ["signal", "clear_signal"]
        _, tx = begin(quiet, register(quiet).json()["client_id"])
        assert "It cannot control your devices" in quiet.get("/pair", params={"tx": tx}).text


def test_the_address_must_be_https_and_nothing_but_an_address():
    assert remote.public_url({"url": "https://name.tail1234.ts.net/"}) == "https://name.tail1234.ts.net"
    assert remote.public_url({"url": "https://cue.example.com:8443"}) == "https://cue.example.com:8443"
    assert remote.public_url({"url": "http://127.0.0.1:18771"}) == "http://127.0.0.1:18771"
    for wrong in ("", "http://cue.example.com", "https://cue.example.com/mcp", "cue.example.com", "https://"):
        with pytest.raises(ValueError, match="public https address"):
            remote.public_url({"url": wrong})


def test_the_daemon_serves_remote_only_when_asked_and_approves_through_the_local_api(home):
    from conftest import demo_config
    auth = {"Authorization": "Bearer secret"}
    with TestClient(create_app(demo_config(), "secret"), base_url="http://127.0.0.1:18770") as api:
        assert api.get("/v1/remote", headers=auth).json() == {"enabled": False, "error": None}
        assert api.get("/v1/health", headers=auth).json()["remote"] is None
        assert api.post("/v1/remote/approve", headers=auth, json={"code": "123456"}).status_code == 422
        assert api.post("/v1/remote/approve", json={"code": "123456"}).status_code == 401

    broken = {**demo_config(), "remote": {"enabled": True, "url": "http://not-https.example"}}
    with TestClient(create_app(broken, "secret"), base_url="http://127.0.0.1:18770") as api:
        status = api.get("/v1/remote", headers=auth).json()          # a bad address never stops the daemon
        assert status["enabled"] is False and "public https address" in status["error"]
        assert api.get("/v1/health", headers=auth).json()["devices"] == 3


def test_config_tables_are_replaced_without_touching_the_rest(home):
    path = home / "config.toml"
    path.write_text('# mine\n[daemon]\nport = 1  # keep\n\n[remote]\nenabled = false\n\n# my lamp\n[devices.lamp]\nbridge = "v"\n')
    config_module.write_table("remote", {"enabled": True, "url": URL})
    text = path.read_text()
    assert "# mine" in text and "port = 1  # keep" in text and "# my lamp\n[devices.lamp]" in text
    assert text.count("[remote]") == 1 and json.dumps(URL) in text
    config_module.write_table("remote", None)
    assert path.read_text() == '# mine\n[daemon]\nport = 1  # keep\n\n# my lamp\n[devices.lamp]\nbridge = "v"\n'


def test_doctor_tells_an_address_only_this_network_knows_from_a_public_one(monkeypatch):
    import socket
    import struct
    from glimt import doctor
    answer = {"count": 0}

    class Resolver:
        def __init__(self, *_): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def settimeout(self, _): pass
        def sendto(self, query, to):
            assert to == ("1.1.1.1", 53) and b"\x03cue\x07example\x03com\x00" in query
        def recv(self, _):
            if answer["count"] is None:
                raise OSError("offline")
            return struct.pack(">HHHHHH", 0x4375, 0x8180, 1, answer["count"], 0, 0)

    monkeypatch.setattr(socket, "socket", Resolver)
    assert doctor._listed("cue.example.com") is False      # the name exists, with no address yet
    answer["count"] = 2
    assert doctor._listed("cue.example.com") is True
    answer["count"] = None
    assert doctor._listed("cue.example.com") is None        # nobody to ask is not a finding
    assert doctor._listed("127.0.0.1") is None and doctor._listed("localhost") is None
