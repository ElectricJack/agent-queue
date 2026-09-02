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


def build_openapi_spec() -> dict[str, Any]:
    """Build the OpenAPI spec from a throwaway app, with no daemon running.

    The orchestrator is never started and its database is never initialized —
    ``create_app()`` only stores references to them — so this is a fast, pure
    read of the route surface.  ``create_app()`` does write module-level
    state into :mod:`src.api.dependencies`; that state is saved and restored
    here so calling this from a test process leaves no residue.
    """
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import AppConfig, DiscordConfig
    from src.database import Database
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
    )
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
            (
                deps._orchestrator,
                deps._command_handler,
                deps._token_store,
                deps._require_session_token,
            ) = saved


def render_openapi_spec(spec: dict[str, Any]) -> str:
    """Serialize a spec exactly the way the committed ``openapi.json`` is written."""
    return json.dumps(spec, indent=2) + "\n"


def write_openapi_spec(path: Path | str | None = None) -> Path:
    """Generate the spec offline and write it to ``path`` (default ``SPEC_PATH``)."""
    target = Path(path) if path is not None else SPEC_PATH
    target.write_text(render_openapi_spec(build_openapi_spec()), encoding="utf-8")
    return target


if __name__ == "__main__":  # pragma: no cover - thin CLI for the regen scripts
    import sys

    written = write_openapi_spec(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Wrote {written}")
