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
import re
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
@pytest.mark.parametrize("skill_name", ["aq-knowledge", "aq-bazel-upgrade"])
async def test_shipped_skill_seeds_supported_harnesses_and_preserves_edits(
    tmp_path, monkeypatch, optional_homes_present, skill_name
):
    """Exercise real shipped discovery, seeding and drift repair in a fake home."""
    expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path, "expanduser", lambda path: str(tmp_path) if path == "~" else expanduser(path)
    )
    if optional_homes_present:
        for home in (".codex", ".gemini", "snap/gemini-cli/common/.gemini"):
            (tmp_path / home).mkdir(parents=True)

    source = Path(skill_checks_module._shipped_skills()[skill_name])
    result = ensure_default_aq_skills(str(tmp_path))
    for target, receipt in result.items():
        installed = Path(receipt["root"]) / skill_name / "SKILL.md"
        if target == "claude" or optional_homes_present:
            assert skill_name in receipt["created"]
            assert installed.read_bytes() == source.read_bytes()
        else:
            assert not installed.exists()

    customized = Path(result["claude"]["root"]) / skill_name / "SKILL.md"
    custom_text = source.read_text(encoding="utf-8") + "\nOperator customization.\n"
    customized.write_text(custom_text, encoding="utf-8")
    second = ensure_default_aq_skills(str(tmp_path))
    assert skill_name in second["claude"]["skipped"]
    assert customized.read_text(encoding="utf-8") == custom_text
    assert (await _check_installed_skill_drift(None)).data["drifted"] == [str(customized)]

    repaired = await _fix_installed_skill_drift(None)
    assert repaired.severity is Severity.OK
    assert customized.read_bytes() == source.read_bytes()
    assert customized.with_name("SKILL.md.bak").read_text(encoding="utf-8") == custom_text


@pytest.mark.asyncio
async def test_a_drifted_reference_file_is_reported_and_repaired(fake_install):
    """Reference files beside SKILL.md are skill content too, so they drift too."""
    shipped, installed = fake_install
    (shipped / "aq-tasks" / "reference").mkdir()
    (shipped / "aq-tasks" / "reference" / "graphs.md").write_text("current\n", encoding="utf-8")
    stale = installed / "aq-tasks" / "reference" / "graphs.md"
    stale.parent.mkdir()
    stale.write_text("stale\n", encoding="utf-8")

    result = await _check_installed_skill_drift(None)
    assert result.severity is Severity.WARN
    assert result.data["drifted"] == [str(stale)]

    repaired = await _fix_installed_skill_drift(None)
    assert repaired.fix_applied is True
    assert stale.read_text(encoding="utf-8") == "current\n"
    assert stale.with_name("graphs.md.bak").read_text(encoding="utf-8") == "stale\n"
    assert (await _check_installed_skill_drift(None)).severity is Severity.OK


@pytest.mark.asyncio
async def test_a_missing_reference_beside_an_installed_skill_is_copied_in(fake_install):
    """A skill split after it was installed links to a file the install lacks."""
    shipped, installed = fake_install
    (shipped / "aq-tasks" / "reference").mkdir()
    (shipped / "aq-tasks" / "reference" / "graphs.md").write_text("new\n", encoding="utf-8")
    missing = installed / "aq-tasks" / "reference" / "graphs.md"

    result = await _check_installed_skill_drift(None)
    assert result.severity is Severity.WARN
    assert result.data == {"drifted": [], "missing": [str(missing)]}

    repaired = await _fix_installed_skill_drift(None)
    assert repaired.severity is Severity.OK
    assert missing.read_text(encoding="utf-8") == "new\n"
    assert not missing.with_name("graphs.md.bak").exists()


def _skills_with_references() -> list[str]:
    return [
        name
        for name, skill_md in skill_checks_module._shipped_skills().items()
        if len(skill_checks_module.shipped_skill_files(os.path.dirname(skill_md))) > 1
    ]


@pytest.mark.parametrize("skill_name", _skills_with_references())
def test_seeding_copies_reference_files_write_if_absent_per_file(
    tmp_path, monkeypatch, skill_name
):
    """Every shipped file lands; a later-added file arrives beside an edited SKILL.md."""
    expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path, "expanduser", lambda path: str(tmp_path) if path == "~" else expanduser(path)
    )
    source_dir = Path(skill_checks_module._shipped_skills()[skill_name]).parent
    files = skill_checks_module.shipped_skill_files(str(source_dir))
    assert files[0] == "SKILL.md"

    first = ensure_default_aq_skills(str(tmp_path))
    installed_dir = Path(first["claude"]["root"]) / skill_name
    for rel in files:
        assert (installed_dir / rel).read_bytes() == (source_dir / rel).read_bytes(), rel

    skill_md = installed_dir / "SKILL.md"
    skill_md.write_text("operator edit\n", encoding="utf-8")
    reference = installed_dir / files[1]
    reference.unlink()

    second = ensure_default_aq_skills(str(tmp_path))
    assert skill_name in second["claude"]["created"]
    assert skill_md.read_text(encoding="utf-8") == "operator edit\n"
    assert reference.read_bytes() == (source_dir / files[1]).read_bytes()

    third = ensure_default_aq_skills(str(tmp_path))
    assert skill_name in third["claude"]["skipped"]


def test_every_skill_link_resolves_to_a_shipped_file():
    """Markdown links inside a skill point at files the installer ships beside it."""
    link = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
    for name, skill_md in skill_checks_module._shipped_skills().items():
        skill_dir = os.path.dirname(skill_md)
        files = set(skill_checks_module.shipped_skill_files(skill_dir))
        for rel in files:
            text = Path(skill_dir, rel).read_text(encoding="utf-8")
            for target in link.findall(text):
                if "://" in target:
                    continue
                # An installed skill lives outside any checkout, so a repo-rooted
                # link resolves nowhere; name repository docs as plain text.
                assert not target.startswith("/"), (name, rel, target)
                resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
                assert resolved.replace(os.sep, "/") in files, (name, rel, target)
