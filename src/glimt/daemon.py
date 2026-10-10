"""The local daemon: holds connections to your devices open and serves a small HTTP API.

It listens on 127.0.0.1 only and every request needs the bearer token from
~/.glimt/token. Nothing is exposed to your network. The one way in from outside is
remote.py, which is off unless you turn it on and listens on a port of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from importlib.resources import files

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from . import __version__, config as config_module, hooks, paths
from .bridges.base import BridgeError
from .hub import Hub, UnknownDevice
from .signals import SignalEngine

log = logging.getLogger("glimt.daemon")
ALLOWED_HOSTS = ("127.0.0.1", "localhost")


def create_app(config: dict, token: str) -> Starlette:
    hub = Hub(config)
    signals = SignalEngine(hub)
    homekit = remote = remote_error = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal homekit, remote, remote_error
        await hub.start()
        if config.get("remote", {}).get("enabled"):
            loop = asyncio.get_running_loop()
            try:
                from .remote import Remote
                remote = Remote(config["remote"], [device.summary() for device in hub.devices.values()],
                                int(config["remote"].get("port") or paths.port() + 1),
                                # It runs on its own thread; events belong to this loop.
                                changed=lambda: loop.call_soon_threadsafe(remote_changed))
                remote.start()
            except Exception as exc:  # a bad address must not take the lights down with it
                remote_error = str(exc)
                log.error("remote access is off: %s", exc)
        if config.get("homekit", {}).get("enabled"):
            from .homekit import HomeKitExport
            homekit = HomeKitExport(hub, config["homekit"])
            app.state.homekit = homekit
            asyncio.create_task(homekit.start())
        yield
        if remote:
            await asyncio.to_thread(remote.stop)  # the door to outside closes first
        await signals.shutdown()  # a cue in progress must not outlive the daemon
        if homekit:
            await homekit.stop()
        await hub.stop()

    def remote_status() -> dict:
        return remote.status() if remote else {"enabled": False, "error": remote_error}

    def remote_changed() -> None:
        hub.emit({"type": "remote", "remote": remote_status()})

    async def body(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Send a JSON object")
        return data

    def guarded(handler):
        async def wrapped(request: Request):
            # Host check defeats DNS rebinding; the token keeps other local users out.
            if request.url.hostname not in ALLOWED_HOSTS:
                return JSONResponse({"error": "Forbidden host"}, 403)
            sent = request.headers.get("authorization", "")
            if not secrets.compare_digest(sent, "Bearer " + token):
                return JSONResponse({"error": "Missing or wrong token (see ~/.glimt/token)"}, 401)
            try:
                return await handler(request)
            except UnknownDevice as exc:
                return JSONResponse({"error": exc.args[0]}, 404)
            except (ValueError, json.JSONDecodeError) as exc:
                return JSONResponse({"error": str(exc)}, 422)
            except BridgeError as exc:
                return JSONResponse({"error": str(exc)}, 502)
        return wrapped

    async def health(request):
        return JSONResponse({
            "ok": True, "version": __version__, "uptime": round(time.time() - hub.started),
            "devices": len(hub.devices),
            "bridges": {name: {"type": cfg["type"], "error": hub.bridge_errors.get(name)}
                        for name, cfg in config["bridges"].items()},
            "homekit": homekit.status() if homekit else None,
            "pid": os.getpid(), "mute": signals.muted(),
            "cli": hooks.launcher(),  # how the apps start this same Glimt for setup and agents
            "remote": remote_status() if remote or remote_error else None,
        })

    async def devices(request):
        return JSONResponse({"devices": hub.snapshot(), "signal": signals.active})

    async def device(request):
        return JSONResponse(hub.get(request.path_params["id"]).public())

    async def set_device(request):
        return JSONResponse(await hub.apply(request.path_params["id"], await body(request),
                                            source=request.headers.get("x-glimt-source", "api")))

    async def act(request):
        return JSONResponse(await hub.act(request.path_params["id"], request.path_params["action"],
                                          await body(request)))

    async def refresh(request):
        await hub.refresh()
        return JSONResponse({"devices": hub.snapshot()})

    async def fire(request):
        return JSONResponse(signals.fire(await body(request)))

    async def clear(request):
        return JSONResponse(await signals.clear())

    async def signal_status(request):
        return JSONResponse(signals.status())

    async def mute(request):
        minutes = (await body(request)).get("minutes")
        if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0):
            raise ValueError("minutes must be a positive number; leave it out to mute until unmuted")
        result = signals.mute(minutes)
        await signals.clear()  # you have seen it: a held cue goes too
        return JSONResponse(result)

    async def unmute(request):
        return JSONResponse(signals.unmute())

    async def remote_info(request):
        return JSONResponse(remote_status())

    def remote_access():
        if not remote:
            raise ValueError("Remote access is off. Turn it on with: glimt remote enable")
        return remote.access

    async def remote_approve(request):
        return JSONResponse(remote_access().approve((await body(request)).get("code", "")))

    async def remote_deny(request):
        return JSONResponse(remote_access().deny((await body(request)).get("code", "")))

    async def remote_revoke(request):
        return JSONResponse(remote_access().revoke(request.path_params["app"]))

    async def events(request):
        queue = hub.subscribe()

        async def stream():
            try:
                yield "data: " + json.dumps({"type": "hello", "devices": hub.snapshot(), "signal": signals.active,
                                             "mute": signals.muted(), "remote": remote_status()}) + "\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), 20)
                        if event is None:  # the daemon is shutting down
                            return
                        yield "data: " + json.dumps(event) + "\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def dashboard(request):
        html = files("glimt").joinpath("dashboard.html").read_text()
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; "
                                       "script-src 'unsafe-inline'; frame-ancestors 'none'"})

    app = Starlette(lifespan=lifespan, routes=[
        Route("/", dashboard),
        Route("/v1/health", guarded(health)),
        Route("/v1/devices", guarded(devices)),
        Route("/v1/devices/{id}", guarded(device), methods=["GET"]),
        Route("/v1/devices/{id}", guarded(set_device), methods=["POST"]),
        Route("/v1/devices/{id}/actions/{action}", guarded(act), methods=["POST"]),
        Route("/v1/refresh", guarded(refresh), methods=["POST"]),
        Route("/v1/signal", guarded(fire), methods=["POST"]),
        Route("/v1/signal", guarded(clear), methods=["DELETE"]),
        Route("/v1/signals", guarded(signal_status)),
        Route("/v1/mute", guarded(mute), methods=["POST"]),
        Route("/v1/mute", guarded(unmute), methods=["DELETE"]),
        Route("/v1/remote", guarded(remote_info)),
        Route("/v1/remote/approve", guarded(remote_approve), methods=["POST"]),
        Route("/v1/remote/deny", guarded(remote_deny), methods=["POST"]),
        Route("/v1/remote/apps/{app:path}", guarded(remote_revoke), methods=["DELETE"]),
        Route("/v1/events", guarded(events)),
    ])
    app.state.hub, app.state.signals = hub, signals
    return app


def run(config: dict | None = None, port: int | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = config if config is not None else config_module.load()
    port = port or int(config["daemon"].get("port", paths.port()))
    # [daemon] debug = ["glimt", "pyhap"] turns on verbose logs for just those loggers.
    for name in config["daemon"].get("debug", []):
        logging.getLogger(name).setLevel(logging.DEBUG)
    app = create_app(config, paths.token())
    log.info("glimt %s on http://127.0.0.1:%s with %d device(s)", __version__, port, len(config["devices"]))

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Open event streams never finish on their own; end them so shutdown is immediate.
            hub = app.state.hub
            if hub._loop:
                hub._loop.call_soon_threadsafe(hub.close_streams)
            super().handle_exit(sig, frame)

    Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", access_log=False,
                          log_level="warning", timeout_graceful_shutdown=3)).run()
