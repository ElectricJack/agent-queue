"""Additive sync of shipped capabilities into the vault supervisor profile.

Seeding is write-if-absent, so every control a release granted the shipped
supervisor stayed ``capability denied`` on an existing install until someone
hand-edited ``vault/agent-types/supervisor/profile.md`` (four times on
2026-09-24).  ``src/profiles/capability_sync.py`` merges the missing grants on
daemon start and on every vault reload; ``profiles.supervisor_capability_drift``
reports the gap and ``--fix`` runs the same merge.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import pytest

import src.doctor  # side effect: populates sys.modules with the submodules
from src.config import AppConfig, DatabaseConfig
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import DoctorRegistry, run_doctor
from src.event_schemas import validate_event
from src.profiles.capability_sync import (
    CAPABILITIES_SYNCED_EVENT,
    DEFAULT_SYNCED_PROFILE_IDS,
    STATUS_CURRENT,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_NOT_SEEDED,
    STATUS_SYNCED,
    capability_sync_enabled,
    capability_sync_setting,
    publish_sync_result,
    sync_profile_capabilities,
    sync_shipped_capabilities,
)
from src.profiles.drift import diff_profile, shipped_profile_path
from src.profiles.parser import parse_profile
from src.vault_watcher import VaultChange
from tests.db_fixtures import lease_dsn

# ``src/doctor/__init__.py`` rebinds ``profile_checks`` to the factory function;
# sys.modules is the unambiguous route to the module (see test_profile_drift).
profile_checks_mod = sys.modules["src.doctor.profile_checks"]

CHECK_ID = "profiles.supervisor_capability_drift"


def _profile(profile_id: str, aq_commands: list[str], *, frontmatter: str = "") -> str:
    commands = ",\n".join(f'    "{name}"' for name in aq_commands)
    return f"""---
id: {profile_id}
name: {profile_id.title()}
{frontmatter}---

# {profile_id.title()}

## Role
Operator prose that must survive every merge.

## Capabilities

```json
{{
  "harness_tools": [
    "Bash",
    "Read"
  ],
  "aq_commands": [
{commands}
  ],
  "plugin_tools": []
}}
```

