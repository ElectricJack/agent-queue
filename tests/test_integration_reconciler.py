"""Fake-clock visits against PostgreSQL: lost events, replay, fencing and shadow."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from src.database import Database
from src.database.tables import playbook_artifacts
from src.event_bus import EventBus
from src.integration.reconciler import CompiledPolicyAdapter, IntegrationReconciler, VisitTransition
from src.integration.subjects import (
    Decision,
    GateArgs,
    GateFacts,
    HoldFacts,
    JournalMode,
    PolicyArtifactPin,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    SealArgs,
    Subject,
    SubjectEngine,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    SubjectState,
    WaitArgs,
)
from tests.db_fixtures import lease_dsn

PIN = PolicyArtifactPin(playbook_id="root-table", artifact_sha256="sha256:" + "1" * 64)


@pytest.fixture
async def db():
    database = Database(lease_dsn("reconciler"))
    await database.initialize()
    async with database.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=PIN.artifact_sha256,
                playbook_id=PIN.playbook_id,
                source_digest="sha256:" + "2" * 64,
                contract_fingerprint="sha256:" + "3" * 64,
                compiler_build="test",
                path="/test/root-table.json",
                created_at=1.0,
            )
        )
    yield database
    await database.close()


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Policy:
    def __init__(self, request=None):
        self.request = request or SealArgs()
        self.decisions = []
        self.settlements = []

    async def decide(self, subject, facts):
        self.decisions.append((subject, facts))
        return Decision(
            subject_id=subject.id,
            subject_version=subject.version,
            policy=subject.policy,
            rule="root-building",
            facts_digest=facts.digest(),
            request=self.request,
        )

    async def settle(self, subject, decision, outcome, *, now):
        self.settlements.append(outcome)
        return VisitTransition(
            schedule=SubjectSchedule.progress(
                now=now,
                max_wait_seconds=subject.schedule.max_wait_seconds,
            )
        )


class PurePolicy:
    """The policy compiler's synchronous public interface, without compiler imports."""

    def __init__(self, request=None):
        self.request = request or SealArgs()
        self.outcomes = []

    def evaluate(self, subject, facts):
        return Decision(
            subject_id=subject.id,
            subject_version=subject.version,
            policy=subject.policy,
            rule="compiled-case",
            facts_digest=facts.digest(),
            request=self.request,
        )

    def schedule(self, subject, decision, outcome, *, now):
        self.outcomes.append(outcome)
        bound = min(subject.schedule.max_wait_seconds, 30)
        if outcome.is_unknown or outcome.primitive is Primitive.WAIT:
            return SubjectSchedule.wait(
                now=now,
                until=now + 17,
                reason="compiled-route",
                max_wait_seconds=bound,
            )
        if outcome.primitive is Primitive.GATE:
            return SubjectSchedule.hold(
                now=now,
                gate_id=outcome.detail["gate_id"],
                max_wait_seconds=bound,
                revisit_at=now if outcome.outcome == "answered" else outcome.detail["timeout_at"],
            )
        return SubjectSchedule.progress(now=now, max_wait_seconds=bound)

    def phase(self, subject, decision, outcome):
        return SubjectPhase.TESTING if outcome.outcome == "sealed" else None


async def add_subject(db, clock, subject_id="s1", **overrides):
    values = dict(
        id=subject_id,
        project_id="p",
        repository_id="repo",
        kind=SubjectKind.ROOT_BATCH,
        subject_key=f"root_batch:repo:{subject_id}",
        engine=SubjectEngine.RECONCILER,
        phase=SubjectPhase.BUILDING,
        policy=PIN,
        target_ref="refs/heads/aq/test",
        head_sha="a" * 40,
        base_sha="b" * 40,
        generation=2,
        schedule=SubjectSchedule.progress(now=clock(), max_wait_seconds=60),
        created_at=clock(),
        updated_at=clock(),
    )
    values.update(overrides)
    subject = Subject(**values)
    await db.ensure_integration_subject(subject.to_row())
    return subject


async def read_subject(db, subject_id="s1"):
    return Subject.from_row(await db.get_integration_subject(subject_id))


