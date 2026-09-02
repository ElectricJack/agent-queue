"""Offline generation of the daemon's OpenAPI spec.

``openapi.json`` at the repo root is a committed build artifact: the typed
Python client in ``packages/aq-client/`` and the dashboard's TypeScript
client are both generated from it, so whenever ``src/api/models`` or a
codegen router changes, the committed spec has to be regenerated or every
typed consumer silently loses the change.

The regeneration scripts historically fetched the spec from a running
daemon, which made "regenerate the spec" a step nobody could take casually.
Nothing about the spec actually needs a daemon: ``create_app()`` builds the
whole route surface from the command registry, and FastAPI derives the spec
from the routes.  :func:`build_openapi_spec` does exactly that against a
throwaway orchestrator, so both ``scripts/regenerate-api-client.sh
--offline`` and the drift guard in ``tests/test_api_client_contract.py``
can produce the spec from a plain checkout.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "openapi.json"


#: Every module-level name :func:`src.api.app.create_app` writes into
#: :mod:`src.api.dependencies` (see ``src/api/app.py`` lines 91-115).  Building
#: a throwaway app must not leave any of them mutated for the caller, so they
#: are saved and restored around the build; keep this list in step with
#: ``create_app()``.
_CREATE_APP_DEPS_GLOBALS = (
    "_orchestrator",
    "_command_handler",
    "_token_store",
    "_require_session_token",
    "_health_provider",
    "_plan_content_provider",
    "_started_at",
    "_base_url",
)


def build_openapi_spec() -> dict[str, Any]:
    """Build the OpenAPI spec from a throwaway app, with no daemon running.

    The orchestrator is never started and its database is never initialized —
    ``create_app()`` only stores references to them — so this is a fast, pure
    read of the route surface.  ``create_app()`` does write module-level state
    into :mod:`src.api.dependencies`; every name in
    :data:`_CREATE_APP_DEPS_GLOBALS` is saved and restored here so calling this
    from a test process leaves the dependencies module as it found it.

    One known residue remains: ``create_app()`` starts the WebSocket manager,
    which subscribes to the orchestrator's event bus, and the app is never shut
    down.  The bus is the throwaway ``EventBus()`` created below and is dropped
    with the app, so the subscription dies with it.
    """
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import AppConfig, DiscordConfig
    from src.database import Database
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    saved = {name: getattr(deps, name) for name in _CREATE_APP_DEPS_GLOBALS}
    with tempfile.TemporaryDirectory(prefix="aq-openapi-") as tmp:
        root = Path(tmp)
        config = AppConfig(
            discord=DiscordConfig(bot_token="spec", guild_id="1"),
            workspace_dir=str(root / "workspaces"),
            database_path=str(root / "spec.db"),
            data_dir=str(root / "data"),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = Database(str(root / "spec.db"))
        orchestrator.git = MagicMock()
        orchestrator.bus = EventBus()
        try:
            return create_app(orchestrator, config).openapi()
        finally:
            for name, value in saved.items():
                setattr(deps, name, value)


def render_openapi_spec(spec: dict[str, Any]) -> str:
    """Serialize a spec exactly the way the committed ``openapi.json`` is written."""
    return json.dumps(spec, indent=2) + "\n"


def write_openapi_spec(
    path: Path | str | None = None,
    spec: dict[str, Any] | None = None,
) -> Path:
    """Write ``spec`` to ``path`` (defaults: built offline, ``SPEC_PATH``).

    ``spec`` lets a caller that already has one — the regeneration scripts
    fetching it from a running daemon — reuse :func:`render_openapi_spec`
    rather than re-implementing the on-disk format.  That format is asserted
    byte-for-byte by
    ``tests/test_api_client_contract.py::test_committed_openapi_json_matches_the_live_app_surface``.
    """
    target = Path(path) if path is not None else SPEC_PATH
    if spec is None:
        spec = build_openapi_spec()
    target.write_text(render_openapi_spec(spec), encoding="utf-8")
    return target


if __name__ == "__main__":  # pragma: no cover - thin CLI for the regen scripts
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Write the daemon's OpenAPI spec in the committed on-disk format.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help=f"where to write the spec (default: {SPEC_PATH})",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="render a spec read from stdin (e.g. curl'd from a running daemon) "
        "instead of building it from this checkout",
    )
    args = parser.parse_args()

    # json.load raises on empty/invalid stdin *before* anything is written, so a
    # failed fetch upstream in the pipe leaves the existing file untouched.
    from_stdin = json.load(sys.stdin) if args.stdin else None
    written = write_openapi_spec(args.path, spec=from_stdin)
    print(f"Wrote {written}")
