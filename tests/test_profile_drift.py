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
    merge_profile_grants,
    reseed_profile,
    scan_profile_drift,
    system_profile_ids,
)
from tests.db_fixtures import lease_dsn
from src.config import DatabaseConfig

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


# A second shipped fixture with real (non-empty) capability lists, used by
# the missing_grants / merge tests below.  Kept separate from SHIPPED so the
# section-rename and config-only tests above stay unaffected.
SHIPPED_WITH_GRANTS = """---
id: reviewer
name: Reviewer
---

## Config

```json
{
  "needs_workspace": true,
  "read_only": true,
  "harness": "claude",
  "lifecycle": "task"
}
```

## Capabilities

```json
{
  "harness_tools": [
    "Bash",
    "Read"
  ],
  "aq_commands": [
    "escalation_apply_reply",
    "task_close",
    "task_show"
  ],
  "plugin_tools": []
}
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


# --- missing grants ---------------------------------------------------------


@pytest.fixture
def grants_root(tmp_path) -> str:
    root = tmp_path / "grants-defaults"
    _write(root / "reviewer" / "profile.md", SHIPPED_WITH_GRANTS)
    return str(root)


def test_missing_aq_command_grant_is_detected_and_named(grants_root, data_dir):
    vault = SHIPPED_WITH_GRANTS.replace('    "escalation_apply_reply",\n', "")
    _write(_vault(data_dir), vault)

    drift = diff_profile("reviewer", data_dir, grants_root)

    assert drift.status == STATUS_DRIFTED
    assert drift.is_drifted
    assert drift.missing_grants == {"aq_commands": ["escalation_apply_reply"]}
    assert "escalation_apply_reply" in drift.summary()
    assert "missing 1 aq_commands grant(s)" in drift.summary()
    assert drift.to_dict()["missing_grants"] == {"aq_commands": ["escalation_apply_reply"]}


def test_extra_vault_grant_is_not_drift(grants_root, data_dir):
    vault = SHIPPED_WITH_GRANTS.replace(
        '    "task_show"\n', '    "task_show",\n    "operator_added_command"\n'
    )
    _write(_vault(data_dir), vault)

    drift = diff_profile("reviewer", data_dir, grants_root)

    assert drift.status == STATUS_OK
    assert drift.missing_grants == {}


def test_harness_only_difference_still_reports_as_today(grants_root, data_dir):
    # Regression: a Config-only divergence must not pick up a spurious
    # missing_grants entry now that grants are compared too.
    vault = SHIPPED_WITH_GRANTS.replace('"harness": "claude"', '"harness": "codex"')
    _write(_vault(data_dir), vault)

    drift = diff_profile("reviewer", data_dir, grants_root)

    assert drift.status == STATUS_DRIFTED
    assert [(d.field, d.shipped, d.vault) for d in drift.config] == [
        ("harness", "claude", "codex")
    ]
    assert drift.missing_grants == {}


def test_legacy_tools_vault_reports_missing_sections_and_no_missing_grants(
    grants_root, data_dir
):
    vault = SHIPPED_WITH_GRANTS.replace("## Capabilities", "## Tools").replace(
        '"aq_commands"', '"allowed"'
    )
    _write(_vault(data_dir), vault)

    drift = diff_profile("reviewer", data_dir, grants_root)

    assert drift.status == STATUS_DRIFTED
    assert drift.missing_sections == ["capabilities"]
    assert drift.missing_grants == {}


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


# --- merge (additive grant repair) ------------------------------------------

# Vault copy with an operator edit (`harness: codex`), an operator-added
# extra grant, an operator-added extra section, and a missing shipped grant
# (`escalation_apply_reply`) — the realistic case merge exists for.
VAULT_STALE_GRANTS = """---
id: reviewer
name: Reviewer
---

## Config

```json
{
  "needs_workspace": true,
  "read_only": true,
  "harness": "codex",
  "lifecycle": "task"
}
```

## Capabilities

```json
{
  "harness_tools": [
    "Bash",
    "Read"
  ],
  "aq_commands": [
    "task_close",
    "task_show",
    "operator_added_command"
  ],
  "plugin_tools": []
}
```

## Role

Review things.

## Notes