def make_loop(db, clock, *, policy=None, observer=None, port=None, **options):
    observations, actions = [], []
    policy = policy or Policy()

    async def observe(subject):
        observations.append(subject.id)
        if observer:
            return await observer(subject)
        return SubjectFacts(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=clock(),
            head=subject.head,
            wait_overdue=subject.wait_overdue(clock()),
        )

    async def act(subject, args):
        actions.append((subject.id, args))
        # Prove the decision was durable before any external write.
        entries = await db.list_integration_subject_journal(subject.id)
        assert entries[-1]["entry_kind"] == "decision"
        if port:
            return await port(subject, args)
        return PrimitiveOutcome(primitive=Primitive.SEAL, outcome="sealed")

    ports = PrimitivePorts({Primitive.SEAL: act, Primitive.GATE: act})
    mode = options.pop("mode", JournalMode.ACTIVE)
    loop = IntegrationReconciler(db, observe, policy, ports, mode=mode, clock=clock, **options)
    return SimpleNamespace(loop=loop, policy=policy, observations=observations, actions=actions)


async def test_lost_event_and_restart_revisit_a_bounded_wait(db):
    clock = Clock()
    await add_subject(db, clock)
    harness = make_loop(db, clock, policy=Policy(WaitArgs(seconds=999, reason="CI pending")))
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.state is SubjectState.WAITING
    assert subject.schedule.next_due_at == clock() + 60
    assert harness.actions == []
    # No event arrives; a completely new loop has no cursor or in-memory state.
    clock.advance(61)
    restarted = make_loop(db, clock)
    await restarted.loop.tick()
    assert restarted.policy.decisions[0][1].wait_overdue
    assert len(restarted.actions) == 1
    assert (await read_subject(db)).schedule.next_due_at == clock()


async def test_exactly_one_action_and_atomic_result_per_visit(db):
    clock = Clock()
    await add_subject(db, clock)
    harness = make_loop(db, clock)
    await harness.loop.tick()
    assert len(harness.policy.decisions) == len(harness.observations) == len(harness.actions) == 1
    subject = await read_subject(db)
    assert subject.version == 1 and subject.last_visit_at == clock()
    entries = await db.list_integration_subject_journal("s1")
    assert [entry["entry_kind"] for entry in entries] == ["decision", "action"]
    assert {entry["head_sha"] for entry in entries} == {"a" * 40}
    assert {entry["generation"] for entry in entries} == {2}
    assert {entry["policy_artifact_sha256"] for entry in entries} == {PIN.artifact_sha256}
    assert entries[0]["payload"]["facts"]["subject_version"] == 0
    assert entries[1]["outcome"] == "sealed"


async def test_shadow_journals_choice_without_action_settlement_or_domain_write(db):
    clock = Clock()
    original = await add_subject(db, clock)
    harness = make_loop(db, clock, mode=JournalMode.SHADOW)
    await harness.loop.tick()
    after = await read_subject(db)
    assert harness.actions == [] and harness.policy.settlements == []
    for column in (
        "engine",
        "phase",
        "target_ref",
        "head_sha",
        "base_sha",
        "generation",
        "writer",
        "budget",
    ):
        assert getattr(original, column) == getattr(after, column)
    assert after.schedule.next_due_at == clock() + 60
    entries = await db.list_integration_subject_journal("s1")
    assert len(entries) == 1 and entries[0]["mode"] == "shadow"
    assert entries[0]["primitive"] == Primitive.SEAL.value
    assert entries[0]["entry_kind"] == "decision"




@pytest.mark.parametrize("failure", ["none", "exception", "timeout"])
async def test_git_first_diagnostics_cannot_change_selected_policy_or_emit_actions(db, failure):
    from src.integration.shadow import GitFirstDiagnostics

    clock = Clock()
    original = await add_subject(db, clock)
    digests = []

    async def diagnose(subject, facts):
        digests.append(facts.digest())
        GitFirstDiagnostics.record(subject, "repair_progress", False, True, reason="fixture")
        if failure == "exception":
            raise RuntimeError("unavailable Git")

    harness = make_loop(db, clock, policy=Policy(WaitArgs(seconds=5, reason="old-policy")),
                        diagnostics=diagnose)
    await harness.loop.tick()
    assert harness.actions == []
    assert digests == [harness.policy.decisions[0][1].digest()]
    assert harness.policy.settlements == []
    entries = await db.list_integration_subject_journal("s1")
    assert [entry["primitive"] for entry in entries] == [Primitive.WAIT.value] * 2
    assert not any("git_first" in entry["payload"] for entry in entries)
    after = await read_subject(db)
    assert after.engine is original.engine
    assert after.schedule.wait_reason == "old-policy"
    assert after.schedule.next_due_at == clock() + 5
    assert after.head_sha == original.head_sha and after.writer == original.writer


