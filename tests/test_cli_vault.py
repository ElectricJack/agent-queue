"""Contract tests for ``aq vault migrate`` (api-cli plan 18)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner


def _report(*, errors: int = 0) -> dict:
    return {
        "projects_discovered": ["alpha"],
        "obsidian": {"action": "skip", "reason": "already migrated"},
        "notes": {"alpha": {"would_move": 2, "would_skip": 1, "moved": True}},
        "memory": {"alpha": {"would_copy": 1, "would_update": 0, "would_skip": 0,
                             "copied": True}},
        "rules": {"would_move": 0, "would_skip": 0, "moved": 1, "skipped": 0,
                  "errors": errors},
        "summary": {"total_moved": 3, "total_copied": 1, "total_skipped": 1,
                    "total_errors": errors},
    }


@pytest.fixture
def runner():
    return CliRunner()


def test_vault_migrate_requires_data_dir_and_forwards_dry_run_and_backend_options(
    runner, tmp_path,
):
    """Plan 18: no migration on a missing dir; exact forwarding otherwise."""
    from src.cli.app import cli

    # -- Missing data directory refuses before any migration runs ----------
    with patch("src.vault.run_vault_migration") as migrate:
        result = runner.invoke(cli, [
            "vault", "migrate", "--data-dir", str(tmp_path / "does-not-exist"),
        ])
    assert result.exit_code == 1, result.output
    assert "does not exist" in result.output
    migrate.assert_not_called()

    # -- Dry-run forwards the resolved dir, project filter, and flag -------
    data_dir = tmp_path / "aq-data"
    data_dir.mkdir()
    with patch("src.vault.run_vault_migration", return_value=_report()) as migrate:
        result = runner.invoke(cli, [
            "vault", "migrate", "--data-dir", str(data_dir), "--dry-run",
            "--project", "alpha", "--project", "beta",
        ])
    assert result.exit_code == 0, result.output
    migrate.assert_called_once_with(
        data_dir=str(data_dir), project_ids=["alpha", "beta"], dry_run=True,
    )
    assert "DRY RUN" in result.output
    assert "Preview complete" in result.output
    assert "no changes made" in result.output

    # -- Live run: no project filter means auto-discovery (None) -----------
    with patch("src.vault.run_vault_migration", return_value=_report()) as migrate:
        result = runner.invoke(cli, ["vault", "migrate", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    migrate.assert_called_once_with(
        data_dir=str(data_dir), project_ids=None, dry_run=False,
    )
    assert "Migration complete" in result.output

    # -- Migration errors surface as a nonzero exit ------------------------
    with patch("src.vault.run_vault_migration", return_value=_report(errors=2)):
        result = runner.invoke(cli, ["vault", "migrate", "--data-dir", str(data_dir)])
    assert result.exit_code == 1, result.output
    assert "Errors: 2" in result.output