operator notes worth keeping
"""


def test_merge_appends_only_missing_grants_and_preserves_operator_edits(
    grants_root, data_dir
):
    _write(_vault(data_dir), VAULT_STALE_GRANTS)

    result = merge_profile_grants(data_dir, "reviewer", root=grants_root)

    assert result["profile_id"] == "reviewer"
    assert result["added"] == {"aq_commands": ["escalation_apply_reply"]}
    assert result["changed"] is True
    assert result["backup_path"] is not None

    merged_text = _vault(data_dir).read_text(encoding="utf-8")
    from src.profiles.parser import parse_profile

    parsed = parse_profile(merged_text)
    assert parsed.errors == []
    # Operator edit preserved.
    assert parsed.config["harness"] == "codex"
    # Operator-added grant and section preserved.
    assert "operator_added_command" in parsed.capabilities["aq_commands"]
    assert "notes" in parsed.sections
    assert "operator notes worth keeping" in merged_text
    # Missing grant now present, existing order kept, nothing duplicated.
    assert parsed.capabilities["aq_commands"] == [
        "task_close",
        "task_show",
        "operator_added_command",
        "escalation_apply_reply",
    ]

    # Grants are now in sync; the operator's harness edit is a real Config
    # divergence, independent of the merge, and still reports drifted.
    after = diff_profile("reviewer", data_dir, grants_root)
    assert after.missing_grants == {}
    assert after.status == STATUS_DRIFTED
    assert [d.field for d in after.config] == ["harness"]


def test_merge_writes_a_backup_matching_the_original(grants_root, data_dir):
    _write(_vault(data_dir), VAULT_STALE_GRANTS)
    result = merge_profile_grants(data_dir, "reviewer", root=grants_root)
    assert Path(result["backup_path"]).read_text(encoding="utf-8") == VAULT_STALE_GRANTS


def test_merge_is_a_noop_when_nothing_is_missing(grants_root, data_dir):
    _write(_vault(data_dir), SHIPPED_WITH_GRANTS)
    result = merge_profile_grants(data_dir, "reviewer", root=grants_root)
    assert result == {
        "profile_id": "reviewer",
        "added": {},
        "backup_path": None,
        "changed": False,
    }
    assert not [p for p in os.listdir(_vault(data_dir).parent) if ".bak-" in p]
    assert _vault(data_dir).read_text(encoding="utf-8") == SHIPPED_WITH_GRANTS


def test_merge_refuses_when_vault_has_no_capabilities_section(grants_root, data_dir):
    legacy = SHIPPED_WITH_GRANTS.replace("## Capabilities", "## Tools").replace(
        '"aq_commands"', '"allowed"'
    )
    _write(_vault(data_dir), legacy)

    with pytest.raises(ValueError, match="no '## Capabilities' section"):
        merge_profile_grants(data_dir, "reviewer", root=grants_root)

    assert _vault(data_dir).read_text(encoding="utf-8") == legacy
    assert not [p for p in os.listdir(_vault(data_dir).parent) if ".bak-" in p]


def test_merge_refuses_a_non_system_profile(grants_root, data_dir):
    with pytest.raises(FileNotFoundError):
        merge_profile_grants(data_dir, "my-custom-profile", root=grants_root)


def test_merge_refuses_when_vault_has_no_copy(grants_root, data_dir):
    with pytest.raises(FileNotFoundError):
        merge_profile_grants(data_dir, "reviewer", root=grants_root)


def test_merge_refuses_and_leaves_no_backup_when_merged_text_would_not_parse(
    grants_root, data_dir, monkeypatch
):
    import src.profiles.drift as drift_mod

    _write(_vault(data_dir), VAULT_STALE_GRANTS)

    def _corrupt(block_text: str, ns: str, new_names: list[str]) -> str:
        # Produce syntactically invalid JSON so the pre-write validation
        # guard has something real to catch.
        return block_text + ",,,not json,,,"

    monkeypatch.setattr(drift_mod, "_rewrite_capabilities_array", _corrupt)

    with pytest.raises(ValueError, match="would not parse cleanly"):
        merge_profile_grants(data_dir, "reviewer", root=grants_root)

    assert _vault(data_dir).read_text(encoding="utf-8") == VAULT_STALE_GRANTS
    assert not [p for p in os.listdir(_vault(data_dir).parent) if ".bak-" in p]


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
    # A Config-only divergence has nothing for --grants-only to fix.
    assert "--grants-only" not in result.detail


async def test_check_detail_mentions_grants_only_when_grants_are_missing(
    grants_root, data_dir, monkeypatch
):
    vault = SHIPPED_WITH_GRANTS.replace('    "escalation_apply_reply",\n', "")
    _write(_vault(data_dir), vault)
    monkeypatch.setattr(
        profile_checks_mod,
        "scan_profile_drift",
        lambda d: scan_profile_drift(d, grants_root),
    )
    result = await profile_checks_mod._check_system_profile_drift(
        DoctorContext(config=_Config(data_dir))
    )
    assert result.severity == Severity.WARN
    assert "--grants-only" in result.detail
    assert "keeps your edits" in result.detail


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
        database=DatabaseConfig(url=lease_dsn("test.db")),
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


async def test_profile_reseed_grants_only_merges_instead_of_overwriting(handler):
    data_dir = handler.config.data_dir
    vault_path = _vault(data_dir, "reviewer")
    original = vault_path.read_text(encoding="utf-8")
    # Simulate a shipped upgrade that added a grant the vault copy lacks,
    # plus an operator edit that a full reseed would destroy.
    stale = original.replace('"harness": "claude"', '"harness": "codex"')
    (stale, removed) = _drop_one_aq_command_grant(stale)
    _write(vault_path, stale)

    drifted = await handler.execute("profile_drift", {"profile_id": "reviewer"})
    (row,) = drifted["profiles"]
    assert removed in row["missing_grants"]["aq_commands"]

    result = await handler.execute(
        "profile_reseed", {"profile_id": "reviewer", "grants_only": True}
    )

    assert result["success"] is True
    assert result["mode"] == "grants_only"
    assert result["added"] == {"aq_commands": [removed]}
    assert result["changed"] is True
    assert Path(result["backup_path"]).read_text(encoding="utf-8") == stale

    merged = vault_path.read_text(encoding="utf-8")
    assert '"harness": "codex"' in merged  # operator edit survived

    after = await handler.execute("profile_drift", {"profile_id": "reviewer"})
    assert after["profiles"][0]["missing_grants"] == {}
    # The operator's harness edit is a real Config divergence and is
    # unaffected by a grants-only merge — it still reports drifted.
    assert after["profiles"][0]["status"] == STATUS_DRIFTED


def _drop_one_aq_command_grant(text: str) -> tuple[str, str]:
    """Remove one ``aq_commands`` entry from ``text``'s Capabilities block.

    Round-trips through JSON (rather than regex line-splicing) so the
    result is guaranteed to still parse; the test only needs a valid vault
    file missing exactly one grant, not any particular formatting.
    """
    import json as _json

    import src.profiles.drift as drift_mod

    start, end = drift_mod._capabilities_json_span(text)
    data = _json.loads(text[start:end])
    removed = data["aq_commands"].pop()
    new_block = _json.dumps(data, indent=2)
    return text[:start] + new_block + text[end:], removed