async def test_git_first_diagnostics_skip_legacy_ownership_even_in_existing_shadow_loop(db):
    from unittest.mock import AsyncMock

    clock = Clock()
    await add_subject(db, clock, engine=SubjectEngine.LEGACY)
    diagnose = AsyncMock()
    harness = make_loop(db, clock, mode=JournalMode.SHADOW, diagnostics=diagnose)
    await harness.loop.tick()
    diagnose.assert_not_awaited()
    assert harness.actions == []


async def test_unknown_refusal_has_bounded_exponential_backoff(db):
    clock = Clock()
    await add_subject(db, clock)

    async def unknown(subject, args):
        return PrimitiveOutcome.unknown(Primitive.SEAL, "remote_unavailable")

    harness = make_loop(db, clock, port=unknown, backoff_ceiling_seconds=999)
    for expected_delay in (5, 10, 20, 40, 60, 60):
        await harness.loop.tick()
        subject = await read_subject(db)
        assert subject.schedule.next_due_at == clock() + expected_delay
        assert subject.schedule.wait_reason == "remote_unavailable"
        clock.advance(expected_delay)


@pytest.mark.parametrize("no_default", [True, False])
async def test_gate_has_explicit_hold_and_timeout_or_human_answer(db, no_default):
    clock = Clock()
    await add_subject(db, clock)
    request = GateArgs(
        question="Repair exhausted?",
        choices=("retry", "hold"),
        no_default=no_default,
        default_choice=None if no_default else "retry",
        default_after_seconds=None if no_default else 30,
    )

    async def gate(subject, args):
        return PrimitiveOutcome(
            primitive=Primitive.GATE, outcome="created", detail={"gate_id": "g1"}
        )

    harness = make_loop(db, clock, policy=Policy(request), port=gate)
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.state is SubjectState.HELD and subject.schedule.gate_id == "g1"
    assert subject.schedule.next_due_at == (None if no_default else clock() + 30)
    # Answers accelerate even a no-default hold; timeouts survive a restart.
    clock.advance(30)
    if no_default:
        await harness.loop.on_event({"_event_type": "gate.answered", "gate_id": "g1"})
    restarted = make_loop(db, clock)
    await restarted.loop.tick()
    assert restarted.observations == ["s1"]


async def test_gate_without_recorded_identity_is_a_retry_not_an_indefinite_hold(db):
    clock = Clock()
    await add_subject(db, clock)

    async def missing(subject, args):
        return PrimitiveOutcome(primitive=Primitive.GATE, outcome="created")

    harness = make_loop(
        db,
        clock,
        policy=Policy(
            GateArgs(
                question="Hold?",
                choices=("hold",),
                no_default=True,
            )
        ),
        port=missing,
    )
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.state is SubjectState.WAITING
    assert subject.schedule.wait_reason == "gate outcome missing gate_id"


async def test_reused_gate_keeps_its_absolute_deadline_after_an_early_event(db):
    clock = Clock()
    deadline = clock() + 30
    await add_subject(
        db,
        clock,
        schedule=SubjectSchedule.hold(
            now=clock(),
            gate_id="g1",
            max_wait_seconds=60,
            revisit_at=deadline,
        ),
    )
    clock.advance(5)

    async def observer(subject):
        return SubjectFacts(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=clock(),
            gate=GateFacts(
                gate_id="g1", status="open", timeout_at=deadline, default_choice="retry"
            ),
        )

    async def reused(subject, args):
        return PrimitiveOutcome(
            primitive=Primitive.GATE, outcome="reused", detail={"gate_id": "g1"}
        )

    harness = make_loop(
        db,
        clock,
        observer=observer,
        port=reused,
        policy=Policy(
            GateArgs(
                question="Repair exhausted?",
                choices=("retry",),
                default_choice="retry",
                default_after_seconds=30,
            )
        ),
    )
    await harness.loop.on_event({"_event_type": "gate.updated", "gate_id": "g1"})
    await harness.loop.tick()
    # The racing wake keeps it due now; the next visit must recover the original
    # deadline, rather than reset the gate's timeout by another thirty seconds.
    clock.advance(1)
    await harness.loop.tick()
    assert (await read_subject(db)).schedule.next_due_at == deadline


