"""Shared filesystem setup for newly registered projects."""

from __future__ import annotations

from pathlib import Path

from src.vault import ensure_vault_project_dirs


def ensure_project_storage(data_dir: str, project_id: str) -> None:
    """Create the task and standard vault directories for *project_id*."""
    Path(data_dir, "tasks", project_id).mkdir(parents=True, exist_ok=True)
    ensure_vault_project_dirs(data_dir, project_id)


__all__ = ["ensure_project_storage"]