## Rules
- More operator prose.
"""


SHIPPED_SUPERVISOR = _profile(
    "supervisor",
    ["integration_eject", "integration_status", "review_dispatch", "task_show"],
)
SHIPPED_REVIEWER = _profile("reviewer", ["task_close", "task_show"])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _vault(data_dir: str, profile_id: str = "supervisor") -> Path:
    return Path(data_dir) / "vault" / "agent-types" / profile_id / "profile.md"


def _aq_commands(path: Path) -> list[str]:
    parsed = parse_profile(path.read_text(encoding="utf-8"))
    assert parsed.errors == []
    return list(parsed.capabilities["aq_commands"])


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob("profile.md.bak-*"))


@pytest.fixture
def root(tmp_path) -> str:
    base = tmp_path / "defaults"
    _write(base / "supervisor" / "profile.md", SHIPPED_SUPERVISOR)
    _write(base / "reviewer" / "profile.md", SHIPPED_REVIEWER)
    return str(base)


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


# A stale vault copy: two shipped controls missing, one grant only the
# operator added, and an operator edit to the prose.
STALE_SUPERVISOR = _profile(
    "supervisor", ["integration_status", "task_show", "operator_custom_command"]
).replace("Operator prose that must survive", "Hand-edited operator prose that must survive")


# --- frontmatter opt-out / opt-in -------------------------------------------


def test_setting_reads_booleans_and_boolean_words():
    assert capability_sync_setting(_profile("x", [], frontmatter="capability_sync: false\n")) is False
    assert capability_sync_setting(_profile("x", [], frontmatter="capability_sync: true\n")) is True
    assert capability_sync_setting(_profile("x", [], frontmatter='capability_sync: "off"\n')) is False
    assert capability_sync_setting(_profile("x", [], frontmatter="capability_sync: maybe\n")) is None
    assert capability_sync_setting(_profile("x", [])) is None


def test_only_the_supervisor_is_synced_by_default():
    assert DEFAULT_SYNCED_PROFILE_IDS == {"supervisor"}
    assert capability_sync_enabled("supervisor", _profile("supervisor", []))
    assert not capability_sync_enabled("reviewer", _profile("reviewer", []))
    assert not capability_sync_enabled(
        "supervisor", _profile("supervisor", [], frontmatter="capability_sync: false\n")
    )
    assert capability_sync_enabled(
        "reviewer", _profile("reviewer", [], frontmatter="capability_sync: true\n")
    )


# --- the merge ---------------------------------------------------------------


def test_sync_adds_missing_grants_and_keeps_unknown_extras(root, data_dir):
    vault = _vault(data_dir)
    _write(vault, STALE_SUPERVISOR)

    result = sync_profile_capabilities(data_dir, "supervisor", root=root)

    assert result.status == STATUS_SYNCED
    assert result.changed is True
    assert result.added == {"aq_commands": ["integration_eject", "review_dispatch"]}
    assert result.added_names() == ["integration_eject", "review_dispatch"]
    commands = _aq_commands(vault)
    # Existing entries keep their order; the operator's extra grant stays;
    # the shipped additions are appended.
    assert commands == [
        "integration_status",
        "task_show",
        "operator_custom_command",
        "integration_eject",
        "review_dispatch",
    ]
    merged = vault.read_text(encoding="utf-8")
    assert "Hand-edited operator prose that must survive every merge." in merged
    assert "- More operator prose." in merged
    # Outside the Capabilities block, not one byte moved.
    head = STALE_SUPERVISOR.split("## Capabilities")[0]
    tail = STALE_SUPERVISOR.split("## Rules")[1]
    assert merged.startswith(head)
    assert merged.endswith(tail)
    # The backup is the pre-merge file.
    assert result.backup_path is not None
    assert Path(result.backup_path).read_text(encoding="utf-8") == STALE_SUPERVISOR
    assert diff_profile("supervisor", data_dir, root).missing_grants == {}


def test_sync_is_idempotent(root, data_dir):
    vault = _vault(data_dir)
    _write(vault, STALE_SUPERVISOR)
    assert sync_profile_capabilities(data_dir, "supervisor", root=root).status == STATUS_SYNCED
    after_first = vault.read_text(encoding="utf-8")
    backups = _backups(vault)

    again = sync_profile_capabilities(data_dir, "supervisor", root=root)

    assert again.status == STATUS_CURRENT
    assert again.added == {}
    assert again.backup_path is None
    assert vault.read_text(encoding="utf-8") == after_first
    assert _backups(vault) == backups


def test_opt_out_leaves_the_vault_copy_alone(root, data_dir):
    vault = _vault(data_dir)
    curated = STALE_SUPERVISOR.replace("name: Supervisor\n", "name: Supervisor\ncapability_sync: false\n")
    _write(vault, curated)

    result = sync_profile_capabilities(data_dir, "supervisor", root=root)

    assert result.status == STATUS_DISABLED
    assert vault.read_text(encoding="utf-8") == curated
    assert _backups(vault) == []


def test_other_profiles_sync_only_when_they_opt_in(root, data_dir):
    stale = _profile("reviewer", ["task_show"])
    _write(_vault(data_dir, "reviewer"), stale)

    assert sync_profile_capabilities(data_dir, "reviewer", root=root).status == STATUS_DISABLED
    assert _vault(data_dir, "reviewer").read_text(encoding="utf-8") == stale

    opted_in = stale.replace("name: Reviewer\n", "name: Reviewer\ncapability_sync: true\n")
    _write(_vault(data_dir, "reviewer"), opted_in)
    result = sync_profile_capabilities(data_dir, "reviewer", root=root)
    assert result.status == STATUS_SYNCED
    assert result.added == {"aq_commands": ["task_close"]}


def test_missing_vault_copy_is_not_seeded(root, data_dir):
    result = sync_profile_capabilities(data_dir, "supervisor", root=root)
    assert result.status == STATUS_NOT_SEEDED
    assert not _vault(data_dir).exists()


def test_a_vault_copy_without_capabilities_fails_without_writing(root, data_dir):
    legacy = "---\nid: supervisor\nname: Supervisor\n---\n\n## Role\nOld.\n"
    _write(_vault(data_dir), legacy)

    result = sync_profile_capabilities(data_dir, "supervisor", root=root)

    assert result.status == STATUS_FAILED
    assert "Capabilities" in result.error
    assert _vault(data_dir).read_text(encoding="utf-8") == legacy
    assert _backups(_vault(data_dir)) == []


def test_a_non_shipped_profile_fails(root, data_dir):
    result = sync_profile_capabilities(data_dir, "my-own", root=root)
    assert result.status == STATUS_FAILED
    assert "not a shipped system profile" in result.error


def test_sync_shipped_capabilities_touches_only_synced_profiles(root, data_dir):
    _write(_vault(data_dir), STALE_SUPERVISOR)
    stale_reviewer = _profile("reviewer", ["task_show"])
    _write(_vault(data_dir, "reviewer"), stale_reviewer)

    results = {r.profile_id: r for r in sync_shipped_capabilities(data_dir, root=root)}

    assert set(results) == {"supervisor", "reviewer"}
    assert results["supervisor"].status == STATUS_SYNCED
    assert results["reviewer"].status == STATUS_DISABLED
    assert _vault(data_dir, "reviewer").read_text(encoding="utf-8") == stale_reviewer


def test_the_real_shipped_supervisor_restores_the_2026_09_24_denials(data_dir):
    """The shipped file itself, minus the controls that were denied that day."""
    shipped = Path(shipped_profile_path("supervisor")).read_text(encoding="utf-8")
    denied = [
        "integration_release_stale_owners",
        "integration_retry_cleanup",
        "integration_eject",
        "review_dispatch",
        "integration_adopt_legacy_deliveries",
    ]
    start = shipped.index("## Capabilities")
    end = shipped.index("## Rules")
    block = shipped[start:end]
    for name in denied:
        assert f'    "{name}",\n' in block, name
        block = block.replace(f'    "{name}",\n', "", 1)
    block = block.replace('"plugin_tools": [\n', '"plugin_tools": [\n    "operator_plugin_tool",\n', 1)
    stale = shipped[:start] + block + shipped[end:]
    _write(_vault(data_dir), stale)

    result = sync_profile_capabilities(data_dir, "supervisor")

    assert result.status == STATUS_SYNCED
    assert sorted(result.added["aq_commands"]) == sorted(denied)
    parsed = parse_profile(_vault(data_dir).read_text(encoding="utf-8"))
    assert parsed.errors == []
    shipped_caps = parse_profile(shipped).capabilities
    for ns, names in shipped_caps.items():
        assert set(names) <= set(parsed.capabilities[ns]), ns
    assert "operator_plugin_tool" in parsed.capabilities["plugin_tools"]
    assert sync_profile_capabilities(data_dir, "supervisor").status == STATUS_CURRENT


def test_the_real_shipped_supervisor_syncs_inbox_grants_with_a_backup(data_dir):
    shipped = Path(shipped_profile_path("supervisor")).read_text(encoding="utf-8")
    inbox_grants = [
        "supervisor_inbox_history",
        "supervisor_inbox_reply",
        "supervisor_inbox_status",
    ]
    stale = shipped
    for name in inbox_grants:
        stale = stale.replace(f'    "{name}",\n', "", 1)
    stale = stale.replace(
        '"aq_commands": [\n',
        '"aq_commands": [\n    "operator_custom_command",\n',
        1,
    ).replace(
        "You are a supervisor in Agent Queue.",
        "Operator-authored supervisor role.",
        1,
    )
    vault = _vault(data_dir)
    _write(vault, stale)

    result = sync_profile_capabilities(data_dir, "supervisor")

    assert result.status == STATUS_SYNCED
    assert result.added == {"aq_commands": inbox_grants}
    assert result.backup_path is not None
    assert _backups(vault) == [Path(result.backup_path)]
    assert Path(result.backup_path).read_text(encoding="utf-8") == stale
    commands = _aq_commands(vault)
    assert set(inbox_grants) <= set(commands)
    assert "operator_custom_command" in commands
    assert "supervisor_inbox_post" not in commands
    merged = vault.read_text(encoding="utf-8")
    assert merged.split("## Capabilities")[0] == stale.split("## Capabilities")[0]
    assert merged.split("## Rules")[1] == stale.split("## Rules")[1]
    assert sync_profile_capabilities(data_dir, "supervisor").status == STATUS_CURRENT
    assert _backups(vault) == [Path(result.backup_path)]


# --- logging and the event --------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.emitted.append((event_type, payload))


async def test_publish_emits_a_schema_valid_event_naming_each_grant(root, data_dir, caplog):
    _write(_vault(data_dir), STALE_SUPERVISOR)
    result = sync_profile_capabilities(data_dir, "supervisor", root=root)
    bus = _Bus()

    with caplog.at_level("INFO", logger="src.profiles.capability_sync"):
        await publish_sync_result(result, event_bus=bus, trigger="startup")

    ((event_type, payload),) = bus.emitted
    assert event_type == CAPABILITIES_SYNCED_EVENT
    assert payload["profile_id"] == "supervisor"
    assert payload["added"] == {"aq_commands": ["integration_eject", "review_dispatch"]}
    assert payload["trigger"] == "startup"
    assert validate_event(event_type, payload, strict_extras=True) == []
    assert "integration_eject, review_dispatch" in caplog.text


async def test_publish_is_silent_when_nothing_changed(root, data_dir):
    _write(_vault(data_dir), SHIPPED_SUPERVISOR)
    bus = _Bus()
    result = sync_profile_capabilities(data_dir, "supervisor", root=root)
    assert result.status == STATUS_CURRENT
    await publish_sync_result(result, event_bus=bus, trigger="reload")
    assert bus.emitted == []


# --- the vault watcher reload ----------------------------------------------


async def test_watcher_reload_merges_before_the_db_sync(data_dir, monkeypatch):
    from src.profiles import sync as sync_mod

    shipped = Path(shipped_profile_path("supervisor")).read_text(encoding="utf-8")
    stale = shipped.replace('    "review_dispatch",\n', "", 1)
    assert stale != shipped
    vault = _vault(data_dir)
    _write(vault, stale)

    seen: list[list[str]] = []

    async def _fake_sync(parsed, db, *, source_path=None, fallback_id=None):
        seen.append(list(parsed.capabilities["aq_commands"]))
        return sync_mod.ProfileSyncResult(success=True, profile_id="supervisor", action="updated")

    monkeypatch.setattr(sync_mod, "sync_profile_to_db", _fake_sync)
    bus = _Bus()
    change = VaultChange(
        path=str(vault), rel_path="agent-types/supervisor/profile.md", operation="modified"
    )

    await sync_mod.on_profile_changed([change], db=object(), event_bus=bus, data_dir=data_dir)

    assert "review_dispatch" in _aq_commands(vault)
    (synced_commands,) = seen
    assert "review_dispatch" in synced_commands
    assert [e for e, _ in bus.emitted] == [CAPABILITIES_SYNCED_EVENT]
    assert bus.emitted[0][1]["trigger"] == "reload"


async def test_watcher_reload_ignores_non_vault_paths(tmp_path, data_dir, monkeypatch):
    from src.profiles import sync as sync_mod

    elsewhere = tmp_path / "agent-types" / "supervisor" / "profile.md"
    stale = Path(shipped_profile_path("supervisor")).read_text(encoding="utf-8").replace(
        '    "review_dispatch",\n', "", 1
    )
    _write(elsewhere, stale)

    async def _fake_sync(parsed, db, *, source_path=None, fallback_id=None):
        return sync_mod.ProfileSyncResult(success=True, profile_id="supervisor", action="updated")

    monkeypatch.setattr(sync_mod, "sync_profile_to_db", _fake_sync)
    change = VaultChange(
        path=str(elsewhere), rel_path="agent-types/supervisor/profile.md", operation="modified"
    )
    await sync_mod.on_profile_changed([change], db=object(), data_dir=data_dir)

    assert elsewhere.read_text(encoding="utf-8") == stale


# --- doctor check --------------------------------------------------------------


class _Config:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir


@pytest.fixture
def check_root(root, monkeypatch):
    """Point the doctor check's reads and fix at the fixture defaults tree."""
    monkeypatch.setattr(
        profile_checks_mod, "diff_profile", functools.partial(diff_profile, root=root)
    )
    monkeypatch.setattr(
        profile_checks_mod,
        "sync_profile_capabilities",
        functools.partial(sync_profile_capabilities, root=root),
    )
    return root