async def test_event_during_visit_survives_schedule_commit_without_duplicate_action(db):
    clock = Clock()
    await add_subject(db, clock)
    policy = Policy(WaitArgs(seconds=60, reason="CI pending"))

    async def racing_observer(subject):
        clock.advance(1)
        await db.wake_integration_subjects(now=clock(), subject_ids=(subject.id,))
        return SubjectFacts(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=clock(),
        )

    harness = make_loop(db, clock, policy=policy, observer=racing_observer)
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.schedule.next_due_at == clock()  # wake is not overwritten by +60 wait
    assert subject.version == 1 and not harness.actions
    assert len(await db.list_integration_subject_journal("s1")) == 2


async def test_event_bus_only_wakes_matching_live_subjects_and_unsubscribes(db):
    clock = Clock()
    await add_subject(
        db,
        clock,
        schedule=SubjectSchedule.wait(
            now=clock(),
            until=clock() + 60,
            reason="CI",
            max_wait_seconds=60,
        ),
    )
    await add_subject(
        db,
        clock,
        "other",
        schedule=SubjectSchedule.wait(
            now=clock(),
            until=clock() + 60,
            reason="CI",
            max_wait_seconds=60,
        ),
    )
    harness = make_loop(db, clock)
    bus = EventBus(validate_events=False)
    harness.loop.subscribe(bus)
    await bus.emit("unrelated.event", {"subject_id": "s1"})
    assert (await read_subject(db)).schedule.next_due_at == clock() + 60
    await bus.emit("integration.ci_completed", {"subject_id": "s1"})
    assert (await read_subject(db)).schedule.next_due_at == clock()
    assert (await read_subject(db)).version == 0
    assert (await read_subject(db, "other")).schedule.next_due_at == clock() + 60
    assert not harness.actions and not await db.list_integration_subject_journal("s1")
    await harness.loop.stop()
    assert bus.subscriber_count("*") == 0


async def test_background_ticks_and_cancelled_waiter_share_one_remote_pass(db):
    clock = Clock()
    await add_subject(db, clock)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(subject, args):
        entered.set()
        await release.wait()
        return PrimitiveOutcome(primitive=Primitive.SEAL, outcome="sealed")

    harness = make_loop(db, clock, port=slow)
    await harness.loop.tick(background=True)
    await entered.wait()
    await harness.loop.tick(background=True)
    waiter = asyncio.create_task(harness.loop.tick())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await harness.loop.tick(background=True)
    assert len(harness.actions) == 1
    release.set()
    await harness.loop.tick()
    assert (await read_subject(db)).version == 1


async def test_restart_after_remote_action_does_not_blindly_repeat_it(db):
    clock = Clock()
    await add_subject(db, clock)
    written = asyncio.Event()

    async def remote_write_then_crash(subject, args):
        written.set()
        await asyncio.Event().wait()

    harness = make_loop(db, clock, port=remote_write_then_crash)
    await harness.loop.tick(background=True)
    await written.wait()
    await harness.loop.stop()
    assert (await read_subject(db)).version == 0
    assert len(await db.list_integration_subject_journal("s1")) == 1
    restarted = make_loop(db, clock)
    await restarted.loop.tick()
    assert restarted.actions == []
    assert (await read_subject(db)).schedule.wait_reason == "interrupted_visit"
    entries = await db.list_integration_subject_journal("s1")
    assert len(entries) == 2 and entries[1]["payload"]["result"]["reason"] == "interrupted_visit"
    clock.advance(5)
    await restarted.loop.tick()
    assert len(restarted.actions) == 1


async def test_action_schedule_transaction_rolls_back_and_restart_reobserves(db, monkeypatch):
    clock = Clock()
    await add_subject(db, clock)
    original = db.update_integration_subject_on

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(db, "update_integration_subject_on", interrupted)
    harness = make_loop(db, clock)
    await harness.loop.tick()  # exception is isolated; the due row is still live
    assert (await read_subject(db)).version == 0
    assert len(await db.list_integration_subject_journal("s1")) == 1
    monkeypatch.setattr(db, "update_integration_subject_on", original)
    restarted = make_loop(db, clock)
    await restarted.loop.tick()
    assert not restarted.actions
    assert (await read_subject(db)).version == 1


