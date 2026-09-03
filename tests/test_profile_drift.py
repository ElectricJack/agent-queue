"""System-profile drift detection — ``src/profiles/drift.py`` + doctor check.

``vault.ensure_default_profiles()`` is write-if-absent, so a vault copy of a
*system* profile seeded by an older release keeps that release's schema and
semantics forever.  The regression that motivated this: a vault ``reviewer``
with ``read_only: false`` re-arms ``GitOpsMixin._task_produces_no_code()``'s
require-a-PR close gate for a session that is told never to push.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import src.doctor  # side effect: populates sys.modules with the submodules
from src.doctor.models import DoctorContext, Severity
from src.profiles.drift import (
    SEMANTIC_CONFIG_FIELDS,
    STATUS_DRIFTED,
    STATUS_NOT_SEEDED,
    STATUS_OK,
    STATUS_RETIRED,
    STATUS_UNREADABLE,
    diff_profile,
    reseed_profile,
    scan_profile_drift,
    system_profile_ids,
)

# ``src/doctor/__init__.py`` rebinds the package attribute ``profile_checks``
# to the *factory function*, shadowing the submodule; sys.modules is the only
# unambiguous route to the module itself.  See tests/test_pool_doctor.py.
profile_checks_mod = sys.modules["src.doctor.profile_checks"]


SHIPPED = """---
id: reviewer
name: Reviewer
---

## Config

```json
{
  "needs_workspace": true,
  "read_only": true,
  "harness": "claude",
  "lifecycle": "task",
  "description": "shipped wording"
}
```

## Capabilities

```json
{"harness_tools": [], "aq_commands": [], "plugin_tools": []}
```

## Role

Review things.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def defaults_root(tmp_path) -> str:
    root = tmp_path / "defaults"
    _write(root / "reviewer" / "profile.md", SHIPPED)
    # A stray directory without a profile.md must not be treated as a profile.
    (root / "not-a-profile").mkdir(parents=True)
    return str(root)


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _vault(data_dir: str, profile_id: str = "reviewer") -> Path:
    return Path(data_dir) / "vault" / "agent-types" / profile_id / "profile.md"


# --- discovery -------------------------------------------------------------


def test_system_profile_ids_requires_a_profile_md(defaults_root):
    assert system_profile_ids(defaults_root) == ["reviewer"]


def test_system_profile_ids_covers_the_real_defaults_tree():
    ids = system_profile_ids()
    assert "reviewer" in ids and "final-reviewer" in ids
    assert ids == sorted(ids)


# --- comparison ------------------------------------------------------------


def test_identical_copy_is_ok(defaults_root, data_dir):
    _write(_vault(data_dir), SHIPPED)
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_OK
    assert not drift.is_drifted
    assert drift.config == []


def test_missing_vault_copy_is_not_seeded_not_drift(defaults_root, data_dir):
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_NOT_SEEDED
    assert not drift.is_drifted
    assert "seeded on next daemon start" in drift.summary()


def test_a_tombstoned_id_reads_as_retired_not_not_seeded(defaults_root, data_dir):
    """The two look identical on disk and need opposite advice: ``not_seeded``
    is about to be fixed by startup, ``retired`` never will be."""
    from src.profiles.retired_defaults import retire_default

    retire_default(data_dir, "reviewer", "not used here")

    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_RETIRED
    # Retirement is a decision, not divergence — the doctor check stays quiet.
    assert not drift.is_drifted
    assert "profile-reseed reviewer" in drift.summary()
    assert drift.to_dict()["status"] == STATUS_RETIRED


def test_a_tombstone_does_not_mask_a_vault_copy_that_exists(defaults_root, data_dir):
    from src.profiles.retired_defaults import retire_default

    _write(_vault(data_dir), SHIPPED)
    retire_default(data_dir, "reviewer")
    assert diff_profile("reviewer", data_dir, defaults_root).status == STATUS_OK


def test_stale_read_only_is_reported(defaults_root, data_dir):
    _write(_vault(data_dir), SHIPPED.replace('"read_only": true', '"read_only": false'))
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_DRIFTED
    assert [(d.field, d.shipped, d.vault) for d in drift.config] == [
        ("read_only", True, False)
    ]
    assert "read_only" in drift.summary()


def test_absent_semantic_field_reads_as_none(defaults_root, data_dir):
    stale = SHIPPED.replace('  "read_only": true,\n', "")
    _write(_vault(data_dir), stale)
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert [(d.field, d.shipped, d.vault) for d in drift.config] == [
        ("read_only", True, None)
    ]


