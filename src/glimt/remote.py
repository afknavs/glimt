"""Remote access: a second door for agents that run in someone else's cloud (ChatGPT, a
dot, Claude on the web) and so can never reach 127.0.0.1.

It serves the MCP tools over HTTP on its own local port. You point a tunnel at that port
and at nothing else, so the dashboard, the local API and its token are never on the
internet. Finding the address is not enough to get in:

* an app signs in with OAuth 2.1 (PKCE), and its sign-in page shows a code;
* it is let in only when you confirm that code on this computer. Nothing typed into a web
  page can grant access, so there is nothing for a stranger to guess;
* tokens are kept hashed, are bound to the public address, and each app can be revoked.

The listener has its own thread and event loop and talks to the daemon the way every
other client does, so traffic from outside cannot stall the loop that drives your devices.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from functools import partial
from typing import Callable
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import client as daemon_client, mcp_server, paths

log = logging.getLogger("glimt.remote")

ACCESS_TTL = 3600       # an access token; the app renews it by itself ([remote] token_minutes changes it)
CODE_TTL = 300          # how long an app has to collect its tokens once you have said yes
PENDING_TTL = 600       # how long a sign-in waits for you
REFRESH_GRACE = 120     # once its replacement is in use, an old refresh token works this much longer
MAX_PENDING = 8         # sign-ins waiting at once, and registrations nobody has approved:
MAX_UNAPPROVED = 64     # both live in memory only, so a stranger cannot fill the disk
SAVE_EVERY = 600        # "last used" reaches the disk at most this often
UNSAFE_SCHEMES = ("javascript", "data", "vbscript", "file", "blob", "about")
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def public_url(settings: dict) -> str:
    """The address the outside world uses, checked: https, a host, and nothing after it."""
    url = str(settings.get("url") or "").strip().rstrip("/")
    parsed = urlparse(url)
    local = parsed.scheme == "http" and parsed.hostname in LOOPBACK
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or not (parsed.scheme == "https" or local):
        raise ValueError('[remote] url must be the public https address alone, like "https://name.example.com"')
    return url


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _plain(text: str | None, limit: int = 40) -> str:
    """A name an app chose for itself, made safe to print in a terminal or a page."""
    return "".join(ch for ch in (text or "") if ch.isprintable()).strip()[:limit]


@dataclass
class Pending:
    code: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires: float
    state: str = "waiting"  # waiting | approved | denied
    redirect: str | None = None

    @property
    def name(self) -> str:
        return _plain(self.client.client_name) or self.returns_to or "An app"

    @property
    def returns_to(self) -> str:
        return urlparse(str(self.params.redirect_uri)).hostname or ""


class Access:
    """Who may come in. The OAuth authorization server the MCP library asks (the async
    methods), and what the local API and CLI use to approve, list and revoke (the rest)."""

    def __init__(self, url: str, changed: Callable[[], None] = lambda: None, access_ttl: int = ACCESS_TTL) -> None:
        self.url, self.resource, self.changed, self.access_ttl = url, url + "/mcp", changed, access_ttl
        self._lock = threading.RLock()  # the listener's loop and the daemon's both come here
        self._file = paths.secrets_dir() / "remote.json"
        try:
            saved = json.loads(self._file.read_text()) if self._file.exists() else {}
        except (OSError, ValueError):
            log.warning("%s is unreadable; every app will have to sign in again", self._file)
            saved = {}
        self._apps: dict[str, dict] = saved.get("apps", {})
        self._tokens: dict[str, dict] = saved.get("tokens", {})
        self._unapproved: dict[str, tuple[float, OAuthClientInformationFull]] = {}
        self._pending: dict[str, Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._saved = time.time()

    # ---- what you do on this computer -------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            self._expire()
            return {
                "apps": [{"id": key, "name": app["name"], "approved": app["approved"], "used": app.get("used")}
                         for key, app in self._apps.items()],
                "pending": [{"code": item.code, "name": item.name, "returns_to": item.returns_to,
                             "expires": int(item.expires)}
                            for item in self._pending.values() if item.state == "waiting"],
            }

    def approve(self, code: str) -> dict:
        with self._lock:
            item = self._waiting(code)
            params, now = item.params, time.time()
            grant = secrets.token_urlsafe(32)
            self._codes[grant] = AuthorizationCode(
                code=grant, scopes=params.scopes or [], expires_at=now + CODE_TTL, client_id=item.client.client_id,
                code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.resource)
            item.state = "approved"
            item.redirect = construct_redirect_uri(str(params.redirect_uri), code=grant, state=params.state)
            self._apps.setdefault(item.client.client_id, {
                "name": item.name, "approved": int(now), "used": None,
                "client": item.client.model_dump(mode="json", exclude_none=True)})
            self._unapproved.pop(item.client.client_id, None)
            self._save()
            result = {"ok": True, "name": item.name}
        self.changed()
        return result

    def deny(self, code: str) -> dict:
        with self._lock:
            item = self._waiting(code)
            item.state = "denied"
            item.redirect = construct_redirect_uri(str(item.params.redirect_uri), error="access_denied",
                                                   state=item.params.state)
        self.changed()
        return {"ok": True, "name": item.name}

    def revoke(self, which: str) -> dict:
        """Forget an app, by id or by name, and every token it holds."""
        with self._lock:
            ids = [key for key, app in self._apps.items() if which in (key, app["name"])
                   or which.lower() == app["name"].lower()]
            if not ids:
                raise ValueError(f"No connected app is called '{which}'. See: glimt remote")
            names = sorted({self._apps[key]["name"] for key in ids})
            for key in ids:
                self._forget(key)
            self._save()
        self.changed()
        return {"ok": True, "revoked": names}

    def name_of_caller(self) -> str:
        """Which app is making this request: it becomes the name on the cue."""
        token = get_access_token()
        with self._lock:
            return self._apps.get(token.client_id, {}).get("name", "remote") if token else "remote"

    def pending(self, tx: str) -> Pending | None:
        with self._lock:
            self._expire()
            return self._pending.get(tx)

    def flush(self) -> None:
        with self._lock:
            self._save()

    # ---- the OAuth authorization server ------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            self._expire()
            if client_id in self._apps:
                return OAuthClientInformationFull.model_validate(self._apps[client_id]["client"])
            return self._unapproved[client_id][1] if client_id in self._unapproved else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            if parsed.scheme in UNSAFE_SCHEMES or (parsed.scheme == "http" and parsed.hostname not in LOOPBACK):
                raise RegistrationError("invalid_redirect_uri", "redirect_uris must be https, or http on this computer")
        with self._lock:
            self._expire()
            while len(self._unapproved) >= MAX_UNAPPROVED:
                del self._unapproved[next(iter(self._unapproved))]
            self._unapproved[client_info.client_id] = (time.time() + PENDING_TTL * 6, client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if urlparse(str(params.redirect_uri)).scheme in UNSAFE_SCHEMES:
            raise AuthorizeError("invalid_request", "That redirect_uri is not allowed")
        with self._lock:
            self._expire()
            waiting = [tx for tx, item in self._pending.items() if item.state == "waiting"]
            for tx in waiting[:max(0, len(waiting) - MAX_PENDING + 1)]:
                del self._pending[tx]  # the oldest gives way; yours is the one you just started
            taken, code = {item.code for item in self._pending.values()}, None
            while code is None or code in taken:
                code = f"{secrets.randbelow(10 ** 6):06d}"
            tx = secrets.token_urlsafe(24)
            self._pending[tx] = Pending(code, client, params, time.time() + PENDING_TTL)
        self.changed()
        return f"{self.url}/pair?tx={tx}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        with self._lock:
            grant = self._codes.get(authorization_code)
            return grant if grant and grant.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        with self._lock:
            self._codes.pop(authorization_code.code, None)  # one use
            self._drop_tokens(client.client_id)  # a fresh sign-in replaces whatever the app held before
            return self._issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        with self._lock:
            record = self._live(refresh_token, "refresh")
            if not record or record["client"] != client.client_id:
                return None
            return RefreshToken(token=refresh_token, client_id=record["client"], scopes=record["scopes"],
                                expires_at=record.get("expires"), resource=record["resource"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        with self._lock:
            # Rotate, but the old token is retired only once the app is seen using what it gets
            # here (_live). A computer can fall asleep between issuing an answer and delivering
            # it; the app then asks again with the token it still has, and must not be locked out.
            return self._issue(client.client_id, scopes, replaces=_digest(refresh_token.token))

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._lock:
            record = self._live(token, "access")
            if not record:
                return None
            self._apps[record["client"]]["used"] = int(time.time())
            if time.time() - self._saved > SAVE_EVERY:
                self._save()
            return AccessToken(token=token, client_id=record["client"], scopes=record["scopes"],
                               expires_at=record["expires"], resource=record["resource"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:  # the app disconnected itself: it and everything it holds go
            self._forget(token.client_id)
            self._save()
        self.changed()

    # ---- internals (call with the lock held) -------------------------------------------

    def _waiting(self, code: str) -> Pending:
        self._expire()
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        for item in self._pending.values():
            if item.state == "waiting" and secrets.compare_digest(item.code, digits):
                return item
        raise ValueError("No sign-in is waiting with that code. A code lasts 10 minutes; start again from the app.")

    def _issue(self, client_id: str, scopes: list[str], replaces: str | None = None) -> OAuthToken:
        if client_id not in self._apps:  # revoked between your yes and its collecting the tokens
            raise TokenError("invalid_grant", "This app is no longer allowed")
        access, refresh, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())
        common = {"client": client_id, "scopes": scopes, "resource": self.resource}
        if replaces:
            common["replaces"] = replaces
        self._tokens[_digest(access)] = {"kind": "access", "expires": now + self.access_ttl, **common}
        self._tokens[_digest(refresh)] = {"kind": "refresh", **common}
        self._save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.access_ttl,
                          refresh_token=refresh, scope=" ".join(scopes) or None)

    def _live(self, token: str, kind: str) -> dict | None:
        record = self._tokens.get(_digest(token))
        if not record or record["kind"] != kind or record["client"] not in self._apps:
            return None
        if record.get("expires") and record["expires"] < time.time():
            return None
        old = self._tokens.get(record.pop("replaces", None))
        if old:  # the app received its renewal: the token it traded in can go, and its deadline never moves back
            old.setdefault("expires", int(time.time()) + REFRESH_GRACE)
        return record

    def _drop_tokens(self, client_id: str) -> None:
        self._tokens = {key: record for key, record in self._tokens.items() if record["client"] != client_id}

    def _forget(self, client_id: str) -> None:
        self._apps.pop(client_id, None)
        self._drop_tokens(client_id)

    def _expire(self) -> None:
        now = time.time()
        self._pending = {tx: item for tx, item in self._pending.items() if item.expires > now}
        self._unapproved = {key: entry for key, entry in self._unapproved.items() if entry[0] > now}
        self._codes = {key: grant for key, grant in self._codes.items() if grant.expires_at > now}

    def _save(self) -> None:
        now = time.time()
        self._tokens = {key: record for key, record in self._tokens.items()
                        if not record.get("expires") or record["expires"] > now}
        paths.write_private(self._file, json.dumps({"apps": self._apps, "tokens": self._tokens}, indent=1))
        self._saved = now


# ---- the sign-in page ------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Glimt</title>
<style>
  :root { color-scheme: light dark; --ink: #1d1d1f; --soft: #6e6e73; --line: #d2d2d7; --page: #fbfbfd; }
  @media (prefers-color-scheme: dark) { :root { --ink: #f5f5f7; --soft: #a1a1a6; --line: #424245; --page: #000; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--page); color: var(--ink);
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  main { max-width: 26rem; padding: 2rem 1.5rem; text-align: center; }
  h1 { font-size: 1.6rem; line-height: 1.2; font-weight: 600; margin: 0 0 .6rem; letter-spacing: -.01em; }
  p { margin: 0 0 1rem; color: var(--soft); }
  .code { font: 600 2.6rem/1 ui-monospace, "SF Mono", Menlo, monospace; letter-spacing: .12em; color: var(--ink);
          margin: 1.8rem 0 1.4rem; font-variant-numeric: tabular-nums; }
  code { font: .82em ui-monospace, "SF Mono", Menlo, monospace; color: var(--ink); white-space: nowrap; }
  small { display: block; margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--line);
          color: var(--soft); font-size: .8rem; }
</style></head><body><main>
<h1 id="title">Allow __NAME__ to use Glimt?</h1>
<p id="what">__WHAT__</p>
<div class="code" id="code" aria-label="Confirmation code">__CODE__</div>
<p id="how">Confirm this code on your computer: choose Allow in the Glimt menu, or run
<code>glimt remote approve __DIGITS__</code></p>
<small>Access is granted on your computer, never on this page. You will return to __HOST__.</small>
</main>
<script nonce="__NONCE__">
  const say = (title, how) => { document.getElementById("title").textContent = title;
    document.getElementById("how").textContent = how;
    for (const id of ["what", "code"]) document.getElementById(id).hidden = true; };
  const poll = async () => {
    let answer = {state: "waiting"};
    try { answer = await (await fetch("pair/status?tx=__TX__", {cache: "no-store"})).json(); } catch (error) {}
    if (answer.state === "waiting") return setTimeout(poll, 1500);
    if (answer.state === "approved") say("Connected", "Taking you back…");
    else if (answer.state === "denied") say("Declined", "Nothing was shared.");
    else return say("This request has expired", "Start again from the app you were connecting.");
    if (answer.redirect) setTimeout(() => location.replace(answer.redirect), 600);
  };
  poll();
</script></body></html>
"""
GONE = PAGE.split("<main>")[0] + ("<main><h1>This request has expired</h1>"
                                  "<p>Start again from the app you were connecting.</p></main></body></html>")


