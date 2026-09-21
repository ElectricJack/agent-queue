"""Provider outages page a human only when one must act (provider-failover D19).

The active Discord half of D19: one durable escalation per outage, with
``source_kind = "provider_availability"``, filed under the project whose
queued work the outage strands most, resolved when the condition clears --
and idempotent across the half-change notification, an event replay, the
command and the periodic tick.  Real PostgreSQL, a fake clock and an injected
auth probe -- no CLI, no LLM, no Discord.
"""

from __future__ import annotations

import pytest

from src.config import AppConfig
from src.database import Database
from src.escalations.facts import KIND_ROOT, EscalationFacts
from src.escalations.plan import plan_deliveries
from src.models import AgentProfile, Project, Task, TaskStatus
from src.providers.availability import (
    DEGRADED,
    EXHAUSTED,
    FAILING,
    LAUNCH_FAILURE,
    UNAUTHENTICATED,
)
from src.providers.availability_service import (
    ESCALATION_SOURCE_KIND,
    ProviderAvailabilityService,
)
from src.providers.snapshot import ProviderUsageSnapshot
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.provider import SessionDiedDuringStartup
from tests.db_fixtures import lease_dsn

T0 = 1_789_958_640.0
HOUR = 3600.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))

    def of(self, event_type):
        return [data for kind, data in self.events if kind == event_type]


class Probe:
    """An injected auth probe; every provider is signed in unless told otherwise."""

    def __init__(self):
        self.answers: dict[str, str] = {}

    async def __call__(self, provider, timeout):
        return self.answers.get(provider, "authenticated")


@pytest.fixture
async def db():
    database = Database(lease_dsn("provider-escalation.db"))
    await database.initialize()
    await database.create_profile(AgentProfile(id="std-codex", name="c", harness="codex"))
    await database.create_profile(AgentProfile(id="std-claude", name="c", harness="claude"))
    for pid, profile in (("alpha", "std-codex"), ("beta", "std-codex"), ("gamma", "std-claude")):
        await database.create_project(Project(id=pid, name=pid, default_profile_id=profile))
    yield database
    await database.close()


def registry() -> HarnessRegistry:
    reg = HarnessRegistry()
    reg.upsert(Harness(id="codex", name="codex", command="codex"))
    reg.upsert(Harness(id="claude", name="claude", command="claude"))
    return reg


class Env:
    def __init__(self, db) -> None:
        self.db = db
        self.clock = Clock()
        self.bus = Bus()
        self.probe = Probe()
        self.config = AppConfig()
        self.service = self.new_service()

    def new_service(self) -> ProviderAvailabilityService:
        """A service over the same database -- what a daemon restart builds."""
        service = ProviderAvailabilityService(
            db=self.db,
            config_getter=lambda: self.config,
            bus=self.bus,
            harness_registry=registry(),
            probe=self.probe,
            clock=self.clock,
        )

        async def on_half(transition):
            return None

        service.on_half_change = on_half
        return service

    async def queue(self, task_id: str, project_id: str, profile_id: str | None = None) -> None:
        await self.db.create_task(
            Task(
                id=task_id,
                project_id=project_id,
                title=task_id,
                description="",
                status=TaskStatus.READY,
                profile_id=profile_id,
            )
        )

    async def log_out(self, provider: str = "codex") -> None:
        """The 2026-09-20 incident: a login dialog, confirmed by the probe."""
        self.probe.answers[provider] = "not_authenticated"
        await self.service.record_startup_death(
            SessionDiedDuringStartup(
                "s-1",
                detail="quarantine dialog 'login-required' matched during startup",
                dialog="login-required",
                signal="auth",
            ),
            harness=provider,
            project_id="alpha",
        )
        await self.service.wait_for_probes()
        assert self.service.effective_state(provider) == UNAUTHENTICATED

    async def exhaust(self, provider: str, resets_in: float) -> None:
        await self.db.record_provider_usage([
            ProviderUsageSnapshot(
                provider=provider, window="primary", used_percent=100.0,
                observed_at=self.clock.now, source="transcript",
                resets_at=self.clock.now + resets_in,
            )
        ])
        await self.service.tick()
        assert self.service.effective_state(provider) == EXHAUSTED

    async def incidents(self, *, states=None) -> list[dict]:
        return await self.db.list_escalations(source_kind=ESCALATION_SOURCE_KIND, states=states)


@pytest.fixture
def env(db):
    return Env(db)


def by_provider(incidents: list[dict]) -> dict[str, dict]:
    return {row["source_identity"].rpartition(":")[0]: row for row in incidents}


# -- filing ------------------------------------------------------------------------


