"""``skills.installed_drift`` — shipped aq-* skills vs. the installed copies.

``ensure_default_aq_skills`` is write-if-absent, so correcting a skill in-tree
leaves every existing install serving the old text with nothing to say so.  The
2026-09-08 CLI audit found exactly that: installed skills still documented
``aq task tree`` / ``aq task result`` and a ``--task-id`` close, all fixed in
``src/skills/`` long before.  These tests pin the report and the repair.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from src.doctor import default_registry
from src.doctor.models import Severity
from src.doctor.skill_checks import (
    CHECK_ID,
    _check_installed_skill_drift,
    _fix_installed_skill_drift,
    skill_checks,
)
from src.vault import ensure_default_aq_skills

#: ``src.doctor`` re-exports the ``skill_checks`` *function*, which shadows the
#: submodule of the same name on the package; import it explicitly so
#: ``monkeypatch.setattr`` lands on the module the checks read their helpers from.
skill_checks_module = importlib.import_module("src.doctor.skill_checks")


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """A shipped tree and one harness install, both under ``tmp_path``."""
    shipped = tmp_path / "shipped"
    (shipped / "aq-cli").mkdir(parents=True)
    (shipped / "aq-cli" / "SKILL.md").write_text("current guidance\n", encoding="utf-8")
    (shipped / "aq-tasks").mkdir()
    (shipped / "aq-tasks" / "SKILL.md").write_text("also current\n", encoding="utf-8")

    installed = tmp_path / "installed"
    for name in ("aq-cli", "aq-tasks"):
        (installed / name).mkdir(parents=True)
        (installed / name / "SKILL.md").write_text("current guidance\n", encoding="utf-8")
    (installed / "aq-tasks" / "SKILL.md").write_text("also current\n", encoding="utf-8")

    monkeypatch.setattr(
        skill_checks_module,
        "_shipped_skills",
        lambda: {
            name: str(shipped / name / "SKILL.md") for name in ("aq-cli", "aq-tasks")
        },
    )
    monkeypatch.setattr(skill_checks_module, "_installed_roots", lambda: [str(installed)])
    return shipped, installed


def test_the_check_is_registered_on_the_default_registry():
    assert CHECK_ID in {check.id for check in skill_checks()}
    registry = default_registry()
    ids = {check.id for check in getattr(registry, "_checks", {}).values()}
    assert CHECK_ID in ids, sorted(ids)


@pytest.mark.asyncio
async def test_matching_copies_report_ok(fake_install):
    result = await _check_installed_skill_drift(None)
    assert result.severity is Severity.OK
    assert result.fixable is False


@pytest.mark.asyncio
async def test_a_drifted_copy_is_reported_and_named(fake_install):
    _, installed = fake_install
    stale = installed / "aq-cli" / "SKILL.md"
    stale.write_text("aq task tree <id>   # deleted two releases ago\n", encoding="utf-8")

    result = await _check_installed_skill_drift(None)
    assert result.severity is Severity.WARN
    assert result.fixable is True
    assert result.data["drifted"] == [str(stale)]


@pytest.mark.asyncio
async def test_a_missing_install_is_not_drift(fake_install):
    """Write-if-absent seeding will create it; only a *differing* copy is drift."""
    _, installed = fake_install
    os.remove(installed / "aq-cli" / "SKILL.md")

    result = await _check_installed_skill_drift(None)
    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_the_fix_backs_up_before_re_copying(fake_install):
    _, installed = fake_install
    stale = installed / "aq-cli" / "SKILL.md"
    stale.write_text("stale, but the operator may have edited it\n", encoding="utf-8")

    result = await _fix_installed_skill_drift(None)

    assert result.severity is Severity.OK
    assert result.fix_applied is True
    assert stale.read_text(encoding="utf-8") == "current guidance\n"
    backup = installed / "aq-cli" / "SKILL.md.bak"
    assert backup.read_text(encoding="utf-8") == "stale, but the operator may have edited it\n"
    # And the repair is what the check then reports as healthy.
    assert (await _check_installed_skill_drift(None)).severity is Severity.OK


@pytest.mark.asyncio
async def test_no_harness_directory_is_info_not_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_checks_module, "_installed_roots", list)
    result = await _check_installed_skill_drift(None)
    assert result.severity is Severity.INFO
    assert "no harness skill directory" in result.detail


@pytest.mark.parametrize("optional_homes_present", [False, True])
async def test_knowledge_skill_seeds_supported_harnesses_and_preserves_edits(
    tmp_path, monkeypatch, optional_homes_present
):
    """Exercise real shipped discovery, seeding and drift repair in a fake home."""
    expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path, "expanduser", lambda path: str(tmp_path) if path == "~" else expanduser(path)
    )
    if optional_homes_present:
        for home in (".codex", ".gemini", "snap/gemini-cli/common/.gemini"):
            (tmp_path / home).mkdir(parents=True)

    source = Path(skill_checks_module._shipped_skills()["aq-knowledge"])
    result = ensure_default_aq_skills(str(tmp_path))
    for target, receipt in result.items():
        installed = Path(receipt["root"]) / "aq-knowledge" / "SKILL.md"
        if target == "claude" or optional_homes_present:
            assert "aq-knowledge" in receipt["created"]
            assert installed.read_bytes() == source.read_bytes()
        else:
            assert not installed.exists()

    customized = Path(result["claude"]["root"]) / "aq-knowledge" / "SKILL.md"
    custom_text = source.read_text(encoding="utf-8") + "\nOperator customization.\n"
    customized.write_text(custom_text, encoding="utf-8")
    second = ensure_default_aq_skills(str(tmp_path))
    assert "aq-knowledge" in second["claude"]["skipped"]
    assert customized.read_text(encoding="utf-8") == custom_text
    assert (await _check_installed_skill_drift(None)).data["drifted"] == [str(customized)]

    repaired = await _fix_installed_skill_drift(None)
    assert repaired.severity is Severity.OK
    assert customized.read_bytes() == source.read_bytes()
    assert customized.with_name("SKILL.md.bak").read_text(encoding="utf-8") == custom_text
