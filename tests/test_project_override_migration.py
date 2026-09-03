"""Retirement of project-scoped agent profiles.

``src/profiles/project_override_migration.py`` is the one-shot upgrade path:
it promotes every ``vault/projects/<pid>/agent-types/<type>/profile.md``
override (and the legacy colon-encoded
``vault/agent-types/project:<pid>:<type>/`` layout) into its system profile,
deletes it, and drops the ``project:`` rows from ``agent_profiles``.  The
same code backs the ``profiles.project_overrides`` doctor check.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.database import Database
from src.doctor.models import DoctorContext, Severity
from src.doctor.profile_checks import OVERRIDES_CHECK_ID, profile_checks
from src.profiles.parser import parse_profile
from src.profiles.project_override_migration import (
    find_project_override_paths,
    promote_project_profile_overrides,
    retire_project_scoped_profiles,
)

SYSTEM_MD = """---
id: worker
name: Worker
---

## Role

Do the work.

## Config

```json
{
  "lifecycle": "task",
  "harness": "claude"
}
```

## Rules

Be careful.
"""

OVERRIDE_MD = """---
id: project:proj-a:worker
name: Worker (proj-a)
---

## Config

```json
{
  "lifecycle": "pool",
  "min_active": 2,
  "max_active": 6
}
```
"""


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(data_dir=str(tmp_path))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _system(data_dir: Path, agent_type: str = "worker") -> Path:
    return data_dir / "vault" / "agent-types" / agent_type / "profile.md"


def _override(data_dir: Path, project_id: str = "proj-a", agent_type: str = "worker") -> Path:
    return data_dir / "vault" / "projects" / project_id / "agent-types" / agent_type / "profile.md"


def _flat_override(data_dir: Path, project_id: str = "proj-a", agent_type: str = "worker") -> Path:
    return data_dir / "vault" / "agent-types" / f"project:{project_id}:{agent_type}" / "profile.md"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_finds_both_retired_layouts(tmp_path):
    _write(_override(tmp_path), OVERRIDE_MD)
    _write(_flat_override(tmp_path, "proj-b", "reviewer"), OVERRIDE_MD)
    _write(_system(tmp_path), SYSTEM_MD)

    found = {(pid, agent_type) for pid, agent_type, _ in find_project_override_paths(tmp_path)}

    assert found == {("proj-a", "worker"), ("proj-b", "reviewer")}


def test_finds_nothing_in_a_clean_vault(tmp_path):
    _write(_system(tmp_path), SYSTEM_MD)
    assert find_project_override_paths(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_override_config_is_merged_into_the_system_profile(tmp_path):
    """Last writer wins on ``## Config``; the system profile's prose survives."""
    system = _write(_system(tmp_path), SYSTEM_MD)
    override = _write(_override(tmp_path), OVERRIDE_MD)

    report = promote_project_profile_overrides(str(tmp_path))

    assert report["success"] and report["promoted"] == 1
    parsed = parse_profile(system.read_text(encoding="utf-8"))
    assert parsed.config["lifecycle"] == "pool"
    assert parsed.config["min_active"] == 2
    assert parsed.config["max_active"] == 6
    # Untouched system keys and prose stay put — this is a surgical merge.
    assert parsed.config["harness"] == "claude"
    assert "Do the work." in system.read_text(encoding="utf-8")
    assert "Be careful." in system.read_text(encoding="utf-8")
    # And the override is gone, directories included.
    assert not override.exists()
    assert not override.parent.exists()


def test_promotion_logs_the_diff(tmp_path):
    _write(_system(tmp_path), SYSTEM_MD)
    _write(_override(tmp_path), OVERRIDE_MD)

    report = promote_project_profile_overrides(str(tmp_path))

    changes = report["promotions"][0]["config_changes"]
    assert changes["lifecycle"] == ["task", "pool"]
    assert changes["min_active"] == [None, 2]
    assert "lifecycle" in report["details"][0]


def test_override_becomes_the_system_profile_when_none_exists(tmp_path):
    """No system profile to merge into — the override is moved into place."""
    override = _write(_override(tmp_path), OVERRIDE_MD)

    report = promote_project_profile_overrides(str(tmp_path))

    assert report["promotions"][0]["action"] == "moved"
    system = _system(tmp_path)
    parsed = parse_profile(system.read_text(encoding="utf-8"))
    # The frontmatter id is rewritten, or the file would upsert the dead row.
    assert parsed.frontmatter.id == "worker"
    assert parsed.config["lifecycle"] == "pool"
    assert not override.exists()


