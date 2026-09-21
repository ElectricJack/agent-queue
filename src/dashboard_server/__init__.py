"""The dashboard server: the verified bundle plus a same-origin proxy to the daemon.

A small process separate from the daemon (docs/specs/dashboard-server.md). It
serves the release dashboard with SPA fallback and reverse-proxies ``/api``,
``/health``, ``/ready`` and ``/ws`` to the daemon, which is what the Vite dev
server does in a source checkout, so the browser stays same-origin and the
daemon exposes an API and nothing else.

Import boundary (§1): this package imports :mod:`src.config`, the standard
library, Starlette, uvicorn and aiohttp -- never ``src.api``,
``src.orchestrator``, ``src.database``, ``src.commands`` or ``fastapi``.  This
``__init__`` imports nothing, so ``src.dashboard_server.bundle`` stays cheap to
import from the installer.
"""
