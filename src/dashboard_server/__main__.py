"""``python -m src.dashboard_server`` -- run the dashboard server in the foreground.

``aq dashboard serve`` calls :func:`main`; the managed ``aq dashboard start``
spawns this module.  Startup fails closed (spec §4): no bundle directory (a
source checkout) exits ``2`` with the way forward, a bundle that does not
verify exits ``1`` with the reason, and so does a busy port -- which is never
answered by trying the next one, because bookmarks and the installer need a
deterministic URL.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_BUNDLE = 2

#: uvicorn's own default, so the proxy is never the tighter WebSocket limit.
MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024
#: SIGTERM closes proxied WebSockets and SSE relays first; this bounds the rest.
GRACEFUL_SHUTDOWN_SECONDS = 5

logger = logging.getLogger("aq.dashboard_server")

NO_BUNDLE_HINT = (
    "No dashboard bundle is installed at {directory}.\n"
    "A source checkout has none: run the Vite dev server with `npm -w dashboard run dev`,\n"
    "or build and stage one with `aq install --restart-from dashboard.build`."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.dashboard_server",
        description="Serve the verified dashboard bundle and proxy the daemon's API.",
    )
    parser.add_argument("--host", help="Bind address (default: dashboard.server.host)")
    parser.add_argument("--port", type=int, help="Bind port (default: dashboard.server.port)")
    parser.add_argument("--api-url", help="Daemon API base (default: AQ_API_URL, then mcp_server)")
    parser.add_argument("--config", type=Path, help="config.yaml to read (default: ~/.agent-queue)")
    parser.add_argument("--bundle-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return parser


def bind_listener(host: str, port: int) -> socket.socket:
    """Bind the listening socket ourselves so a busy port is reported by its key."""
    address = "127.0.0.1" if host == "localhost" else host
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((address, port))
        sock.listen(2048)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def _uvicorn_server(app, *, log_level: str):
    import uvicorn

    class _Server(uvicorn.Server):
        async def shutdown(self, sockets=None) -> None:
            # Stop accepting first, then end what the proxy is relaying
            # (WebSockets close 1001, SSE relays are cancelled) so the
            # graceful wait below has nothing long-lived left to wait for.
            for server in self.servers:
                server.close()
            await app.proxy.close()
            await super().shutdown(sockets)

    config = uvicorn.Config(
        app,
        lifespan="on",
        # uvicorn's info lines name every WebSocket peer and path -- an access
        # log by another name (§2.2: there is none).  Its warnings still show.
        log_level="debug" if log_level == "debug" else "warning",
        access_log=False,
        # The edge gates key on the real peer; no client-supplied header may
        # replace it, and nothing here adds or trusts forwarding headers.
        proxy_headers=False,
        server_header=False,
        date_header=False,
        ws_max_size=MAX_WS_MESSAGE_BYTES,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return _Server(config)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from src.dashboard_server.bundle import dashboard_directory
    from src.dashboard_server.settings import SettingsError, load_settings

    try:
        settings = load_settings(
            args.config,
            host=args.host,
            port=args.port,
            api_url=args.api_url,
            bundle_directory=args.bundle_dir,
        )
    except SettingsError as error:
        print(f"dashboard server: {error}", file=sys.stderr)
        return EXIT_FAILED

    directory = settings.bundle_directory or dashboard_directory()
    if not directory.is_dir():
        print(NO_BUNDLE_HINT.format(directory=directory), file=sys.stderr)
        return EXIT_NO_BUNDLE

    from src.dashboard_server.app import create_app

    try:
        app = create_app(settings)
    except ValueError as error:
        print(
            f"dashboard server: the bundle at {directory} failed verification: {error}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        sock = bind_listener(settings.host, settings.port)
    except OSError as error:
        reason = error.strerror or error
        print(
            f"dashboard server: cannot listen on {settings.host}:{settings.port} ({reason}). "
            "Free the port or set dashboard.server.port.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    logger.info(
        "serving dashboard %s at %s, proxying %s",
        app.bundle.version, settings.url, settings.api_url,
    )
    server = _uvicorn_server(app, log_level=args.log_level)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        # uvicorn re-raises the signal it shut down for; Ctrl-C on a
        # foreground `aq dashboard serve` is the normal way to stop it.
        pass
    finally:
        sock.close()
    logger.info("dashboard server stopped")
    return EXIT_OK if server.started else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
