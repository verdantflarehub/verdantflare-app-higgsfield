"""Two internal listeners, one credential owner, coordinated graceful shutdown."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import signal
import socket

import uvicorn

from .dashboard import DashboardAssets
from .channel import VideoChannel
from .management import create_management_app
from .server import create_app, gateway_owner


class Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The process coordinator owns SIGTERM/SIGINT for both listeners.
        yield


async def serve_pair(servers, sockets, stop):
    tasks = [asyncio.create_task(server.serve(sockets=[sock])) for server, sock in zip(servers, sockets, strict=True)]
    stopping = asyncio.create_task(stop.wait())
    cancelled = False
    try:
        await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        cancelled = True
    finally:
        # Never cancel an ASGI lifespan/worker thread while it may own OAuth.
        # A listener failure drains its peer before the outer owner lock closes.
        for server in servers:
            server.should_exit = True
        stopping.cancel()
        drain = asyncio.gather(*tasks, stopping, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                cancelled = True
        results = drain.result()[:-1]
    if cancelled:
        raise asyncio.CancelledError
    for result in results:
        if isinstance(result, BaseException):
            raise RuntimeError("gateway_listener_failed") from None
    if any(not server.started for server in servers):
        raise RuntimeError("gateway_listener_failed")


@contextmanager
def signals(stop):
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


async def run():
    assets = DashboardAssets(Path(os.environ.get("HIGGSFIELD_DASHBOARD_DIR", "/app/dashboard")))
    execution_token = os.environ["HIGGSFIELD_INTERNAL_TOKEN"]
    management_token = os.environ["HIGGSFIELD_MANAGEMENT_TOKEN"]
    station_id = os.environ["HIGGSFIELD_STATION_ID"]
    stop = asyncio.Event()
    with signals(stop), gateway_owner() as gateway, ExitStack() as stack:
        channel = VideoChannel(gateway.binding, gateway.organization)
        stack.callback(channel.close)
        # All credentials, binding and package checks precede either listener.
        apps = [create_app(gateway, execution_token), create_management_app(gateway,
            token=management_token, execution_token=execution_token, station_id=station_id, assets=assets, video_channel=channel)]
        sockets = []
        for port in (8000, 8001):
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.setblocking(False)
            sockets.append(sock)
        servers = [Server(uvicorn.Config(app, host="0.0.0.0", port=port, workers=1,
            access_log=False, log_level="warning", lifespan="on", timeout_graceful_shutdown=None))
            for app, port in zip(apps, (8000, 8001), strict=True)]
        await serve_pair(servers, sockets, stop)


def main():
    try:
        asyncio.run(run())
    except (OSError, ValueError, KeyError, RuntimeError):
        # Configuration/SDK diagnostics can contain private paths or addresses.
        raise SystemExit("gateway_startup_or_listener_failed") from None


if __name__ == "__main__":
    main()