def _ctx(data_dir: str) -> DoctorContext:
    return DoctorContext(config=_Config(data_dir))


def test_check_is_registered_with_a_fix():
    assert CHECK_ID in src.doctor.default_registry().ids()
    checks = {c.id: c for c in profile_checks_mod.profile_checks()}
    assert checks[CHECK_ID].fix is not None
    assert checks[CHECK_ID].owner == "profiles"


async def test_check_warns_and_names_missing_capabilities(check_root, data_dir):
    _write(_vault(data_dir), STALE_SUPERVISOR)
    result = await profile_checks_mod._check_supervisor_capability_drift(_ctx(data_dir))
    assert result.severity == Severity.WARN
    assert "integration_eject" in result.detail
    assert "review_dispatch" in result.detail
    assert "--fix" in result.detail
    assert result.data["missing"] == {"aq_commands": ["integration_eject", "review_dispatch"]}
    assert result.data["capability_sync"] is True
    json.dumps(result.to_dict())


async def test_check_is_ok_when_nothing_is_missing(check_root, data_dir):
    _write(_vault(data_dir), STALE_SUPERVISOR)
    sync_profile_capabilities(data_dir, "supervisor", root=check_root)
    result = await profile_checks_mod._check_supervisor_capability_drift(_ctx(data_dir))
    assert result.severity == Severity.OK