def test_prose_the_migration_will_not_merge_is_reported(tmp_path):
    """A hand-written Role in an override is surfaced, never concatenated."""
    _write(_system(tmp_path), SYSTEM_MD)
    _write(
        _override(tmp_path),
        OVERRIDE_MD + "\n## Role\n\nSomething the operator wrote.\n",
    )

    report = promote_project_profile_overrides(str(tmp_path))

    assert report["promotions"][0]["prose_conflicts"] == ["role"]
    assert "unmerged sections: role" in report["details"][0]
    assert "Do the work." in _system(tmp_path).read_text(encoding="utf-8")


def test_legacy_flat_layout_is_promoted_too(tmp_path):
    _write(_system(tmp_path), SYSTEM_MD)
    flat = _write(_flat_override(tmp_path), OVERRIDE_MD)

    report = promote_project_profile_overrides(str(tmp_path))

    assert report["promoted"] == 1
    assert not flat.exists() and not flat.parent.exists()
    assert parse_profile(_system(tmp_path).read_text(encoding="utf-8")).config["max_active"] == 6


def test_migration_is_idempotent(tmp_path):
    _write(_system(tmp_path), SYSTEM_MD)
    _write(_override(tmp_path), OVERRIDE_MD)

    promote_project_profile_overrides(str(tmp_path))
    before = _system(tmp_path).read_text(encoding="utf-8")
    again = promote_project_profile_overrides(str(tmp_path))

    assert again == {
        "success": True,
        "promoted": 0,
        "failed": 0,
        "details": [],
        "promotions": [],
    }
    assert _system(tmp_path).read_text(encoding="utf-8") == before


def test_unwritable_system_profile_keeps_the_override_and_fails(tmp_path):
    """A half-done promotion must report failure rather than lose the override."""
    system = _write(_system(tmp_path), SYSTEM_MD)
    override = _write(_override(tmp_path), OVERRIDE_MD)
    system.chmod(0o444)
    try:
        report = promote_project_profile_overrides(str(tmp_path))
    finally:
        system.chmod(0o644)

    assert not report["success"] and report["failed"] == 1
    assert override.exists(), "the override must survive a failed promotion"


# ---------------------------------------------------------------------------
# Database rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retirement_drops_legacy_rows(tmp_path, db):
    from src.models import AgentProfile

    _write(_system(tmp_path), SYSTEM_MD)
    _write(_override(tmp_path), OVERRIDE_MD)
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_profile(AgentProfile(id="project:proj-a:worker", name="Worker (proj-a)"))

    report = await retire_project_scoped_profiles(str(tmp_path), db)

    assert report["deleted_rows"] == ["project:proj-a:worker"]
    assert await db.get_profile("project:proj-a:worker") is None
    assert await db.get_profile("worker") is not None


# ---------------------------------------------------------------------------
# Doctor check
# ---------------------------------------------------------------------------


def _check(check_id: str):
    return next(c for c in profile_checks() if c.id == check_id)


@pytest.mark.asyncio
async def test_doctor_reports_and_fixes_leftover_overrides(tmp_path, db, config):
    from src.models import AgentProfile

    config.data_dir = str(tmp_path)
    _write(_system(tmp_path), SYSTEM_MD)
    _write(_override(tmp_path), OVERRIDE_MD)
    await db.create_profile(AgentProfile(id="project:proj-a:worker", name="Worker (proj-a)"))
    ctx = DoctorContext(config=config, db=db)
    check = _check(OVERRIDES_CHECK_ID)

    reported = await check.run(ctx)
    assert reported.severity is Severity.WARN
    assert reported.fixable
    assert "proj-a/worker" in reported.detail
    assert reported.data["profile_rows"] == ["project:proj-a:worker"]

    fixed = await check.fix(ctx)
    assert fixed.severity is Severity.OK and fixed.fix_applied
    assert fixed.data["deleted_rows"] == ["project:proj-a:worker"]

    after = await check.run(ctx)
    assert after.severity is Severity.OK
    assert json.loads(json.dumps(after.data)) == {}


@pytest.mark.asyncio
async def test_doctor_is_ok_on_a_clean_vault(tmp_path, db, config):
    config.data_dir = str(tmp_path)
    _write(_system(tmp_path), SYSTEM_MD)

    result = await _check(OVERRIDES_CHECK_ID).run(DoctorContext(config=config, db=db))

    assert result.severity is Severity.OK
    assert "no project-scoped profile overrides remain" in result.detail
