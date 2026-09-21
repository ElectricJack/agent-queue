"""The four ``providers.*`` availability doctor checks (provider-failover D21).

Every check runs with no orchestrator wired -- ``aq doctor`` must work with
the daemon down -- so each test seeds the stored rows and hands the check a
bare ``DoctorContext(config, db)``.  All four are report-only.
"""

from __future__ import annotations

import sys
import time

import pytest

import src.doctor  # noqa: F401 -- populates sys.modules; see test_provider_doctor.py
from src.config import AppConfig, ProviderFailoverConfig
from src.doctor.models import DoctorContext, Severity
from src.doctor.provider_availability_checks import (
    AVAILABILITY_CHECK_ID,
    FAILOVER_PLAYBOOK_CHECK_ID,
    HELD_TASKS_CHECK_ID,
    RECOVERY_STUCK_CHECK_ID,
    provider_availability_checks,
)
from src.models import AgentProfile, Project, Task, TaskStatus
from src.providers.availability import STARTUP_DIALOG, ProviderAvailability
from src.providers.availability_service import (
    ProviderAvailabilityService,
    availability_to_row,
)

provider_checks = sys.modules["src.doctor.provider_checks"]


@pytest.fixture
async def db(tmp_path):
    from src.database import Database
    from tests.db_fixtures import lease_dsn

    database = Database(lease_dsn("pa.db"))
    await database.initialize()
    yield database
    await database.close()


def _config(**failover) -> AppConfig:
    config = AppConfig()
    cfg = ProviderFailoverConfig()
    for key, value in failover.items():
        section, _, field = key.partition("__")
        if field:
            setattr(getattr(cfg, section), field, value)
        else:
            setattr(cfg, section, value)
    config.provider_failover = cfg
    return config


async def _run(db, check_id: str, config: AppConfig | None = None):
    check = {c.id: c for c in provider_availability_checks()}[check_id]
    return await check.run(DoctorContext(config=config or _config(), db=db))


async def _save(db, row: ProviderAvailability) -> None:
    await db.save_provider_availability(availability_to_row(row))


async def _service(db, config: AppConfig | None = None) -> ProviderAvailabilityService:
    service = ProviderAvailabilityService(db=db, config_getter=lambda: config or _config())
    await service.load()
    return service


async def _unauthenticated(db, provider: str = "codex", config=None) -> None:
    service = await _service(db, config)
    for _ in range(2):
        await service.record(provider, STARTUP_DIALOG, "auth", project_id="p1")


def test_every_availability_check_is_registered_and_report_only() -> None:
    ids = {c.id for c in provider_checks.CHECKS}
    for check_id in (
        AVAILABILITY_CHECK_ID,
        RECOVERY_STUCK_CHECK_ID,
        FAILOVER_PLAYBOOK_CHECK_ID,
        HELD_TASKS_CHECK_ID,
    ):
        assert check_id in ids
    assert all(c.fix is None for c in provider_availability_checks())


# -- providers.availability ----------------------------------------------------


async def test_availability_ok_with_nothing_recorded(db) -> None:
    result = await _run(db, AVAILABILITY_CHECK_ID)
    assert result.severity == Severity.OK


async def test_availability_warns_with_reason_and_remediation(db) -> None:
    now = time.time()
    await _save(db, ProviderAvailability(provider="claude", vendor="anthropic", since=now))
    await _unauthenticated(db)
    result = await _run(db, AVAILABILITY_CHECK_ID)
    assert result.severity == Severity.WARN
    assert "codex unauthenticated since" in result.detail
    assert "codex login" in result.detail


async def test_availability_errors_when_every_session_provider_is_down(db) -> None:
    await _unauthenticated(db, "codex")
    await _unauthenticated(db, "claude")
    # The direct path does not count toward "every provider".
    await _save(db, ProviderAvailability(provider="llm", since=time.time()))
    result = await _run(db, AVAILABILITY_CHECK_ID)
    assert result.severity == Severity.ERROR
    assert "every session provider is unavailable" in result.detail


async def test_an_operator_disable_is_info_not_a_failure(db) -> None:
    now = time.time()
    await _save(db, ProviderAvailability(provider="claude", since=now))
    await _save(
        db,
        ProviderAvailability(
            provider="codex",
            since=now,
            override_state="disabled",
            override_until=now + 3600,
            override_by="human:local-operator",
            override_reason="rotating",
            override_set_at=now,
        ),
    )
    result = await _run(db, AVAILABILITY_CHECK_ID)
    assert result.severity == Severity.INFO
    assert "rotating" in result.detail


async def test_availability_off_is_info(db) -> None:
    await _unauthenticated(db)
    result = await _run(db, AVAILABILITY_CHECK_ID, _config(mode="off"))
    assert result.severity == Severity.INFO


# -- providers.recovery_stuck --------------------------------------------------


async def test_recovery_stuck_past_until_without_probation(db) -> None:
    now = time.time()
    await _save(
        db,
        ProviderAvailability(
            provider="codex", state="exhausted", reason_code="usage_exhausted",
            since=now - 7200, until=now - 600,
        ),
    )
    result = await _run(db, RECOVERY_STUCK_CHECK_ID)
    assert result.severity == Severity.WARN
    assert result.data["stuck"][0]["problem"] == "past_recovery"


