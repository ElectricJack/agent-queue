"""The ``project.created`` event both project-creation paths emit.

Onboarding (``ProjectOnboardingService``) registers a project together with its
primary workspace; ``create_project`` registers a bare project.  Both announce
it with one ``project.created`` event (schema in ``src/event_schemas.py``) so a
playbook can apply per-project defaults without polling the project table.

The event carries facts only.  ``workspace_in_vault`` says whether the primary
workspace is the AQ vault or lies beneath it; what to do about that is the
subscribing playbook's policy.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_CREATED = "project.created"


def path_in_vault(path: str, vault_root: str) -> bool:
    """True when *path* is *vault_root* or lies beneath it, symlinks resolved."""
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(vault_root))


def project_created_payload(
    *,
    project_id: str,
    name: str,
    source: str,
    vault_root: str,
    source_type: str | None = None,
    workspace_id: str | None = None,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """Build a ``project.created`` payload; workspace fields only when one exists."""
    payload: dict[str, Any] = {"project_id": project_id, "name": name, "source": source}
    if source_type is not None:
        payload["source_type"] = source_type
    if workspace_path is not None:
        if workspace_id is not None:
            payload["workspace_id"] = workspace_id
        payload["workspace_path"] = workspace_path
        payload["workspace_in_vault"] = path_in_vault(workspace_path, vault_root)
    return payload


async def emit_project_created(bus: Any, payload: dict[str, Any]) -> None:
    """Emit ``project.created`` without failing the caller.

    The project is already registered when this runs, so a bus failure must not
    turn a successful creation into an error (or, in onboarding, trigger
    compensation of the project it just registered).
    """
    if bus is None:
        return
    try:
        await bus.emit(PROJECT_CREATED, payload)
    except Exception:
        logger.warning(
            "project.created emit failed for %s", payload.get("project_id"), exc_info=True
        )


__all__ = [
    "PROJECT_CREATED",
    "emit_project_created",
    "path_in_vault",
    "project_created_payload",
]
