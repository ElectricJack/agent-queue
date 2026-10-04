"""Integration subjects: the durable rows and typed ports of the reconciler.

``rev-agile-ridge`` revision 2, §3: one subject row, one owner, one due time
and one pinned policy artifact; twenty primitives with closed outcome sets; and
the never-blocked guarantee -- a live subject is held by an explicit gate or is
due again within its bound.  The model halves are pure; the database halves
prove the same rules are enforced by the schema itself.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database import Database, tables
from src.database.tables import (
    integration_subject_journal,
    integration_subjects,
    playbook_artifacts,
)
from src.integration.models import BranchKey, Fence
from src.integration.subjects import (
    DEFAULT_MAX_WAIT_SECONDS,
    OUTCOME_DETAILS,
    PHASE1_ADAPTED_COMMANDS,
    PRIMITIVE_ARGS,
    PRIMITIVE_OUTCOMES,
    UNKNOWN,
    CIEvidence,
    CIObserveArgs,
    CIState,
    ConflictFacts,
    Decision,
    GateArgs,
    HeadIdentity,
    JournalKind,
    JournalMode,
    MemberFacts,
    MemberRef,
    MergeMembersArgs,
    PolicyArtifactPin,
    Primitive,
    PrimitiveArgs,
    PrimitiveOutcome,
    PrimitivePorts,
    PublishArgs,
    Subject,
    SubjectEngine,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    SubjectState,
    WaitArgs,
    WriterBudget,
    WriterLease,
    WriterStatus,
    schedule_values,
    subject_key,
)
from tests.db_fixtures import lease_dsn

NOW = 1_000_000.0
HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
ARTIFACT = "sha256:" + "1" * 64
NEW_ARTIFACT = "sha256:" + "2" * 64
PIN = PolicyArtifactPin(playbook_id="agent-queue-root-train", artifact_sha256=ARTIFACT)


def _subject(**overrides) -> Subject:
    values = {
        "id": "subject-1",
        "project_id": "p",
        "repository_id": "repo",
        "kind": SubjectKind.ROOT_BATCH,
        "subject_key": subject_key(SubjectKind.ROOT_BATCH, "repo", "request-1"),
        "phase": SubjectPhase.BUILDING,
        "policy": PIN,
        "target_ref": "refs/heads/main",
        "head_sha": HEAD,
        "base_sha": BASE,
        "generation": 2,
        "schedule": SubjectSchedule.progress(now=NOW, max_wait_seconds=DEFAULT_MAX_WAIT_SECONDS),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Subject(**values)


# ------------------------------------------------------------------ contracts


def test_schema_vocabularies_match_the_typed_model():
    assert tables.INTEGRATION_SUBJECT_KINDS == tuple(SubjectKind)
    assert tables.INTEGRATION_SUBJECT_PHASES == tuple(SubjectPhase)
    assert tables.INTEGRATION_SUBJECT_ENGINES == tuple(SubjectEngine)
    assert tables.INTEGRATION_WRITER_STATUSES == tuple(WriterStatus)
    assert tables.INTEGRATION_JOURNAL_KINDS == tuple(JournalKind)
    assert tables.INTEGRATION_JOURNAL_MODES == tuple(JournalMode)
    assert tables.INTEGRATION_PRIMITIVES == tuple(Primitive)


def test_the_twenty_primitives_have_closed_outcomes_and_typed_arguments():
    assert len(Primitive) == 20
    assert set(PRIMITIVE_OUTCOMES) == set(Primitive) == set(PRIMITIVE_ARGS)
    adapter = TypeAdapter(PrimitiveArgs)
    for primitive, model in PRIMITIVE_ARGS.items():
        assert model.model_fields["primitive"].default is primitive
        assert PRIMITIVE_OUTCOMES[primitive], primitive
        # ``unknown`` is universal, never listed per primitive -- except the
        # stop proof, whose own closed set names it (§3.4 row 13).
        assert (UNKNOWN in PRIMITIVE_OUTCOMES[primitive]) == (
            primitive is Primitive.WRITER_STOP_PROOF
        )
    for (primitive, outcome), details in OUTCOME_DETAILS.items():
        assert outcome in PRIMITIVE_OUTCOMES[primitive] and details
    # The discriminated union routes a stored request back to its exact model.
    stored = WaitArgs(seconds=300, reason="ci_pending").model_dump(mode="json")
    assert adapter.validate_python(stored) == WaitArgs(seconds=300, reason="ci_pending")
    with pytest.raises(ValidationError):
        adapter.validate_python({"primitive": "rebind_repair"})


def test_outcome_sets_are_the_spec_table():
    assert PRIMITIVE_OUTCOMES[Primitive.GIT_PUBLISH] == {
        "published",
        "target_moved",
        "unknown_after_push",
    }
    assert PRIMITIVE_OUTCOMES[Primitive.CI_OBSERVE] == {s.value for s in CIState}
    assert PRIMITIVE_OUTCOMES[Primitive.WRITER_STOP_PROOF] == {
        "released",
        "preserved_and_released",
        "live",
        "unknown",
    }
    assert sum(len(outcomes) for outcomes in PRIMITIVE_OUTCOMES.values()) == 55


def test_shadow_may_only_observe_wait_and_record():
    assert {p for p in Primitive if not p.mutates} == {
        Primitive.OBSERVE_SUBJECT,
        Primitive.GIT_ANCESTRY,
        Primitive.RECORD_DECISION,
        Primitive.WAIT,
    }


def test_ports_wrap_existing_command_contracts_unchanged():
    """The ports add no command surface: each phase-1 adapter target still exists."""
    from src.commands.contracts.integration import DESIGN_INTEGRATION_COMMANDS

    adapted = {name for names in PHASE1_ADAPTED_COMMANDS.values() for name in names}
    assert adapted <= DESIGN_INTEGRATION_COMMANDS
    # No primitive name shadows an existing command except the seal it adapts.
    assert {p.value for p in Primitive} & DESIGN_INTEGRATION_COMMANDS == {"integration_seal"}


def test_outcomes_outside_the_closed_set_are_refused():
    with pytest.raises(ValidationError, match="has no outcome"):
        PrimitiveOutcome(primitive=Primitive.GIT_PUBLISH, outcome="busy")
    with pytest.raises(ValidationError, match="requires its reason"):
        PrimitiveOutcome(primitive=Primitive.GIT_PUBLISH, outcome=UNKNOWN)
    with pytest.raises(ValidationError, match=r"requires detail \['files', 'member'\]"):
        PrimitiveOutcome(primitive=Primitive.GIT_MERGE_MEMBERS, outcome="conflict")
    conflict = PrimitiveOutcome(
        primitive=Primitive.GIT_MERGE_MEMBERS,
        outcome="conflict",
        detail={"member": "t1", "files": ["a.py"]},
    )
    assert not conflict.is_unknown
    unknown = PrimitiveOutcome.unknown(Primitive.WRITER_FILE, "task_row_missing", task="t9")
    assert (unknown.outcome, unknown.reason, unknown.detail) == (
        UNKNOWN,
        "task_row_missing",
        {"task": "t9"},
    )


def test_gates_need_a_default_or_an_explicit_no_default():
    with pytest.raises(ValidationError, match="default choice and timeout, or no_default"):
        GateArgs(question="Repair exhausted?", choices=("eject", "hold"))
    with pytest.raises(ValidationError, match="one of the gate's choices"):
        GateArgs(question="q", choices=("eject",), default_choice="retry", default_after_seconds=60)
    with pytest.raises(ValidationError, match="carries no default"):
        GateArgs(question="q", choices=("hold",), no_default=True, default_after_seconds=60)
    assert GateArgs(question="q", choices=("hold",), no_default=True).no_default
    GateArgs(
        question="q", choices=("retry", "eject"), default_choice="retry", default_after_seconds=7200
    )


def test_publish_and_merge_arguments_carry_exact_identity():
    fence = Fence(target=BranchKey(repository_id="repo", branch="main"), owner_id="s", token=4)
    publish = PublishArgs(fence=fence, expected_old_sha=BASE, new_sha=HEAD)
    assert publish.require_green
    with pytest.raises(ValidationError):
        PublishArgs(fence=fence, expected_old_sha="main", new_sha=HEAD)
    with pytest.raises(ValidationError):
        MergeMembersArgs(target_ref="refs/heads/aq/x", base_sha=BASE, members=())
    MergeMembersArgs(
        target_ref="refs/heads/aq/x",
        base_sha=BASE,
        members=(MemberRef(task_id="t", head_sha=HEAD),),
    )


# ------------------------------------------------------------------- schedule


def test_every_schedule_is_progressing_waiting_held_or_done():
    progress = SubjectSchedule.progress(now=NOW, max_wait_seconds=600)
    assert (progress.state, progress.next_due_at) == (SubjectState.PROGRESSING, NOW)

    first = SubjectSchedule.backoff(
        now=NOW, max_wait_seconds=600, refusal_streak=0, base_seconds=30, ceiling_seconds=3600
    )
    assert (first.next_due_at, first.refusal_streak) == (NOW + 30, 1)
    late = SubjectSchedule.backoff(
        now=NOW, max_wait_seconds=600, refusal_streak=10, base_seconds=30, ceiling_seconds=3600
    )
    assert late.next_due_at == NOW + 600  # capped by the subject's bound

    wait = SubjectSchedule.wait(now=NOW, until=NOW + 300, reason="ci_pending", max_wait_seconds=600)
    assert (wait.state, wait.next_due_at) == (SubjectState.WAITING, NOW + 300)
    clamped = SubjectSchedule.wait(
        now=NOW, until=NOW + 86_400, reason="writer_filed", max_wait_seconds=600
    )
    assert clamped.next_due_at == NOW + 600

    held = SubjectSchedule.hold(now=NOW, gate_id="gate-1", max_wait_seconds=600)
    assert (held.state, held.next_due_at) == (SubjectState.HELD, None)
    defaulted = SubjectSchedule.hold(
        now=NOW, gate_id="gate-1", max_wait_seconds=600, revisit_at=NOW + 86_400
    )
    assert defaulted.next_due_at == NOW + 86_400  # a gate's default may exceed the bound

    closed = SubjectSchedule.close(now=NOW, reason="published", max_wait_seconds=600)
    assert closed.state is SubjectState.DONE


@pytest.mark.parametrize(
    "values, message",
    [
        ({"next_due_at": None}, "due time or an explicit gate"),
        ({"next_due_at": NOW + 601}, "exceeds the 600s bound"),
        ({"next_due_at": None, "wait_reason": "x"}, "must carry an until"),
        ({"closed_reason": "published"}, "no due time, wait or gate"),
    ],
)
def test_an_unbounded_or_incoherent_schedule_is_refused(values, message):
    fields = {"next_due_at": NOW, "due_set_at": NOW, "max_wait_seconds": 600, **values}
    with pytest.raises(ValidationError, match=message):
        SubjectSchedule(**fields)


# -------------------------------------------------------------------- subject


def test_subject_identity_rules():
    subject = _subject()
    assert subject.head == HeadIdentity(
        repository_id="repo", ref="refs/heads/main", sha=HEAD, generation=2, base_sha=BASE
    )
    assert subject.engine is SubjectEngine.LEGACY and subject.is_live
    with pytest.raises(ValidationError, match="not bound to a task"):
        _subject(task_id="t1")
    with pytest.raises(ValidationError, match="requires its task id"):
        _subject(kind=SubjectKind.SOURCE)
    with pytest.raises(ValidationError, match="only a root batch"):
        _subject(kind=SubjectKind.PARENT_EPISODE, task_id="epic", batch_id="batch-1")
    with pytest.raises(ValidationError, match="done exactly when"):
        _subject(phase=SubjectPhase.DONE)
    with pytest.raises(ValidationError, match="ref it is the head of"):
        _subject(target_ref=None)
    done = _subject(
        phase=SubjectPhase.DONE,
        schedule=SubjectSchedule.close(now=NOW, reason="published", max_wait_seconds=600),
    )
    assert done.state is SubjectState.DONE and not done.is_live


def test_wait_overdue_is_a_fact_of_waiting_subjects_only():
    waiting = _subject(
        schedule=SubjectSchedule.wait(now=NOW, until=NOW + 60, reason="ci", max_wait_seconds=600)
    )
    assert not waiting.wait_overdue(NOW + 60)
    assert waiting.wait_overdue(NOW + 61)
    assert not waiting.wait_overdue(NOW + 61, grace_seconds=30)
    assert not _subject().wait_overdue(NOW + 10_000)


def test_writer_and_budget_shapes():
    with pytest.raises(ValidationError, match="carries no task"):
        WriterLease(status=WriterStatus.NONE, task_id="t1")
    with pytest.raises(ValidationError, match="requires a task id"):
        WriterLease(status=WriterStatus.FILED)
    with pytest.raises(ValidationError, match="cannot precede"):
        WriterBudget(ordinal=0, intelligence_class="standard-high", started_at=10, deadline_at=5)
    budget = WriterBudget(
        ordinal=1,
        intelligence_class="deep-high",
        started_at=NOW,
        deadline_at=NOW + 60,
        attempts=2,
        attempt_limit=2,
    )
    assert budget.exhausted and budget.expired(NOW + 60) and not budget.expired(NOW + 59)


def test_subject_rows_round_trip():
    subject = _subject(
        kind=SubjectKind.PARENT_EPISODE,
        task_id="epic-1",
        subject_key=subject_key(SubjectKind.PARENT_EPISODE, "repo", "epic-1", 3),
        writer=WriterLease(
            status=WriterStatus.WORKING,
            task_id="repair-1",
            fence_token=7,
            session_id="s1",
            claimed_at=NOW,
            last_push_at=NOW + 5,
        ),
        budget=WriterBudget(
            ordinal=0,
            intelligence_class="standard-high",
            started_at=NOW,
            deadline_at=NOW + 5400,
            attempts=1,
            attempt_limit=2,
        ),
        version=4,
    )
    assert subject.subject_key == "parent_episode:repo:epic-1:3"
    assert Subject.from_row(subject.to_row()) == subject
    with pytest.raises(ValueError, match="non-empty part"):
        subject_key(SubjectKind.SOURCE, "repo")


# ---------------------------------------------------------------------- facts


def test_facts_bind_the_tested_head_and_digest_canonically():
    head = HeadIdentity(repository_id="repo", ref="refs/heads/aq/b", sha=HEAD, generation=0)
    candidate = HeadIdentity(
        repository_id="repo", ref="refs/heads/aq/integration/b", sha=OTHER, generation=1
    )
    facts = SubjectFacts(
        subject_id="subject-1",
        subject_version=3,
        kind=SubjectKind.ROOT_BATCH,
        phase=SubjectPhase.TESTING,
        observed_at=NOW,
        head=head,
        candidate=candidate,
        members=(MemberFacts(task_id="t1", head_sha=HEAD, review="approved"),),
        ci=(
            CIEvidence(head_sha=HEAD, state=CIState.RED),
            CIEvidence(head_sha=OTHER, state=CIState.GREEN),
        ),
        conflicts=(ConflictFacts(member_task_id="t2", files=("a.py",)),),
        writer=WriterLease(status=WriterStatus.FILED, task_id="repair-1"),
        budget=WriterBudget(
            ordinal=0,
            intelligence_class="standard-high",
            started_at=NOW - 90,
            deadline_at=NOW - 1,
        ),
    )
    assert facts.ci_state is CIState.GREEN  # judged on the candidate, not the source head
    binding = facts.binding()
    assert binding["ci_state"] == "green"
    assert binding["conflict_count"] == 1
    assert binding["writer_status"] == "filed"
    assert binding["budget_expired"] is True and binding["held"] is False
    assert facts.digest() == facts.model_copy().digest()
    assert facts.digest() != facts.model_copy(update={"base_moved": True}).digest()


def test_decisions_name_exactly_one_typed_primitive():
    facts_digest = "sha256:" + "f" * 64
    decision = Decision.model_validate(
        {
            "subject_id": "subject-1",
            "subject_version": 3,
            "policy": PIN.model_dump(),
            "rule": "root-batch/testing/pending-too-long",
            "facts_digest": facts_digest,
            "request": {
                "primitive": "ci_observe",
                "head": {
                    "repository_id": "repo",
                    "ref": "refs/heads/x",
                    "sha": HEAD,
                    "generation": 0,
                },
            },
        }
    )
    assert isinstance(decision.request, CIObserveArgs)
    assert decision.primitive is Primitive.CI_OBSERVE
    with pytest.raises(ValidationError):
        Decision(
            subject_id="s",
            subject_version=0,
            policy=PIN,
            rule="r",
            facts_digest=facts_digest,
            request={"primitive": "wait", "seconds": 0, "reason": "x"},
        )


# ---------------------------------------------------------------------- ports


async def test_ports_answer_closed_outcomes_or_unknown():
    subject = _subject()
    wait = WaitArgs(seconds=60, reason="ci_pending")

    ports = PrimitivePorts()
    unavailable = await ports.invoke(subject, wait)
    assert (unavailable.outcome, unavailable.reason) == (UNKNOWN, "primitive_unavailable")

    async def waiting(subject, args):
        return PrimitiveOutcome(primitive=Primitive.WAIT, outcome="waiting")

    async def wrong_primitive(subject, args):
        return PrimitiveOutcome(primitive=Primitive.WAIT, outcome="waiting")

    async def invalid(subject, args):
        return PrimitiveOutcome(primitive=Primitive.EJECT, outcome="waiting")

    ports = PrimitivePorts({Primitive.WAIT: waiting, Primitive.EJECT: invalid})
    assert (await ports.invoke(subject, wait)).outcome == "waiting"
    eject = PRIMITIVE_ARGS[Primitive.EJECT](member_task_id="t1", reason="unresolved")
    refused = await ports.invoke(subject, eject)
    assert refused.is_unknown and refused.reason.startswith("contract_violation")
    ports.bind(Primitive.GATE, wrong_primitive)
    gate = GateArgs(question="q", choices=("hold",), no_default=True)
    misrouted = await ports.invoke(subject, gate)
    assert misrouted.reason == "contract_violation: answered for wait"
    with pytest.raises(ValueError, match="already has an adapter"):
        ports.bind(Primitive.WAIT, waiting)
    assert ports.bound == {Primitive.WAIT, Primitive.EJECT, Primitive.GATE}


# ------------------------------------------------------------------- database


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("subjects"))
    await database.initialize()
    async with database._engine.begin() as conn:
        for sha in (ARTIFACT, NEW_ARTIFACT):
            await conn.execute(
                insert(playbook_artifacts).values(
                    artifact_sha256=sha,
                    playbook_id=PIN.playbook_id,
                    source_digest="sha256:" + "c" * 64,
                    contract_fingerprint="sha256:" + "d" * 64,
                    compiler_build="test",
                    path=f"/artifacts/{sha}.json",
                    created_at=1.0,
                )
            )
    yield database
    await database.close()


def _row(subject: Subject) -> dict:
    row = subject.to_row()
    for column in ("last_journal_seq", "version", "wake_requested_at", "last_visit_at"):
        row.pop(column)
    return row


async def _insert_raw(db, **overrides):
    row = {**_row(_subject()), **overrides}
    async with db._engine.begin() as conn:
        await conn.execute(insert(integration_subjects).values(**row))


async def test_creation_is_idempotent_and_never_overwrites(db):
    first, created = await db.ensure_integration_subject(_row(_subject()))
    assert created and first["version"] == 0 and first["engine"] == "legacy"
    again, created = await db.ensure_integration_subject(
        _row(_subject(id="subject-2", phase=SubjectPhase.TESTING))
    )
    assert not created and again["id"] == "subject-1" and again["phase"] == "building"
    assert Subject.from_row(again) == _subject()
    by_key = await db.get_integration_subject_by_key(
        project_id="p", kind="root_batch", subject_key=_subject().subject_key
    )
    assert by_key == again


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        # live, no gate, no due time
        ({"next_due_at": None}, "never_blocked"),
        # an unbounded wait
        ({"next_due_at": NOW + DEFAULT_MAX_WAIT_SECONDS + 1}, "never_blocked"),
        # a wait without its until (held, so the bound itself is satisfied)
        ({"wait_reason": "ci", "next_due_at": None, "gate_id": "gate-1"}, "wait_until"),
        # done but never closed, and closed but still due
        ({"phase": "done", "closed_reason": None, "next_due_at": None}, "never_blocked"),
        ({"phase": "done", "closed_reason": "published"}, "never_blocked"),
        ({"kind": "source"}, "identity"),  # a source without its task
        ({"head_sha": "main"}, "head"),  # not an exact head
        ({"writer_status": "claimed"}, "ck_integration_subjects_writer"),  # writer without task
        ({"budget_ordinal": 0}, "budget"),  # half a budget
    ],
)
async def test_schema_refuses_a_blocked_or_ambiguous_subject(db, overrides, constraint):
    with pytest.raises(IntegrityError, match=constraint):
        await _insert_raw(db, **overrides)


async def test_schema_admits_held_subjects_and_one_admitting_root(db):
    await _insert_raw(db, next_due_at=None, gate_id="gate-1")
    await _insert_raw(
        db,
        id="admit-1",
        subject_key="root_batch:repo:admission",
        phase="admitting",
        head_sha=None,
        base_sha=None,
        target_ref=None,
    )
    with pytest.raises(IntegrityError):
        await _insert_raw(
            db,
            id="admit-2",
            subject_key="root_batch:repo:admission-2",
            phase="admitting",
            head_sha=None,
            base_sha=None,
            target_ref=None,
        )


@pytest.mark.parametrize(
    "values, message",
    [
        ({"policy_artifact_sha256": NEW_ARTIFACT}, "policy artifact is pinned"),
        ({"kind": "source", "task_id": "t1"}, "identity is immutable"),
        ({"version": -1}, "cannot decrease"),
        ({"generation": 1}, "cannot decrease"),
    ],
)
async def test_identity_and_pinned_policy_never_change(db, values, message):
    await _insert_raw(db, version=3)
    with pytest.raises(DBAPIError, match=message):
        async with db._engine.begin() as conn:
            await conn.execute(update(integration_subjects).values(**values))


async def test_a_done_subject_never_reopens(db):
    closed = schedule_values(SubjectSchedule.close(now=NOW, reason="aborted", max_wait_seconds=60))
    await _insert_raw(db, phase="done", **closed)
    reopened = schedule_values(SubjectSchedule.progress(now=NOW, max_wait_seconds=60))
    with pytest.raises(DBAPIError, match="cannot reopen"):
        async with db._engine.begin() as conn:
            await conn.execute(update(integration_subjects).values(phase="building", **reopened))


async def test_due_pages_are_keyset_ordered_and_skip_done_and_future(db):
    for index, due in enumerate((NOW - 5, NOW - 5, NOW - 1, NOW + 10)):
        await _insert_raw(
            db,
            id=f"s{index}",
            subject_key=f"root_batch:repo:r{index}",
            next_due_at=due,
            due_set_at=due,
        )
    closed = schedule_values(
        SubjectSchedule.close(now=NOW, reason="published", max_wait_seconds=60)
    )
    await _insert_raw(db, id="s-done", subject_key="root_batch:repo:done", phase="done", **closed)
    await _insert_raw(
        db, id="s-held", subject_key="root_batch:repo:held", next_due_at=None, gate_id="g"
    )
    first = await db.due_integration_subject_page(now=NOW, after=None, limit=2)
    assert [row["id"] for row in first] == ["s0", "s1"]
    rest = await db.due_integration_subject_page(
        now=NOW, after=(first[-1]["next_due_at"], first[-1]["id"]), limit=10
    )
    assert [row["id"] for row in rest] == ["s2"]
    assert (
        await db.due_integration_subject_page(now=NOW, after=None, limit=10, kinds=["source"]) == []
    )
    assert (
        await db.due_integration_subject_page(now=NOW, after=None, limit=10, engine="reconciler")
        == []
    )
    with pytest.raises(ValueError):
        await db.due_integration_subject_page(now=NOW, after=None, limit=0)


async def test_runtime_mode_scope_filters_before_paging_shared_root_batches(db):
    from src.integration.reconciler import ScopedIntegrationDB

    async with db._engine.begin() as conn:
        await conn.execute(
            insert(tables.projects),
            [
                {
                    "id": mode, "name": mode, "hierarchical_integration_mode": mode,
                    "created_at": NOW,
                }
                for mode in ("train", "hierarchy", "development")
            ],
        )
    for index, mode in enumerate(("development", "train", "hierarchy", "development")):
        await _insert_raw(
            db,
            id=f"mode-{index}",
            project_id=mode,
            subject_key=f"root_batch:repo:mode-{index}",
            engine="reconciler",
            next_due_at=NOW - 10 + index,
            due_set_at=NOW - 10 + index,
        )
    root_db = ScopedIntegrationDB(db, ("train", "hierarchy"))
    first = await root_db.due_integration_subject_page(now=NOW, after=None, limit=1)
    assert [row["id"] for row in first] == ["mode-1"]
    second = await root_db.due_integration_subject_page(
        now=NOW, after=(first[0]["next_due_at"], first[0]["id"]), limit=1
    )
    assert [row["id"] for row in second] == ["mode-2"]
    development_db = ScopedIntegrationDB(db, ("development",))
    rows = await development_db.due_integration_subject_page(now=NOW, after=None, limit=10)
    assert [row["id"] for row in rows] == ["mode-0", "mode-3"]
    assert await root_db.get_integration_subject("mode-1") == first[0]


async def test_versioned_writes_refuse_a_stale_visit(db):
    await db.ensure_integration_subject(_row(_subject()))
    wait = schedule_values(
        SubjectSchedule.wait(now=NOW + 1, until=NOW + 301, reason="ci", max_wait_seconds=3600)
    )
    async with db._engine.begin() as conn:
        written = await db.update_integration_subject_on(
            conn,
            subject_id="subject-1",
            expected_version=0,
            values={"phase": "testing", **wait},
            now=NOW + 1,
        )
        stale = await db.update_integration_subject_on(
            conn,
            subject_id="subject-1",
            expected_version=0,
            values={"phase": "repairing"},
            now=NOW + 2,
        )
    assert (written["version"], written["phase"], written["next_due_at"]) == (
        1,
        "testing",
        NOW + 301,
    )
    assert stale is None
    with pytest.raises(ValueError, match="not writable"):
        async with db._engine.begin() as conn:
            await db.update_integration_subject_on(
                conn,
                subject_id="subject-1",
                expected_version=1,
                values={"policy_artifact_sha256": NEW_ARTIFACT},
                now=NOW,
            )


async def test_an_event_wakes_without_invalidating_the_visit_in_flight(db):
    await db.ensure_integration_subject(
        _row(
            _subject(
                schedule=SubjectSchedule.wait(
                    now=NOW, until=NOW + 600, reason="ci", max_wait_seconds=3600
                )
            )
        )
    )
    # The event pulls the due time forward, never back, and keeps the version.
    assert await db.wake_integration_subjects(now=NOW + 10, batch_ids=["nope"]) == 0
    assert await db.wake_integration_subjects(now=NOW + 10, subject_ids=["subject-1"]) == 1
    woken = await db.get_integration_subject("subject-1")
    assert (woken["next_due_at"], woken["wake_requested_at"], woken["version"]) == (
        NOW + 10,
        NOW + 10,
        0,
    )
    assert await db.wake_integration_subjects(now=NOW + 20, subject_ids=["subject-1"]) == 1
    assert (await db.get_integration_subject("subject-1"))["next_due_at"] == NOW + 10

    # A visit that started before the wake still writes, and stays due now.
    later = schedule_values(
        SubjectSchedule.wait(now=NOW + 30, until=NOW + 900, reason="ci", max_wait_seconds=3600)
    )
    async with db._engine.begin() as conn:
        racing = await db.update_integration_subject_on(
            conn,
            subject_id="subject-1",
            expected_version=0,
            values=later,
            now=NOW + 30,
            visit_started_at=NOW + 5,
        )
    assert (racing["version"], racing["next_due_at"]) == (1, NOW + 30)
    # One that started after it consumed the wake and keeps its own due time.
    async with db._engine.begin() as conn:
        settled = await db.update_integration_subject_on(
            conn,
            subject_id="subject-1",
            expected_version=1,
            values=later,
            now=NOW + 30,
            visit_started_at=NOW + 25,
        )
    assert settled["next_due_at"] == NOW + 900


async def test_wakes_reach_held_subjects_by_writer_task_and_gate(db):
    held = schedule_values(SubjectSchedule.hold(now=NOW, gate_id="gate-9", max_wait_seconds=60))
    writer = {"writer_status": "filed", "writer_task_id": "repair-1"}
    await db.ensure_integration_subject({**_row(_subject()), **held, **writer})
    assert await db.wake_integration_subjects(now=NOW + 5, gate_ids=["gate-9"]) == 1
    row = await db.get_integration_subject("subject-1")
    assert (row["gate_id"], row["next_due_at"]) == ("gate-9", NOW + 5)
    assert await db.wake_integration_subjects(now=NOW + 6, writer_task_ids=["repair-1"]) == 1


def _journal(**overrides) -> dict:
    values = {
        "subject_id": "subject-1",
        "entry_kind": "decision",
        "idempotency_key": "visit-1:decision",
        "visit_id": "visit-1",
        "mode": "shadow",
        "policy_artifact_sha256": ARTIFACT,
        "subject_version": 0,
        "phase": "building",
        "head_sha": HEAD,
        "generation": 2,
        "rule": "root-batch/building/merge",
        "primitive": "git_merge_members",
        "facts_digest": "sha256:" + "e" * 64,
        "payload": {"members": ["t1"]},
        "recorded_at": NOW,
    }
    values.update(overrides)
    return values


async def test_the_journal_is_replay_safe_and_append_only(db):
    await db.ensure_integration_subject(_row(_subject()))
    entry, created = await db.append_integration_subject_journal(_journal())
    replay, replayed = await db.append_integration_subject_journal(
        _journal(payload={"members": ["different"]})
    )
    assert created and not replayed and replay == entry
    action, _ = await db.append_integration_subject_journal(
        _journal(
            entry_kind="action",
            idempotency_key="visit-1:action",
            rule=None,
            facts_digest=None,
            outcome="merged",
            payload={"head": HEAD},
        )
    )
    assert (await db.get_integration_subject("subject-1"))["last_journal_seq"] == action["seq"]
    listed = await db.list_integration_subject_journal("subject-1")
    assert [row["entry_kind"] for row in listed] == ["decision", "action"]
    assert await db.list_integration_subject_journal(
        "subject-1", after_seq=entry["seq"], entry_kinds=["action"]
    ) == [action]

    with pytest.raises(IntegrityError):  # a decision names its rule and observation
        await db.append_integration_subject_journal(_journal(idempotency_key="k2", rule=None))
    with pytest.raises(IntegrityError):  # an attempt is for an exact head
        await db.append_integration_subject_journal(
            _journal(entry_kind="attempt", idempotency_key="k3", head_sha=None, outcome="counted")
        )
    for statement in (
        update(integration_subject_journal).values(outcome="rewritten"),
        integration_subject_journal.delete(),
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with db._engine.begin() as conn:
                await conn.execute(statement)


async def _drop_artifact_rows(db) -> None:
    """Delete every artifact row past its FKs, as the file collector's re-check sees it."""
    async with db._engine.connect() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(playbook_artifacts.delete())
        await conn.commit()


async def test_pinned_artifacts_are_protected_from_collection(db):
    await db.ensure_integration_subject(_row(_subject()))
    collected = await db.collect_playbook_artifacts(NOW, min_versions=0, limit=100)
    assert {sha for sha, _path in collected} == {NEW_ARTIFACT}
    async with db._engine.connect() as conn:
        remaining = set(
            (await conn.execute(select(playbook_artifacts.c.artifact_sha256))).scalars()
        )
    assert remaining == {ARTIFACT}
    await _drop_artifact_rows(db)
    assert await db.filter_referenced_artifact_shas([ARTIFACT, NEW_ARTIFACT]) == {ARTIFACT}


async def test_journal_entries_pin_the_artifact_they_ran_under(db):
    await db.ensure_integration_subject(_row(_subject()))
    await db.append_integration_subject_journal(_journal(policy_artifact_sha256=NEW_ARTIFACT))
    assert await db.collect_playbook_artifacts(NOW, min_versions=0, limit=100) == []
    await _drop_artifact_rows(db)
    assert await db.filter_referenced_artifact_shas([ARTIFACT, NEW_ARTIFACT]) == {
        ARTIFACT,
        NEW_ARTIFACT,
    }
