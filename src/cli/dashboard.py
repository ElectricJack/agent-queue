"""``aq dashboard`` -- the dashboard server process (docs/specs/dashboard-server.md §1).

A hand-written group: ``src/cli/auto_commands.py`` merges the generated
``state-*`` commands into it.  The process commands here are local, like
``aq start``: they are not ``CommandHandler`` commands and must work while
the daemon is down.
"""

from __future__ import annotations

import click

from .app import cli


@cli.group("dashboard")
def dashboard_group() -> None:
    """The dashboard server process and durable dashboard state."""


@dashboard_group.command("serve")
@click.option("--host", default=None, help="Bind address (default: dashboard.server.host).")
@click.option(
    "--port", type=click.IntRange(1, 65535), default=None,
    help="Bind port (default: dashboard.server.port, 8082).",
)
@click.pass_context
def dashboard_serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Serve the verified dashboard and proxy the daemon, in the foreground.

    Serves the bundle packaged with this install at / and forwards /api,
    /health, /ready and /ws to the daemon (the global --api-url, else
    AQ_API_URL, else mcp_server).  Logs go to stderr; stop it with Ctrl-C.
    A source checkout has no bundle and exits 2: run
    `npm -w dashboard run dev` there instead.
    """
    from .envelope import reject_json_mode

    reject_json_mode(ctx, "aq dashboard serve", "it runs a foreground server that logs to stderr")
    from src.dashboard_server.__main__ import main

    argv: list[str] = []
    if host is not None:
        argv += ["--host", host]
    if port is not None:
        argv += ["--port", str(port)]
    api_url = (ctx.find_root().obj or {}).get("api_url")
    if api_url:
        argv += ["--api-url", str(api_url)]
    code = main(argv)
    if code:
        raise SystemExit(code)