async def test_a_logged_out_provider_files_exactly_one_escalation(env):
    await env.queue("b-1", "beta")
    await env.queue("b-2", "beta")
    await env.queue("a-1", "alpha")
    await env.queue("g-1", "gamma")  # claude: not stranded by codex
    await env.log_out("codex")
    generation = env.service.row("codex").generation

    await env.service.tick()  # the transition marked it due
    first = await env.service.notify_state_change("codex")  # the half-change message
    again = await env.service.notify_state_change("codex")  # an event replay
    env.clock.tick(120)
    await env.service.tick()  # the periodic reconcile
    restarted = env.new_service()  # a daemon restart
    await restarted.load()
    await restarted.tick()

    incidents = await env.incidents()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["project_id"] == "beta"  # most stranded tasks
    assert incident["supervisor_owner"] == "supervisor-beta"
    assert incident["task_id"] is None
    assert incident["source_identity"] == f"codex:{generation}"
    assert incident["incident_key"] == f"provider:codex:{generation}"
    assert (incident["state"], incident["severity"]) == ("needs_human", "high")
    assert "codex" in incident["summary"]
    assert "codex login" in incident["decision_requested"]
    assert "Queued tasks routed to codex: 3 (alpha 1, beta 2)" in incident["investigation"]
    assert first["escalation"]["outcome"] == "open"
    assert again["escalation"]["outcome"] == "open"
    created = env.bus.of("escalation.created.v1")
    assert len(created) == 1
    assert created[0]["version"] == 1
    assert created[0]["source_kind"] == ESCALATION_SOURCE_KIND
    # Discord gets one root post, hence one thread; nothing else is owed yet.
    plan = plan_deliveries(EscalationFacts.from_row(incident), deliveries=[], messages=[])
    assert [delivery.kind for delivery in plan.deliveries] == [KIND_ROOT]


async def test_ties_are_broken_by_project_id(env):
    await env.queue("b-1", "beta")
    await env.queue("a-1", "alpha")
    await env.log_out("codex")
    await env.service.tick()
    (incident,) = await env.incidents()
    assert incident["project_id"] == "alpha"


async def test_nothing_is_filed_until_the_outage_strands_work(env):
    await env.log_out("codex")
    outcomes = await env.service.reconcile_escalations()
    assert {o["provider"]: o["outcome"] for o in outcomes}["codex"] == "no_affected_tasks"
    assert await env.incidents() == []

    await env.queue("a-1", "alpha")
    env.clock.tick(61)
    await env.service.tick()
    (incident,) = await env.incidents()
    assert incident["project_id"] == "alpha"


# -- resolution -----------------------------------------------------------------------


async def test_recovery_resolves_the_escalation_and_the_next_outage_is_new(env):
    await env.queue("a-1", "alpha")
    await env.log_out("codex")
    await env.service.tick()
    (first,) = await env.incidents(states=["needs_human"])

    env.probe.answers["codex"] = "authenticated"
    await env.service.recheck("codex")  # the human ran `codex login`
    assert env.service.effective_state("codex") == DEGRADED  # probation
    await env.service.tick()

    resolved = await env.db.get_escalation(first["id"])
    assert resolved["state"] == "resolved"
    assert "launchable again" in resolved["terminal_outcome"]
    assert resolved["terminal_evidence"]["provider"] == "codex"
    updated = [e for e in env.bus.of("escalation.updated.v1") if e["state"] == "resolved"]
    assert [e["escalation_id"] for e in updated] == [first["id"]]

    # Logged out again: a new outage is a new incident in the same project --
    # the incident key carries the generation, so the resolved one is no bar.
    env.clock.tick(600)
    await env.log_out("codex")
    await env.service.tick()
    open_now = await env.incidents(states=["needs_human"])
    assert len(open_now) == 1 and open_now[0]["id"] != first["id"]
    assert open_now[0]["project_id"] == "alpha"


async def test_an_outage_a_human_closed_is_not_paged_again(env):
    await env.queue("a-1", "alpha")
    await env.log_out("codex")
    await env.service.tick()
    (incident,) = await env.incidents()
    await env.db.transition_escalation(
        incident["id"],
        expected_revision=incident["revision"],
        new_state="cancelled",
        terminal_outcome="operator is on it",
    )
    env.clock.tick(61)
    await env.service.tick()
    outcome = (await env.service.reconcile_escalations("codex"))[0]
    assert outcome["outcome"] == "closed"
    assert len(await env.incidents()) == 1


# -- what never escalates --------------------------------------------------------------


async def test_exhausted_with_a_known_reset_never_escalates(env):
    await env.queue("a-1", "alpha")
    await env.exhaust("codex", resets_in=6 * HOUR)
    for _ in range(3):
        env.clock.tick(HOUR)
        await env.service.tick()
    assert env.service.effective_state("codex") == EXHAUSTED
    result = await env.service.notify_state_change("codex")
    assert result["escalation"]["outcome"] == "not_needed"
    assert await env.incidents() == []


