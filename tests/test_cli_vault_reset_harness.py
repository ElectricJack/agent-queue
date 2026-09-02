"""``aq vault reset-harness`` — restore a vault harness copy from the shipped file."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.sessions.harness_manifest import shipped_harness_dir
from src.vault import ensure_default_harnesses


@pytest.fixture
def runner():
    return CliRunner()


def _copy(tmp_path, name):
    return tmp_path / "vault" / "harnesses" / f"{name}.md"


def test_restores_an_edited_copy(runner, tmp_path):
    ensure_default_harnesses(str(tmp_path))
    _copy(tmp_path, "claude").write_text("# operator\n", encoding="utf-8")

    result = runner.invoke(cli, ["vault", "reset-harness", "claude", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "restored shipped file (was edited)" in result.output
    shipped = Path(shipped_harness_dir()) / "claude.md"
    assert _copy(tmp_path, "claude").read_bytes() == shipped.read_bytes()


def test_current_copy_is_reported_not_rewritten(runner, tmp_path):
    ensure_default_harnesses(str(tmp_path))
    result = runner.invoke(cli, ["vault", "reset-harness", "codex", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "already matches" in result.output


def test_all_resets_every_shipped_harness(runner, tmp_path):
    result = runner.invoke(cli, ["vault", "reset-harness", "--all", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for name in ("claude", "codex", "gemini"):
        assert _copy(tmp_path, name).is_file()
        assert f"{name}: restored shipped file (was missing)" in result.output


def test_unknown_name_fails(runner, tmp_path):
    result = runner.invoke(cli, ["vault", "reset-harness", "nope", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no shipped harness named 'nope'" in result.output


def test_no_names_fails(runner, tmp_path):
    result = runner.invoke(cli, ["vault", "reset-harness", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "--all" in result.output


def test_dry_run_reports_status_without_writing(runner, tmp_path):
    ensure_default_harnesses(str(tmp_path))
    _copy(tmp_path, "gemini").write_text("# operator\n", encoding="utf-8")

    result = runner.invoke(
        cli, ["vault", "reset-harness", "--dry-run", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "gemini" in result.output and "edited" in result.output
    assert "current" in result.output
    assert _copy(tmp_path, "gemini").read_text(encoding="utf-8") == "# operator\n"
