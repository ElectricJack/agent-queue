"""Compatibility shim: the dashboard bundle verifier lives in :mod:`src.dashboard_server.bundle`.

A pre-change ``aq update`` keeps running its old code after it pulls, and
lazy-imports ``src.dashboard_assets.runtime.verify_dashboard_bundle`` from the
*new* checkout to verify the bundle it just rebuilt (docs/specs/dashboard-server.md
§6.2).  This module therefore stays, re-exports that name, and must not import
``fastapi`` or anything of the daemon's -- Starlette, which the bundle module
already needs, is the heaviest thing it loads.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from src.dashboard_server.bundle import (
    DashboardBundle,
    dashboard_directory,
    installed_version,
    verify_bundle_inventory,
    verify_dashboard_bundle,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "DashboardBundle",
    "dashboard_directory",
    "installed_version",
    "mount_dashboard",
    "verify_dashboard_bundle",
]


# Interim: smart-meadow.4 deletes this class with ``mount_dashboard``.
class _DashboardStaticFiles(StaticFiles):
    """Static files with an index fallback for browser-routed dashboard URLs."""

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and Path(path).suffix == "":
            return await super().get_response("index.html", scope)
        return response


# Interim: smart-meadow.4 deletes this and the daemon's call in src/api/app.py.
def mount_dashboard(app: FastAPI, *, directory: Path | None = None) -> DashboardBundle | None:
    """Mount a verified release dashboard at ``/dashboard`` when one exists.

    It verifies the inventory but not the manifest's ``base``, so an install
    whose bundle predates ``base`` keeps its daemon starting until the dashboard
    server replaces this mount.
    """
    directory = directory or dashboard_directory()
    if not directory.is_dir():
        # Source checkouts deliberately do not carry generated dashboard output.
        return None
    # A release wheel that has an incomplete or altered package-data tree must
    # fail closed rather than serving an unverified browser application.
    bundle = verify_bundle_inventory(directory)

    # A mount at /dashboard only matches /dashboard/...; the bare path relies
    # on Starlette's slash redirect, which happens only when *no* route
    # matches.  The daemon mounts its MCP app at "/" after this, so a bare
    # /dashboard was swallowed by that catch-all and answered 404 -- the
    # first macOS install built the dashboard, restarted the daemon, and still
    # could not reach it.  Registered before the mount and before MCP's.
    async def _to_dashboard(request: Request) -> Response:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/dashboard/{query}", status_code=307)

    app.router.add_route(
        "/dashboard", _to_dashboard, methods=["GET", "HEAD"], include_in_schema=False
    )
    app.mount(
        "/dashboard",
        _DashboardStaticFiles(directory=str(bundle.directory), html=True),
        name="dashboard",
    )
    return bundle
