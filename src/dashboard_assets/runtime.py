"""Compatibility shim: the dashboard bundle verifier lives in :mod:`src.dashboard_server.bundle`.

A pre-change ``aq update`` keeps running its old code after it pulls, and
lazy-imports ``src.dashboard_assets.runtime.verify_dashboard_bundle`` from the
*new* checkout to verify the bundle it just rebuilt (docs/specs/dashboard-server.md
§6.2).  This module therefore stays, re-exports those names, and must not import
``fastapi`` or anything of the daemon's.  The daemon no longer serves the
dashboard at all: it answers ``/dashboard`` with a 404 hint, so there is nothing
to mount here any more.
"""

from __future__ import annotations

from src.dashboard_server.bundle import (
    DashboardBundle,
    dashboard_directory,
    installed_version,
    verify_dashboard_bundle,
)

__all__ = [
    "DashboardBundle",
    "dashboard_directory",
    "installed_version",
    "verify_dashboard_bundle",
]