def _headers(nonce: str = "") -> dict:
    script = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if nonce else ""
    return {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; {script}"
                                       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}


class Journal:
    """One line per request from outside, for `[daemon] debug = ["glimt.remote"]`: enough to
    see what an app is doing, with no query strings, tokens or bodies."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not log.isEnabledFor(logging.DEBUG):
            return await self.app(scope, receive, send)
        agent = dict(scope["headers"]).get(b"user-agent", b"").decode("latin-1")[:40]

        async def noted(message) -> None:
            if message["type"] == "http.response.start":
                log.debug("%s %s -> %s  (%s)", scope["method"], scope["path"], message["status"], _plain(agent))
            await send(message)

        await self.app(scope, receive, noted)


def build_app(settings: dict, lines: list[str], *, request: Callable[..., dict] | None = None,
              changed: Callable[[], None] = lambda: None) -> tuple[Starlette, Access]:
    """The app a tunnel is pointed at, and the Access that guards it."""
    url = public_url(settings)
    control = bool(settings.get("control", True))
    access = Access(url, changed, int(float(settings.get("token_minutes", ACCESS_TTL / 60)) * 60))
    server = mcp_server.build(
        # Never autostart: a daemon that is shutting down must not launch its own successor.
        request=request or partial(daemon_client.request, autostart=False), lines=lines,
        source=access.name_of_caller, control=control,
        instructions=mcp_server.REMOTE_INSTRUCTIONS + (mcp_server.REMOTE_CONTROL if control else ""),
        auth_server_provider=access,
        auth=AuthSettings(issuer_url=url, resource_server_url=access.resource, validate_token_resource=True,
                          client_registration_options=ClientRegistrationOptions(enabled=True),
                          revocation_options=RevocationOptions(enabled=True)))

    @server.custom_route("/pair", methods=["GET"])
    async def pair(request: Request):
        tx = request.query_params.get("tx", "")
        item = access.pending(tx)
        if not item or item.state != "waiting":
            return HTMLResponse(GONE, 410, headers=_headers())
        nonce = secrets.token_urlsafe(12)
        what = ("It will be able to cue your lights and control the devices in your room." if control
                else "It will be able to cue your lights. It cannot control your devices.")
        page = PAGE
        for key, value in (("__NAME__", html.escape(item.name)), ("__WHAT__", what), ("__DIGITS__", item.code),
                           ("__CODE__", f"{item.code[:3]} {item.code[3:]}"), ("__NONCE__", nonce),
                           ("__HOST__", html.escape(item.returns_to or "the app")), ("__TX__", tx)):
            page = page.replace(key, value)
        return HTMLResponse(page, headers=_headers(nonce))

    @server.custom_route("/pair/status", methods=["GET"])
    async def pair_status(request: Request):
        item = access.pending(request.query_params.get("tx", ""))
        answer = {"state": item.state, "redirect": item.redirect} if item else {"state": "gone"}
        return JSONResponse(answer, headers={"Cache-Control": "no-store"})

    @server.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def resource_metadata(request: Request):  # some clients look here rather than under /mcp
        return JSONResponse({"resource": access.resource, "authorization_servers": [url],
                             "bearer_methods_supported": ["header"]},
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"})

    # Stateless, plain JSON answers: nothing to lose when the daemon restarts, and no
    # long-lived stream for a tunnel to cut.
    app = server.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[urlparse(url).netloc], allowed_origins=[url]))
    app.add_middleware(Journal)
    return app, access


class Remote:
    """Runs the remote listener beside the daemon, on a thread of its own."""

    def __init__(self, settings: dict, lines: list[str], port: int, changed: Callable[[], None] = lambda: None) -> None:
        import uvicorn

        self.port, self.error = port, None
        self.url, self.control = public_url(settings), bool(settings.get("control", True))
        app, self.access = build_app(settings, lines, changed=changed)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio",
                                                     access_log=False, log_level="warning",
                                                     timeout_graceful_shutdown=2))
        self._thread = threading.Thread(target=self._run, name="glimt-remote", daemon=True)

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:  # uvicorn exits the process when it cannot bind; here that is this thread
            self.error = f"could not listen on 127.0.0.1:{self.port}" + (f" ({exc})" if str(exc) not in ("", "1") else "")
            log.error("remote access: %s", self.error)

    def start(self) -> None:
        self._thread.start()
        log.info("remote access for %s on 127.0.0.1:%s", self.url, self.port)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(5)
        self.access.flush()

    def status(self) -> dict:
        return {"enabled": True, "url": self.url, "endpoint": self.access.resource, "port": self.port,
                "control": self.control, "error": self.error, **self.access.status()}