async def test_version_change_after_observation_prevents_action(db):
    clock = Clock()
    await add_subject(db, clock)

    async def transferred(subject):
        async with db.immediate() as conn:
            await db.update_integration_subject_on(
                conn,
                subject_id=subject.id,
                expected_version=0,
                values={"wait_reason": "changed during observation"},
                now=clock(),
            )
        return SubjectFacts(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=clock(),
        )

    harness = make_loop(db, clock, observer=transferred)
    await harness.loop.tick()
    assert not harness.actions
    assert not await db.list_integration_subject_journal("s1")
    assert (await read_subject(db)).version == 1


@pytest.mark.parametrize(
    "defect",
    [
        "policy_pin",
        "facts_digest",
        "subject_version",
        "bound",
        "identity",
        "future_clock",
        "generation",
    ],
)
async def test_bad_policy_binding_or_transition_is_bounded_and_auditable(db, defect):
    clock = Clock()
    await add_subject(db, clock)

    class BadPolicy(Policy):
        async def decide(self, subject, facts):
            decision = await super().decide(subject, facts)
            if defect == "policy_pin":
                return decision.model_copy(
                    update={"policy": PIN.model_copy(update={"playbook_id": "other"})}
                )
            if defect == "facts_digest":
                return decision.model_copy(update={"facts_digest": "sha256:" + "f" * 64})
            if defect == "subject_version":
                return decision.model_copy(update={"subject_version": 99})
            return decision

        async def settle(self, subject, decision, outcome, *, now):
            if defect == "future_clock":
                now += 600
            return VisitTransition(
                schedule=SubjectSchedule.wait(
                    now=now,
                    until=now + 600,
                    reason="bad policy",
                    max_wait_seconds=600 if defect == "bound" else 60,
                ),
                values=(
                    {"engine": "legacy"}
                    if defect == "identity"
                    else {"generation": 1}
                    if defect == "generation"
                    else {}
                ),
            )

    harness = make_loop(db, clock, policy=BadPolicy())
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.state is SubjectState.WAITING
    assert subject.schedule.next_due_at == clock() + 5
    assert subject.schedule.max_wait_seconds == 60 and subject.policy == PIN
    assert subject.engine is SubjectEngine.RECONCILER
    if defect in {"policy_pin", "facts_digest", "subject_version"}:
        assert not harness.actions


@pytest.mark.parametrize("hold", ["product", "gate"])
async def test_binding_human_holds_prevent_mutating_decisions(db, hold):
    clock = Clock()
    await add_subject(db, clock)

    async def observer(subject):
        return SubjectFacts(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=clock(),
            holds=(HoldFacts(kind="manual_pause"),) if hold == "product" else (),
            gate=GateFacts(gate_id="g1", status="open", no_default=True)
            if hold == "gate"
            else None,
        )

    harness = make_loop(db, clock, observer=observer)
    await harness.loop.tick()
    assert not harness.actions
    assert (await read_subject(db)).schedule.next_due_at == clock() + 5


async def test_keyset_paging_does_not_starve_later_subjects(db):
    clock = Clock()
    for subject_id in ("s1", "s2", "s3", "s4", "s5"):
        await add_subject(db, clock, subject_id)
    harness = make_loop(db, clock, page_size=2)
    for _ in range(3):
        await harness.loop.tick()
    assert harness.observations == ["s1", "s2", "s3", "s4", "s5"]
    await harness.loop.tick()
    assert harness.observations[-2:] == ["s1", "s2"]


async def test_policy_can_close_subject_only_with_consistent_closed_schedule(db):
    clock = Clock()
    await add_subject(db, clock)

    class ClosingPolicy(Policy):
        async def settle(self, subject, decision, outcome, *, now):
            return VisitTransition(
                values={"phase": "done"},
                schedule=SubjectSchedule.close(
                    now=now,
                    reason="delivered",
                    max_wait_seconds=60,
                ),
            )

    harness = make_loop(db, clock, policy=ClosingPolicy())
    await harness.loop.tick()
    await harness.loop.on_event({"_event_type": "integration.subject_due", "subject_id": "s1"})
    await harness.loop.tick()
    assert (await read_subject(db)).state is SubjectState.DONE
    assert len(harness.actions) == 1