async def test_check_is_info_when_opted_out(check_root, data_dir):
    _write(_vault(data_dir), STALE_SUPERVISOR.replace("---\n\n#", "capability_sync: false\n---\n\n#", 1))
    result = await profile_checks_mod._check_supervisor_capability_drift(_ctx(data_dir))
    assert result.severity == Severity.INFO
    assert "capability_sync: false" in result.detail
    assert "integration_eject" in result.detail


async def test_check_is_info_before_seeding(check_root, data_dir):
    result = await profile_checks_mod._check_supervisor_capability_drift(_ctx(data_dir))
    assert result.severity == Severity.INFO


async def test_check_warns_when_there_is_no_capabilities_block(check_root, data_dir):
    _write(_vault(data_dir), "---\nid: supervisor\nname: Supervisor\n---\n\n## Role\nOld.\n")
    result = await profile_checks_mod._check_supervisor_capability_drift(_ctx(data_dir))
    assert result.severity == Severity.WARN
    assert "profile-reseed" in result.detail


def _registry() -> DoctorRegistry:
    registry = DoctorRegistry()
    for check in profile_checks_mod.profile_checks():
        if check.id == CHECK_ID:
            registry.register(check)
    return registry


async def test_fix_runs_the_same_merge_and_rechecks_ok(check_root, data_dir):
    vault = _vault(data_dir)
    _write(vault, STALE_SUPERVISOR)

    report = await run_doctor(_registry(), _ctx(data_dir), fix=True, only=[CHECK_ID])

    (row,) = report["checks"]
    assert row["severity"] == "ok"
    assert row["fix_applied"] is True
    assert _aq_commands(vault)[-2:] == ["integration_eject", "review_dispatch"]
    assert "operator_custom_command" in _aq_commands(vault)
    assert len(_backups(vault)) == 1