@pytest.mark.parametrize(
    ("field", "shipped_frag", "vault_frag"),
    [
        ("harness", '"harness": "claude"', '"harness": "codex"'),
        ("lifecycle", '"lifecycle": "task"', '"lifecycle": "pool"'),
        ("needs_workspace", '"needs_workspace": true', '"needs_workspace": false'),
    ],
)
def test_every_semantic_field_is_compared(
    defaults_root, data_dir, field, shipped_frag, vault_frag
):
    _write(_vault(data_dir), SHIPPED.replace(shipped_frag, vault_frag))
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert [d.field for d in drift.config] == [field]


def test_cosmetic_config_change_is_not_drift(defaults_root, data_dir):
    _write(_vault(data_dir), SHIPPED.replace("shipped wording", "operator wording"))
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_OK


def test_semantic_field_set_is_the_documented_one():
    assert SEMANTIC_CONFIG_FIELDS == (
        "read_only",
        "harness",
        "lifecycle",
        "needs_workspace",
    )


def test_renamed_section_shows_as_missing_plus_extra(defaults_root, data_dir):
    # The real-world case: a vault copy predating the ``## Tools`` ->
    # ``## Capabilities`` rename.
    _write(_vault(data_dir), SHIPPED.replace("## Capabilities", "## Tools"))
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_DRIFTED
    assert drift.missing_sections == ["capabilities"]
    assert drift.extra_sections == ["tools"]


def test_operator_added_section_alone_is_not_drift(defaults_root, data_dir):
    _write(_vault(data_dir), SHIPPED + "\n## Notes\n\nlocal notes\n")
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_OK
    assert drift.extra_sections == ["notes"]


def test_unparseable_vault_copy_is_unreadable(defaults_root, data_dir):
    _write(_vault(data_dir), SHIPPED.replace('"read_only": true,', '"read_only": ,'))
    drift = diff_profile("reviewer", data_dir, defaults_root)
    assert drift.status == STATUS_UNREADABLE
    assert drift.is_drifted
    assert any(e.startswith("vault:") for e in drift.errors)


def test_unknown_profile_id_is_unreadable(defaults_root, data_dir):
    drift = diff_profile("nope", data_dir, defaults_root)
    assert drift.status == STATUS_UNREADABLE


def test_scan_covers_every_shipped_profile(defaults_root, data_dir):
    drifts = scan_profile_drift(data_dir, defaults_root)
    assert [d.profile_id for d in drifts] == ["reviewer"]


def test_to_dict_is_json_serialisable(defaults_root, data_dir):
    _write(_vault(data_dir), SHIPPED.replace('"read_only": true', '"read_only": false'))
    payload = diff_profile("reviewer", data_dir, defaults_root).to_dict()
    assert json.loads(json.dumps(payload))["status"] == STATUS_DRIFTED


# --- reseed ----------------------------------------------------------------


def test_reseed_replaces_the_vault_copy_and_backs_it_up(defaults_root, data_dir):
    stale = SHIPPED.replace('"read_only": true', '"read_only": false')
    _write(_vault(data_dir), stale)

    result = reseed_profile(data_dir, "reviewer", defaults_root)

    assert result["created"] is False
    assert _vault(data_dir).read_text(encoding="utf-8") == SHIPPED
    assert Path(result["backup_path"]).read_text(encoding="utf-8") == stale
    assert diff_profile("reviewer", data_dir, defaults_root).status == STATUS_OK


def test_reseed_can_skip_the_backup(defaults_root, data_dir):
    _write(_vault(data_dir), "stale")
    result = reseed_profile(data_dir, "reviewer", defaults_root, backup=False)
    assert result["backup_path"] is None
    assert not [p for p in os.listdir(_vault(data_dir).parent) if ".bak-" in p]


def test_reseed_creates_a_missing_copy(defaults_root, data_dir):
    result = reseed_profile(data_dir, "reviewer", defaults_root)
    assert result["created"] is True
    assert result["backup_path"] is None
    assert _vault(data_dir).exists()


def test_reseed_refuses_a_non_system_profile(defaults_root, data_dir):
    with pytest.raises(FileNotFoundError):
        reseed_profile(data_dir, "my-custom-profile", defaults_root)


# --- doctor check ----------------------------------------------------------


class _Config:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir


def test_check_is_registered_in_the_default_registry():
    assert "profiles.system_drift" in src.doctor.default_registry().ids()


def test_check_has_no_fix():
    # Auto-overwriting would silently discard operator edits; the repair is
    # the explicit ``profile_reseed`` command.
    checks = {c.id: c for c in profile_checks_mod.profile_checks()}
    drift = checks["profiles.system_drift"]
    assert drift.fix is None
    assert drift.owner == "profiles"
    # The retired-override migration, by contrast, is safe to apply.
    assert checks["profiles.project_overrides"].fix is not None