async def test_pure_compiler_interface_projects_adapter_domain_values_under_visit_cas(db):
    clock = Clock()
    await add_subject(db, clock)
    policy = PurePolicy()

    async def sealed(subject, args):
        return PrimitiveOutcome(
            primitive=Primitive.SEAL,
            outcome="sealed",
            detail={
                "subject_values": {
                    "batch_id": "batch-1",
                    "head_sha": "c" * 40,
                    "generation": 3,
                    "phase": "repairing",
                },
            },
        )

    harness = make_loop(db, clock, policy=policy, port=sealed)
    await harness.loop.tick()
    subject = await read_subject(db)
    assert isinstance(harness.loop._policy, CompiledPolicyAdapter)
    assert len(policy.outcomes) == 1 and policy.outcomes[0].outcome == "sealed"
    assert (subject.batch_id, subject.head_sha, subject.generation) == ("batch-1", "c" * 40, 3)
    assert subject.phase is SubjectPhase.TESTING  # the reviewed route owns phase
    assert subject.version == 1 and subject.schedule.max_wait_seconds == 60
    entries = await db.list_integration_subject_journal("s1")
    assert entries[-1]["payload"]["transition"]["values"]["head_sha"] == "c" * 40


@pytest.mark.parametrize("primitive", [Primitive.WAIT, Primitive.SEAL])
async def test_compiled_routes_own_wait_and_unknown_schedules(db, primitive):
    clock = Clock()
    await add_subject(db, clock)
    policy = PurePolicy(
        WaitArgs(seconds=60, reason="request") if primitive is Primitive.WAIT else None
    )

    async def unknown(subject, args):
        return PrimitiveOutcome.unknown(
            primitive, "remote_pending", subject_values={"head_sha": "c" * 40}
        )

    harness = make_loop(db, clock, policy=policy, port=unknown)
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.schedule.next_due_at == clock() + 17
    assert subject.schedule.wait_reason == "compiled-route"
    assert subject.schedule.max_wait_seconds == 60  # retain creation bound
    assert subject.head_sha == "a" * 40  # unknown effects are not projected


@pytest.mark.parametrize("outcome", ["reused", "answered"])
async def test_compiled_gate_routes_preserve_deadline_or_answer_for_next_action(db, outcome):
    clock = Clock()
    await add_subject(db, clock)
    deadline = clock() + 30
    request = GateArgs(
        question="Retry?", choices=("retry",), default_choice="retry", default_after_seconds=30
    )

    async def observe(subject):
        return SubjectFacts(
            subject_id=subject.id,
            subject_version=subject.version,
            kind=subject.kind,
            phase=subject.phase,
            observed_at=clock(),
            gate=GateFacts(gate_id="g1", status="open", timeout_at=deadline),
        )

    async def gate(subject, args):
        return PrimitiveOutcome(
            primitive=Primitive.GATE, outcome=outcome, detail={"gate_id": "g1", "choice": "retry"}
        )

    policy = PurePolicy(request)
    harness = make_loop(db, clock, policy=policy, observer=observe, port=gate)
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.schedule.gate_id == "g1"
    assert subject.schedule.next_due_at == (deadline if outcome == "reused" else clock())


@pytest.mark.parametrize("values", [{"engine": "legacy"}, {"generation": 1}, ["bad-shape"]])
async def test_adapter_projection_cannot_bypass_identity_or_generation_guards(db, values):
    clock = Clock()
    await add_subject(db, clock)

    async def invalid(subject, args):
        return PrimitiveOutcome(
            primitive=Primitive.SEAL, outcome="sealed", detail={"subject_values": values}
        )

    harness = make_loop(db, clock, policy=PurePolicy(), port=invalid)
    await harness.loop.tick()
    subject = await read_subject(db)
    assert subject.engine is SubjectEngine.RECONCILER and subject.generation == 2
    assert subject.phase is SubjectPhase.BUILDING
    assert subject.schedule.next_due_at == clock() + 5


def test_default_is_shadow_and_invalid_timing_is_rejected():
    loop = IntegrationReconciler(None, None, None, PrimitivePorts())
    assert loop._mode is JournalMode.SHADOW
    for option in (
        "page_size",
        "interval_seconds",
        "call_timeout_seconds",
        "bookkeeping_timeout_seconds",
        "backoff_seconds",
    ):
        with pytest.raises(ValueError):
            IntegrationReconciler(None, None, None, PrimitivePorts(), **{option: 0})