async def test_fix_never_merges_into_an_opted_out_profile(check_root, data_dir):
    vault = _vault(data_dir)
    curated = STALE_SUPERVISOR.replace("---\n\n#", "capability_sync: false\n---\n\n#", 1)
    _write(vault, curated)

    report = await run_doctor(_registry(), _ctx(data_dir), fix=True, only=[CHECK_ID])
    (row,) = report["checks"]
    assert row["severity"] == "info"
    # Called directly (the pool-repair path does), it still refuses.
    result = await profile_checks_mod._fix_supervisor_capability_drift(_ctx(data_dir))
    assert result.fix_applied is False
    assert vault.read_text(encoding="utf-8") == curated


async def test_fix_reports_a_refused_merge(check_root, data_dir):
    _write(_vault(data_dir), "---\nid: supervisor\nname: Supervisor\n---\n\n## Role\nOld.\n")
    with pytest.raises(RuntimeError, match="Capabilities"):
        await profile_checks_mod._fix_supervisor_capability_drift(_ctx(data_dir))


# --- daemon start --------------------------------------------------------------


@pytest.fixture
async def stale_orchestrator(tmp_path):
    """An orchestrator initialised over a vault whose supervisor is stale."""
    from src.orchestrator import Orchestrator

    data_dir = tmp_path / "data"
    shipped = Path(shipped_profile_path("supervisor")).read_text(encoding="utf-8")
    stale = shipped.replace('    "integration_eject",\n', "", 1)
    stale = stale.replace('"aq_commands": [\n', '"aq_commands": [\n    "operator_custom_command",\n', 1)
    assert "integration_eject" not in stale
    _write(_vault(str(data_dir)), stale)

    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("capability-sync.db")),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(data_dir),
    )
    orch = Orchestrator(config)
    await orch.initialize()
    yield orch
    await orch.db.close()


async def test_daemon_start_merges_the_supervisor_before_the_db_sync(stale_orchestrator):
    data_dir = stale_orchestrator.config.data_dir
    commands = _aq_commands(_vault(data_dir))
    assert "integration_eject" in commands
    assert "operator_custom_command" in commands
    assert len(_backups(_vault(data_dir))) == 1

    profile = await stale_orchestrator.db.get_profile("supervisor")
    assert profile is not None
    assert "integration_eject" in profile.aq_commands
    assert "operator_custom_command" in profile.aq_commands