async def test_check_ok_when_everything_matches(defaults_root, data_dir, monkeypatch):
    monkeypatch.setattr(profile_checks_mod, "scan_profile_drift", lambda d: [])
    result = await profile_checks_mod._check_system_profile_drift(
        DoctorContext(config=_Config(data_dir))
    )
    assert result.severity == Severity.INFO


async def test_check_warns_on_drift(defaults_root, data_dir, monkeypatch):
    _write(_vault(data_dir), SHIPPED.replace('"read_only": true', '"read_only": false'))
    monkeypatch.setattr(
        profile_checks_mod,
        "scan_profile_drift",
        lambda d: scan_profile_drift(d, defaults_root),
    )
    result = await profile_checks_mod._check_system_profile_drift(
        DoctorContext(config=_Config(data_dir))
    )
    assert result.severity == Severity.WARN
    assert result.data["drifted"] == 1
    assert result.data["profiles"][0]["config"][0]["field"] == "read_only"
    assert "profile-reseed" in result.detail
    assert result.fixable is False


async def test_check_detail_truncates_a_fleet_wide_drift(data_dir, monkeypatch):
    # One upgrade can drift every shipped profile at once; the one-line detail
    # names a few and defers the rest to ``data``.
    from src.profiles.drift import ProfileDrift

    many = [ProfileDrift(profile_id=f"p{i}", status=STATUS_DRIFTED) for i in range(10)]
    monkeypatch.setattr(profile_checks_mod, "scan_profile_drift", lambda d: many)
    result = await profile_checks_mod._check_system_profile_drift(
        DoctorContext(config=_Config(data_dir))
    )
    assert "+7 more" in result.detail
    assert len(result.data["profiles"]) == 10


async def test_check_errors_on_an_unparseable_profile(
    defaults_root, data_dir, monkeypatch
):
    _write(_vault(data_dir), SHIPPED.replace('"read_only": true,', '"read_only": ,'))
    monkeypatch.setattr(
        profile_checks_mod,
        "scan_profile_drift",
        lambda d: scan_profile_drift(d, defaults_root),
    )
    result = await profile_checks_mod._check_system_profile_drift(
        DoctorContext(config=_Config(data_dir))
    )
    assert result.severity == Severity.ERROR


async def test_check_is_ok_when_nothing_is_seeded_yet(
    defaults_root, data_dir, monkeypatch
):
    monkeypatch.setattr(
        profile_checks_mod,
        "scan_profile_drift",
        lambda d: scan_profile_drift(d, defaults_root),
    )
    result = await profile_checks_mod._check_system_profile_drift(
        DoctorContext(config=_Config(data_dir))
    )
    assert result.severity == Severity.OK


# --- command surface -------------------------------------------------------


@pytest.fixture
async def handler(tmp_path):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    from src.orchestrator import Orchestrator

    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    await orch.initialize()
    yield CommandHandler(orch, config)
    await orch.db.close()


async def test_profile_drift_command_reports_the_real_defaults(handler):
    result = await handler.execute("profile_drift", {})
    assert result["success"] is True
    assert result["checked"] == len(system_profile_ids())
    # Orchestrator startup has just run ensure_default_profiles(), so every
    # vault copy is byte-identical to what ships.  A freshly seeded vault
    # having zero drift is the baseline the check exists to defend.
    assert result["drifted_count"] == 0
    assert {r["status"] for r in result["profiles"]} == {STATUS_OK}


async def test_profile_drift_command_flags_a_stale_copy(handler):
    data_dir = handler.config.data_dir
    shipped = Path(
        os.path.join(
            os.path.dirname(sys.modules["src.profiles.drift"].__file__),
            "defaults",
            "reviewer",
            "profile.md",
        )
    ).read_text(encoding="utf-8")
    _write(_vault(data_dir), shipped.replace('"read_only": true', '"read_only": false'))

    result = await handler.execute("profile_drift", {"drifted_only": True})
    assert result["drifted_count"] == 1
    (row,) = result["profiles"]
    assert row["profile_id"] == "reviewer"
    assert row["config"][0]["field"] == "read_only"

    # ... and reseeding puts it back.
    reseeded = await handler.execute("profile_reseed", {"profile_id": "reviewer"})
    assert reseeded["success"] is True
    assert reseeded["created"] is False
    assert Path(reseeded["backup_path"]).exists()
    assert (await handler.execute("profile_drift", {}))["drifted_count"] == 0


async def test_profile_drift_command_rejects_an_unknown_id(handler):
    result = await handler.execute("profile_drift", {"profile_id": "nope"})
    assert "error" in result


async def test_profile_reseed_requires_a_system_profile(handler):
    assert "error" in await handler.execute("profile_reseed", {})
    result = await handler.execute("profile_reseed", {"profile_id": "my-own"})
    assert "not a shipped system profile" in result["error"]