async def test_an_operator_disabled_provider_never_escalates(env):
    await env.queue("a-1", "alpha")
    await env.service.set_state("codex", "disabled", by="human:test", reason="maintenance")
    env.clock.tick(HOUR)
    await env.service.tick()
    assert await env.incidents() == []


# -- failing -------------------------------------------------------------------------------


async def test_failing_escalates_only_once_it_has_lasted(env):
    env.config.provider_failover.notify.escalate_failing_after_seconds = 120
    await env.queue("a-1", "alpha")
    for i in range(env.config.provider_failover.launch.generic_failures_to_trip):
        await env.service.record("codex", LAUNCH_FAILURE, project_id=f"p{i % 2}")
    assert env.service.effective_state("codex") == FAILING
    await env.service.tick()
    assert await env.incidents() == []

    env.clock.tick(121)
    await env.service.tick()
    (incident,) = await env.incidents()
    assert incident["severity"] == "high"
    assert "failing" in incident["summary"]

    # The failure backoff passes and the provider goes on probation.
    env.clock.tick(env.config.provider_failover.recovery.failing_backoff_seconds)
    await env.service.tick()
    assert env.service.effective_state("codex") == DEGRADED
    assert (await env.db.get_escalation(incident["id"]))["state"] == "resolved"


# -- every provider down ----------------------------------------------------------------------


async def test_every_provider_down_is_critical_and_steps_back_down(env):
    await env.queue("a-1", "alpha")
    await env.queue("g-1", "gamma")
    await env.log_out("codex")
    await env.service.tick()
    assert by_provider(await env.incidents())["codex"]["severity"] == "high"

    await env.exhaust("claude", resets_in=3 * HOUR)  # due back well after the threshold
    incidents = by_provider(await env.incidents(states=["needs_human"]))
    assert incidents["codex"]["severity"] == "critical"
    assert incidents["claude"]["severity"] == "critical"
    assert incidents["claude"]["project_id"] == "gamma"
    assert incidents["claude"]["summary"].startswith("Every provider is unavailable")
    assert "Every session provider is unavailable" in incidents["claude"]["investigation"]

    # Claude comes back: its all-down incident resolves, codex's is high again.
    await env.service.set_state(
        "claude", "available", by="human:test", reason="bought more", until=T0 + 10 * HOUR
    )
    await env.service.tick()
    open_now = by_provider(await env.incidents(states=["needs_human"]))
    assert set(open_now) == {"codex"}
    assert open_now["codex"]["severity"] == "high"
    claude = by_provider(await env.incidents())["claude"]
    assert claude["state"] == "resolved"


async def test_everything_exhausted_but_due_back_soon_is_not_escalated(env):
    await env.queue("a-1", "alpha")
    await env.queue("g-1", "gamma")
    await env.exhaust("codex", resets_in=600)
    await env.exhaust("claude", resets_in=4 * HOUR)
    # All down, but codex is due back inside notify.escalate_all_down_after_seconds.
    await env.service.tick()
    assert await env.incidents() == []


async def test_everything_exhausted_for_hours_is_escalated(env):
    await env.queue("a-1", "alpha")
    await env.queue("g-1", "gamma")
    await env.exhaust("codex", resets_in=3 * HOUR)
    await env.exhaust("claude", resets_in=4 * HOUR)
    await env.service.tick()
    incidents = by_provider(await env.incidents())
    assert set(incidents) == {"codex", "claude"}
    assert {row["severity"] for row in incidents.values()} == {"critical"}


# -- cost and safety --------------------------------------------------------------------------


async def test_a_quiet_fleet_costs_the_tick_no_query(env, monkeypatch):
    await env.service.tick()  # the first reconcile looks: nothing open
    calls = []
    original = env.db.list_escalations

    async def counting(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.db, "list_escalations", counting)
    env.clock.tick(61)
    await env.service.tick()
    assert calls == []


async def test_recovery_resolution_cannot_close_another_producers_incident(env):
    incident, _ = await env.db.create_escalation(
        id="esc-question",
        project_id="alpha",
        source_kind="question",
        source_identity="q-1",
        incident_key="question:q-1",
        supervisor_owner="supervisor-alpha",
        summary="s",
        investigation="i",
        decision_requested="d",
        severity="medium",
    )
    refused = await env.db.resolve_escalation_on_recovery(
        incident["id"],
        expected_revision=incident["revision"],
        source_kind=ESCALATION_SOURCE_KIND,
        terminal_outcome="recovered",
        terminal_evidence={},
    )
    assert refused is None
    stale = await env.db.resolve_escalation_on_recovery(
        incident["id"],
        expected_revision=incident["revision"] + 1,
        source_kind="question",
        terminal_outcome="recovered",
        terminal_evidence={},
    )
    assert stale is None
    assert (await env.db.get_escalation(incident["id"]))["state"] == "needs_human"