async def test_recovery_on_schedule_inside_the_grace(db) -> None:
    now = time.time()
    await _save(
        db,
        ProviderAvailability(provider="codex", state="exhausted", since=now - 100, until=now + 60),
    )
    result = await _run(db, RECOVERY_STUCK_CHECK_ID)
    assert result.severity == Severity.OK


async def test_recovery_stuck_unauthenticated_with_no_probe(db) -> None:
    now = time.time()
    interval = ProviderFailoverConfig().recovery.auth_probe_interval_seconds
    await _save(
        db,
        ProviderAvailability(
            provider="codex", state="unauthenticated", since=now - 10 * interval,
            last_probe_at=now - 4 * interval,
        ),
    )
    result = await _run(db, RECOVERY_STUCK_CHECK_ID)
    assert result.severity == Severity.WARN
    assert result.data["stuck"][0]["problem"] == "no_auth_probe"


async def test_unauthenticated_with_a_recent_probe_is_fine(db) -> None:
    now = time.time()
    await _save(
        db,
        ProviderAvailability(
            provider="codex", state="unauthenticated", since=now - 3600, last_probe_at=now - 30,
        ),
    )
    result = await _run(db, RECOVERY_STUCK_CHECK_ID)
    assert result.severity == Severity.OK


# -- providers.failover_playbook -----------------------------------------------


class _Activations:
    def __init__(self, rows):
        self.rows = rows

    async def list_playbook_activations(self, *, enabled_only=False):
        return list(self.rows)


async def test_failover_playbook_not_shipped_is_info() -> None:
    result = await _run(_Activations([]), FAILOVER_PLAYBOOK_CHECK_ID)
    assert result.severity == Severity.INFO
    assert "not shipped" in result.detail


async def test_failover_playbook_present_but_inactive_warns() -> None:
    rows = [{"activation_id": "a1", "playbook_id": "provider-failover", "enabled": False,
             "active_artifact_sha256": None}]
    result = await _run(_Activations(rows), FAILOVER_PLAYBOOK_CHECK_ID)
    assert result.severity == Severity.WARN
    assert "tasks will hold and never move" in result.detail


async def test_failover_playbook_active_is_ok() -> None:
    rows = [{"activation_id": "a1", "playbook_id": "provider-failover", "enabled": True,
             "active_artifact_sha256": "abc"}]
    result = await _run(_Activations(rows), FAILOVER_PLAYBOOK_CHECK_ID)
    assert result.severity == Severity.OK


async def test_failover_playbook_irrelevant_when_rerouting_is_off() -> None:
    rows = [{"activation_id": "a1", "playbook_id": "provider-failover", "enabled": False}]
    for config in (_config(mode="observe"), _config(reroute__enabled=False)):
        result = await _run(_Activations(rows), FAILOVER_PLAYBOOK_CHECK_ID, config)
        assert result.severity == Severity.INFO


# -- providers.held_tasks ------------------------------------------------------


async def _seed_codex_task(db) -> None:
    await db.create_profile(
        AgentProfile(id="standard-high-codex", name="codex", harness="codex",
                     default_class="standard-high")
    )
    await db.create_project(Project(id="p1", name="p1"))
    await db.create_task(
        Task(id="t1", project_id="p1", title="t", description="d", status=TaskStatus.READY,
             profile_id="standard-high-codex", intelligence_class="standard-high")
    )


async def test_held_tasks_ok_when_nothing_is_suppressed(db) -> None:
    await _seed_codex_task(db)
    result = await _run(db, HELD_TASKS_CHECK_ID)
    assert result.severity == Severity.OK
    assert "no provider is holding work" in result.detail


async def test_held_tasks_warns_past_the_threshold_grouped_by_kind(db) -> None:
    await _seed_codex_task(db)
    # Claude is up, but runs a different class: no equivalent rung.
    await db.create_profile(
        AgentProfile(id="fast-low-claude", name="claude", harness="claude",
                     default_class="fast-low")
    )
    await _save(
        db,
        ProviderAvailability(
            provider="codex", state="unauthenticated", reason_code="login_required",
            since=time.time() - 20000,
        ),
    )
    result = await _run(db, HELD_TASKS_CHECK_ID)
    assert result.severity == Severity.WARN
    assert result.data["held"][0]["task_id"] == "t1"
    # No other provider runs standard-high here: the Astra case.
    assert "no_equivalent_rung" in result.data["by_kind"]


async def test_held_tasks_name_every_provider_down(db) -> None:
    await _seed_codex_task(db)
    await _save(
        db,
        ProviderAvailability(provider="codex", state="unauthenticated", since=time.time() - 20000),
    )
    result = await _run(db, HELD_TASKS_CHECK_ID)
    assert "all_providers_unavailable" in result.data["by_kind"]


async def test_held_tasks_under_the_threshold_is_ok(db) -> None:
    await _seed_codex_task(db)
    await _save(
        db,
        ProviderAvailability(provider="codex", state="unauthenticated", since=time.time() - 60),
    )
    result = await _run(db, HELD_TASKS_CHECK_ID)
    assert result.severity == Severity.OK
    assert "1 task(s) held" in result.detail
