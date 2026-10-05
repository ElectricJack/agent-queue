"""Deterministic facts, unknowns and read-only ports for integration subjects."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select

from src.database import Database, tables as t
from src.git.manager import RemoteRefResult, RemoteRefState
from src.integration.models import Fence
from src.integration.observe import (
    DatabaseObservationReader,
    GitObservationReader,
    IntegrationObserver,
    ObservationRows,
)
from src.integration.subjects import (
    CIState,
    ObserveSubjectArgs,
    PolicyArtifactPin,
    Primitive,
    PrimitivePorts,
    RemoteHead,
    Subject,
    SubjectFacts,
    SubjectKind,
    SubjectSchedule,
    WriterBudget,
    WriterLease,
    WriterStatus,
)
from tests.db_fixtures import lease_dsn

NOW = 1000.0
BASE, HEAD, SOURCE, OTHER = (char * 40 for char in "abcd")
ARTIFACT = "sha256:" + "1" * 64
REQUIRED = {"producer_id": "15368", "version": "v1", "names": ["unit", "lint"]}
POLICY = {"root": {"required_checks": REQUIRED}, "parent": {"required_checks": REQUIRED}}


def subject(kind=SubjectKind.ROOT_BATCH, **overrides):
    values = dict(
        id="subject",
        project_id="p",
        repository_id="repo",
        kind=kind,
        subject_key=f"{kind}:repo:fixture",
        phase="testing",
        generation=2,
        task_id=None
        if kind == SubjectKind.ROOT_BATCH
        else "source"
        if kind == SubjectKind.SOURCE
        else "parent",
        batch_id="batch" if kind == SubjectKind.ROOT_BATCH else None,
        target_ref="refs/heads/aq/batch"
        if kind == SubjectKind.ROOT_BATCH
        else "refs/heads/aq/parent"
        if kind == SubjectKind.PARENT_EPISODE
        else "refs/heads/aq/source",
        head_sha=SOURCE if kind == SubjectKind.SOURCE else HEAD,
        base_sha=BASE,
        policy=PolicyArtifactPin(playbook_id="train", artifact_sha256=ARTIFACT),
        schedule=SubjectSchedule.progress(now=NOW, max_wait_seconds=3600),
        created_at=1.0,
        updated_at=1.0,
    )
    values.update(overrides)
    return Subject(**values)


def snapshot(kind=SubjectKind.ROOT_BATCH, **overrides):
    held_subject = subject(kind, **overrides)
    rows = {
        "tasks": (
            {
                "id": "source",
                "project_id": "p",
                "parent_task_id": "parent" if kind == SubjectKind.PARENT_EPISODE else None,
                "status": "COMPLETED",
            },
        ),
        "task_integration_checkpoints": (
            {
                "task_id": "source",
                "repository_id": "repo",
                "branch": "aq/source",
                "checkpoint_sha": SOURCE,
                "generation": 2,
            },
        ),
        "task_branch_origins": (
            {"task_id": "source", "branch_name": "aq/source", "base_sha": BASE},
        ),
        "integration_review_evidence": (
            {
                "id": "review",
                "source_task_id": "source",
                "repository_id": "repo",
                "reviewed_head_sha": SOURCE,
                "source_base": BASE,
                "generation": 2,
                "verdict": "approved",
                "created_at": 10.0,
            },
        ),
        "integration_parent_episodes": (
            {"id": "episode", "parent_task_id": "parent", "generation": 2},
        ),
    }
    if kind == SubjectKind.ROOT_BATCH:
        rows.update(
            {
                "integration_batches": (
                    {
                        "id": "batch",
                        "current_revision": 2,
                        "integration_branch": "aq/batch",
                        "policy_snapshot": POLICY,
                    },
                ),
                "integration_batch_members": (
                    {
                        "batch_id": "batch",
                        "ordinal": 0,
                        "task_id": "source",
                        "reviewed_head_sha": SOURCE,
                        "source_base_sha": BASE,
                        "source_ref": "refs/heads/aq/source",
                        "review_evidence_id": "review",
                        "review_evidence": {"generation": 2},
                    },
                ),
                "integration_candidate_revisions": (
                    {
                        "batch_id": "batch",
                        "revision": 2,
                        "head_sha": HEAD,
                        "construction_base_sha": BASE,
                        "ci_evidence_id": "ci",
                        "source_manifest": [{"task_id": "source"}],
                    },
                ),
            }
        )
    return ObservationRows(
        held_subject,
        {
            "id": "p",
            "status": "ACTIVE",
            "hierarchical_integration_generation": 1,
            "hierarchical_integration_policy": POLICY,
        },
        {
            "id": "repo",
            "default_branch": "main",
            "url": "https://example/repo.git",
            "checkout_base_path": "/checkout",
        },
        rows,
    )


def with_rows(data, **rows):
    return replace(data, rows={**data.rows, **{key: tuple(value) for key, value in rows.items()}})


class Reader:
    def __init__(self, data):
        self.data = data
        self.read = AsyncMock(return_value=data)


class Git:
    def __init__(self, heads=None, ancestry=None):
        self.heads = heads or {
            "refs/heads/main": BASE,
            "refs/heads/aq/batch": HEAD,
            "refs/heads/aq/parent": HEAD,
            "refs/heads/aq/source": SOURCE,
        }
        self.ancestry = ancestry or {
            (SOURCE, HEAD): True,
            (HEAD, SOURCE): False,
            (SOURCE, SOURCE): True,
        }
        self.calls = []

    async def remote_head(self, repository, ref):
        self.calls.append(("remote", repository["id"], ref))
        sha = self.heads.get(ref)
        return RemoteHead(ref=ref, state="present" if sha else "absent", sha=sha)

    async def is_ancestor(self, repository, ancestor, descendant):
        self.calls.append(("ancestry", repository["id"], ancestor, descendant))
        return self.ancestry.get((ancestor, descendant))


async def observe(data, git=None, **kwargs):
    async def session_probe(session):
        return session.get("state") in {"starting", "running", "draining"}

    return await IntegrationObserver(
        Reader(data), git or Git(), clock=lambda: NOW, session_probe=session_probe
    ).observe_subject(data.subject.id, **kwargs)


def ci_row(**overrides):
    values = dict(
        id="ci",
        batch_id="batch",
        candidate_revision=2,
        parent_task_id=None,
        parent_generation=None,
        parent_head_sha=None,
        producer_id="15368",
        required_check_version="v1",
        checks={"unit": "success", "lint": "success"},
        conclusion="success",
        classification="conclusive",
        observed_at=950.0,
    )
    values.update(overrides)
    return values


def source_ci(**overrides):
    values = dict(
        task_id="source",
        repository_id="repo",
        source_head=SOURCE,
        source_base=BASE,
        generation=2,
        policy_generation=1,
        state="green",
        observed_at=960.0,
        evidence={
            "head_sha": SOURCE,
            "producer_id": "15368",
            "required_checks_version": "v1",
            "required_checks": ["unit", "lint"],
            "checks": [{"name": name, "conclusion": "success"} for name in ("unit", "lint")],
        },
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize("kind", list(SubjectKind))
async def test_root_parent_and_source_facts_are_deterministic_without_mutations(kind):
    data = snapshot(kind)
    before = deepcopy(data)
    first, second = await observe(data), await observe(data)
    assert first == second and first.digest() == second.digest()
    assert data == before
    assert first.subject_id == "subject" and first.subject_version == 0
    assert first.members[0].review == "approved"
    assert first.members[0].ancestry == "contained"
    assert first.default_branch_head == BASE and not first.base_moved
    assert (first.candidate is not None) == (kind == SubjectKind.ROOT_BATCH)
    assert not first.holds and not first.unknown


async def test_root_members_keep_sealed_generation_and_report_ejected_members():
    data = snapshot()
    data = with_rows(
        data,
        task_integration_checkpoints=[
            {**data.all("task_integration_checkpoints")[0], "generation": 99}
        ],
        integration_candidate_revisions=[
            {**data.all("integration_candidate_revisions")[0], "source_manifest": []}
        ],
    )
    facts = await observe(data)
    assert facts.members[0].generation == 2 and facts.members[0].review == "approved"
    assert facts.members[0].ejected


async def test_parent_observes_uncheckpointed_children_and_current_episode_dispositions():
    data = snapshot(SubjectKind.PARENT_EPISODE)
    data = with_rows(
        data,
        tasks=[*data.all("tasks"), {"id": "new", "parent_task_id": "parent"}],
        integration_child_dispositions=[
            {"child_task_id": "source", "parent_episode_id": "episode", "disposition": "skipped"}
        ],
    )
    facts = await observe(data)
    assert [member.task_id for member in facts.members] == ["new", "source"]
    assert facts.members[0].head_sha is None and facts.members[1].ejected


@pytest.mark.parametrize(
    "conclusion,checks,expected",
    [
        ("success", {"unit": "success", "lint": "success"}, CIState.GREEN),
        ("failure", {"unit": "failure", "lint": "success"}, CIState.RED),
        ("pending", {"unit": "missing", "lint": "success"}, CIState.PENDING),
        ("cancelled", {"unit": "cancelled", "lint": "success"}, CIState.INFRA),
        ("inconclusive", {"unit": "missing", "lint": "success"}, CIState.INFRA),
        ("success", {"unit": "success"}, CIState.UNTRUSTED),
    ],
)
async def test_ci_is_exact_and_classifies_trusted_complete_checks(conclusion, checks, expected):
    data = with_rows(
        snapshot(), integration_check_evidence=[ci_row(conclusion=conclusion, checks=checks)]
    )
    facts = await observe(data)
    assert facts.ci_state == expected
    assert facts.ci[0].evidence_id == "ci" and facts.ci[0].age_seconds == 50


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"candidate_revision": 1}, CIState.NONE),
        ({"batch_id": "foreign"}, CIState.NONE),
        ({"producer_id": "foreign"}, CIState.UNTRUSTED),
        ({"required_check_version": "v0"}, CIState.UNTRUSTED),
        ({"classification": "fabricated"}, CIState.UNTRUSTED),
        ({"id": "unbound-green"}, CIState.UNTRUSTED),
    ],
)
async def test_stale_and_untrusted_green_cannot_green_the_candidate(change, expected):
    facts = await observe(with_rows(snapshot(), integration_check_evidence=[ci_row(**change)]))
    assert facts.ci_state == expected


async def test_newer_pending_rerun_overrides_old_green_and_aggregate_wins_timestamp_tie():
    data = with_rows(
        snapshot(),
        integration_check_evidence=[
            ci_row(),
            ci_row(id="rerun", observed_at=970, conclusion="pending"),
        ],
    )
    assert (await observe(data)).ci_state == CIState.PENDING
    data = with_rows(
        data,
        integration_check_evidence=[ci_row(), ci_row(id="z-partial", checks={"unit": "success"})],
    )
    assert (await observe(data)).ci_state == CIState.GREEN


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, CIState.GREEN),
        ({"source_head": OTHER}, CIState.NONE),
        ({"generation": 1}, CIState.NONE),
        ({"source_base": OTHER}, CIState.NONE),
        ({"policy_generation": 0}, CIState.UNTRUSTED),
    ],
)
async def test_source_ci_is_pinned_to_head_base_generation_and_policy(change, expected):
    facts = await observe(
        with_rows(snapshot(SubjectKind.SOURCE), integration_source_ci=[source_ci(**change)])
    )
    assert facts.ci_state == expected


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, CIState.GREEN),
        ({"parent_head_sha": OTHER}, CIState.NONE),
        ({"parent_generation": 1}, CIState.NONE),
        ({"parent_task_id": "other-parent"}, CIState.NONE),
    ],
)
async def test_parent_ci_matches_its_exact_head_and_episode_generation(change, expected):
    evidence = (
        ci_row(
            batch_id=None,
            candidate_revision=None,
            parent_task_id="parent",
            parent_generation=2,
            parent_head_sha=HEAD,
            **change,
        )
        if not change
        else {
            **ci_row(
                batch_id=None,
                candidate_revision=None,
                parent_task_id="parent",
                parent_generation=2,
                parent_head_sha=HEAD,
            ),
            **change,
        }
    )
    facts = await observe(
        with_rows(snapshot(SubjectKind.PARENT_EPISODE), integration_check_evidence=[evidence])
    )
    assert facts.ci_state == expected


async def test_explicit_holds_and_exact_head_rejections_remain_binding():
    data = snapshot()
    data = replace(data, project={**data.project, "status": "PAUSED"})
    data = with_rows(
        data,
        task_metadata=[{"task_id": "source", "key": "manual_pause", "value": "{}"}],
        integration_review_evidence=[
            *data.all("integration_review_evidence"),
            {
                **data.all("integration_review_evidence")[0],
                "id": "rejected",
                "verdict": "rejected",
                "created_at": 20,
            },
            {
                **data.all("integration_review_evidence")[0],
                "id": "other-head",
                "reviewed_head_sha": OTHER,
                "created_at": 30,
            },
        ],
        integration_batches=[
            {**data.all("integration_batches")[0], "human_abort_reason": "operator abort"}
        ],
    )
    facts = await observe(data)
    assert facts.members[0].held and facts.members[0].review == "rejected"
    assert {hold.kind for hold in facts.holds} == {
        "manual_pause",
        "project_inactive",
        "review_rejected",
        "operator_hold",
    }
    assert facts.binding()["held"]


def unsealed_root(**changes):
    """An admitting root whose frontier is the project's checkpointed root tasks."""
    data = snapshot(
        batch_id=None,
        phase="admitting",
        target_ref="refs/heads/main",
        head_sha=None,
        base_sha=None,
        generation=0,
    )
    rows = {
        key: value
        for key, value in data.rows.items()
        if not key.startswith(("integration_batch", "integration_candidate"))
    }
    data = replace(data, rows=rows)
    for key, value in changes.items():
        data = with_rows(data, **{key: value})
    return data


async def test_an_unsealed_root_frontier_hold_or_rejection_does_not_hold_the_train():
    review = snapshot().all("integration_review_evidence")[0]
    data = unsealed_root(
        task_metadata=[{"task_id": "source", "key": "manual_pause", "value": "{}"}],
        integration_review_evidence=[review, {**review, "id": "no", "verdict": "rejected",
                                              "created_at": 20}],
    )
    facts = await observe(data, Git(ancestry={(SOURCE, BASE): False, (BASE, SOURCE): True}))
    # The frontier task stays visible (and excluded by the seal's admission),
    # but only a sealed member or the subject's own task binds the subject.
    assert facts.members[0].held and facts.members[0].review == "rejected"
    assert facts.holds == () and not facts.binding()["held"]
    paused = replace(data, project={**data.project, "status": "PAUSED"})
    assert {hold.kind for hold in (await observe(paused)).holds} == {"project_inactive"}


async def test_unconstructed_members_relate_to_the_publication_targets_observed_tip():
    data = unsealed_root()
    facts = await observe(data, Git(ancestry={(SOURCE, BASE): False, (BASE, SOURCE): True}))
    assert facts.candidate is None and facts.head is None
    assert facts.members[0].ancestry == "ahead" and facts.unknown == ()
    # An absent or unread target tip is still not an ancestry fact.
    unread = await observe(data, Git(heads={"refs/heads/aq/source": SOURCE}))
    assert unread.members[0].ancestry == "unknown"
    assert "ancestry_unknown:source" in unread.unknown


@pytest.mark.parametrize(
    "status,answer,held",
    [
        ("open", None, True),
        ("resolved", "hold", True),
        ("resolved", "retry", False),
        ("expired", None, False),
    ],
)
async def test_gate_facts_preserve_answer_and_explicit_hold(status, answer, held):
    data = snapshot(schedule=SubjectSchedule.hold(now=NOW, max_wait_seconds=3600, gate_id="gate"))
    data = with_rows(
        data,
        gates=[
            {
                "id": "gate",
                "status": status,
                "resolution": answer,
                "question": "continue?",
                "timeout_at": None,
            }
        ],
    )
    facts = await observe(data)
    assert facts.gate.answer == answer and facts.gate.no_default
    assert bool(facts.holds) == held
    assert facts.gate.status == ("answered" if status == "resolved" else status)


async def test_missing_gate_and_overdue_wait_are_facts_without_rescheduling():
    held = snapshot(
        schedule=SubjectSchedule.hold(now=900, max_wait_seconds=3600, gate_id="missing")
    )
    facts = await observe(held)
    assert facts.gate.status == "missing" and "gate_missing:missing" in facts.unknown
    waiting = snapshot(
        schedule=SubjectSchedule.wait(now=800, until=900, reason="ci", max_wait_seconds=3600)
    )
    assert (await observe(waiting)).wait_overdue
    assert waiting.subject.schedule.next_due_at == 900


async def test_unknown_remote_is_not_absence_or_a_moved_base_and_skip_makes_no_git_calls():
    class Unavailable(Git):
        async def remote_head(self, repository, ref):
            raise ConnectionError("do not expose exception detail")

    facts = await observe(snapshot(), Unavailable())
    assert all(head.state == "unknown" for head in facts.remote_heads)
    assert facts.default_branch_head is None and not facts.base_moved
    git = Git()
    facts = await observe(snapshot(), git, include_remote=False)
    assert not git.calls and facts.members[0].ancestry == "unknown"
    moved = await observe(snapshot(), Git(heads={"refs/heads/main": OTHER}))
    assert moved.base_moved
    assert (
        next(head for head in moved.remote_heads if head.ref == "refs/heads/aq/batch").state
        == "absent"
    )


@pytest.mark.parametrize(
    "contained,ahead,expected",
    [
        (True, None, "contained"),
        (False, True, "ahead"),
        (False, False, "diverged"),
        (None, False, "unknown"),
    ],
)
async def test_ancestry_does_not_turn_probe_failure_into_divergence(contained, ahead, expected):
    facts = await observe(
        snapshot(), Git(ancestry={(SOURCE, HEAD): contained, (HEAD, SOURCE): ahead})
    )
    assert facts.members[0].ancestry == expected


def writer_snapshot(status="READY", sessions=(), proof=None, locked=False, pushed=False):
    data = snapshot(
        writer=WriterLease(
            status="filed", task_id="repair", stop_proof=proof, last_push_at=990 if pushed else None
        ),
        budget=WriterBudget(
            ordinal=1,
            intelligence_class="standard-high",
            started_at=800,
            deadline_at=950,
            attempts=2,
            attempt_limit=2,
        ),
    )
    return with_rows(
        data,
        tasks=[*data.all("tasks"), {"id": "repair", "status": status}],
        sessions=sessions,
        workspaces=[{"locked_by_task_id": "repair"}] if locked else [],
    )


@pytest.mark.parametrize(
    "status,sessions,proof,locked,pushed,expected",
    [
        ("READY", [], None, False, False, WriterStatus.FILED),
        (
            "IN_PROGRESS",
            [{"id": "session", "task_id": "repair", "state": "running", "started_at": 900}],
            None,
            False,
            False,
            WriterStatus.CLAIMED,
        ),
        (
            "IN_PROGRESS",
            [{"id": "session", "task_id": "repair", "state": "running", "started_at": 900}],
            None,
            False,
            True,
            WriterStatus.WORKING,
        ),
        (
            "COMPLETED",
            [{"id": "session", "task_id": "repair", "state": "stopped"}],
            None,
            False,
            False,
            WriterStatus.UNKNOWN,
        ),
        (
            "COMPLETED",
            [],
            {"stop_proof": {"confirmed_at": 990}, "preserved_sha": OTHER},
            False,
            False,
            WriterStatus.STOPPED,
        ),
        ("COMPLETED", [], {"stop_proof": {"confirmed_at": 990}}, True, False, WriterStatus.UNKNOWN),
        ("COMPLETED", [], {"reason": "task closed"}, False, False, WriterStatus.UNKNOWN),
        (
            "COMPLETED",
            [{"id": "session", "task_id": "repair", "state": "running", "started_at": 900}],
            {"stop_proof": {"confirmed_at": 850}},
            False,
            False,
            WriterStatus.CLAIMED,
        ),
    ],
)
async def test_writer_filed_claimed_working_stopped_and_unknown_require_evidence(
    status, sessions, proof, locked, pushed, expected
):
    facts = await observe(writer_snapshot(status, sessions, proof, locked, pushed))
    assert facts.writer.status == expected
    assert facts.budget.attempts == 2
    assert facts.binding()["budget_expired"] and facts.binding()["budget_exhausted"]
    if expected == WriterStatus.UNKNOWN:
        assert "writer_stop_unproven:repair" in facts.unknown


async def test_existing_owner_recovery_proof_is_reused_but_live_writer_takes_precedence():
    data = writer_snapshot("COMPLETED")
    audit = {
        "id": "audit",
        "task_id": "repair",
        "ref": "aq/batch",
        "outcome": "preserved_and_released",
        "owner_row_id": "owner",
        "created_at": 980,
        "evidence": {"stop_proof": {"session_id": "gone"}, "preserved_sha": OTHER},
    }
    data = with_rows(data, integration_owner_recoveries=[audit])
    assert (await observe(data)).writer.stop_proof == audit["evidence"]
    assert (await observe(data)).writer.status == WriterStatus.STOPPED
    data = with_rows(
        data, sessions=[{"id": "live", "task_id": "repair", "state": "running", "started_at": 990}]
    )
    facts = await observe(data)
    assert facts.writer.status == WriterStatus.CLAIMED and facts.writer.stop_proof is None


async def test_stored_running_state_and_failed_runtime_probe_do_not_prove_liveness():
    data = writer_snapshot(
        "IN_PROGRESS", [{"id": "live", "task_id": "repair", "state": "running", "started_at": 900}]
    )
    observer = IntegrationObserver(Reader(data), Git(), clock=lambda: NOW)
    facts = await observer.observe_subject("subject")
    assert facts.writer.status == WriterStatus.UNKNOWN
    assert "writer_liveness_unknown:repair" in facts.unknown
    observer.session_probe = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    assert (await observer.observe_subject("subject")).writer.status == WriterStatus.UNKNOWN
    observer.session_probe = AsyncMock(return_value=False)
    assert (await observer.observe_subject("subject")).writer.status == WriterStatus.UNKNOWN


async def test_old_stop_receipt_cannot_clear_a_later_writer_session():
    data = writer_snapshot(
        "COMPLETED",
        [{"id": "later", "task_id": "repair", "state": "stopped", "started_at": 995}],
        {"stop_proof": {"confirmed_at": 990}, "preserved_sha": OTHER},
    )
    assert (await observe(data)).writer.status == WriterStatus.UNKNOWN


@pytest.mark.parametrize("pushed_at", [None, 920.0])
async def test_publication_receipt_proves_working_without_fabricating_push_time(pushed_at):
    data = writer_snapshot(
        "IN_PROGRESS", [{"id": "live", "task_id": "repair", "state": "running", "started_at": 900}]
    )
    receipt = {"remote_sha": HEAD}
    if pushed_at is not None:
        receipt["pushed_at"] = pushed_at
    data = with_rows(
        data,
        integration_candidate_resolutions=[
            {"repair_task_id": "repair", "push_evidence": receipt, "updated_at": 999}
        ],
    )
    facts = await observe(data)
    assert facts.writer.status == WriterStatus.WORKING
    assert facts.writer.last_push_at == pushed_at


def claimed_candidate_writer():
    data = writer_snapshot(
        "IN_PROGRESS", [{"id": "live", "task_id": "repair", "state": "running", "started_at": 900}]
    )
    return with_rows(
        data,
        integration_repair_operations=[{"id": "op", "active_stage": 1, "created_at": 800}],
        integration_repair_stages=[
            {
                "operation_id": "op",
                "ordinal": 1,
                "repair_task_id": "repair",
                "starting_sha": BASE,
            }
        ],
        integration_branch_owners=[
            {
                "id": "owner",
                "owner_id": "repair",
                "ref": "aq/batch",
                "fence_token": 4,
                "session_id": "live",
                "handoff_state": "attached",
                "updated_at": 900,
            }
        ],
    )


def builder_mutation(**overrides):
    return {
        "id": "builder",
        "batch_id": "batch",
        "repository_id": "repo",
        "revision": 2,
        "purpose": "candidate_partial",
        "target_branch": "aq/batch",
        "state": "applied",
        "desired_sha": HEAD,
        "remote_sha": HEAD,
        "created_at": 850,
        "prewrite_at": 860,
        "updated_at": 870,
        **overrides,
    }


@pytest.mark.parametrize("state", ["applied", "reserved"])
@pytest.mark.parametrize("purpose", ["candidate_partial", "candidate_final", "repair_handoff"])
async def test_builder_publication_does_not_prove_a_claimed_writer_pushed(state, purpose):
    data = with_rows(
        claimed_candidate_writer(),
        integration_candidate_ref_mutations=[
            builder_mutation(
                state=state, purpose=purpose, remote_sha=HEAD if state == "applied" else None
            )
        ],
    )
    git = Git(ancestry={(SOURCE, HEAD): True, (HEAD, SOURCE): False, (BASE, HEAD): True})
    before = deepcopy(data)
    facts = await observe(data, git)
    assert facts.writer.status is WriterStatus.CLAIMED
    assert facts.writer.last_push_at is None and facts.budget.attempts == 2
    assert ("ancestry", "repo", BASE, HEAD) not in git.calls
    assert data == before


@pytest.mark.parametrize(
    "advanced,expected",
    [(True, WriterStatus.WORKING), (False, WriterStatus.CLAIMED), (None, WriterStatus.CLAIMED)],
)
async def test_writer_must_advance_beyond_builder_publication(advanced, expected):
    data = with_rows(
        claimed_candidate_writer(),
        integration_candidate_ref_mutations=[builder_mutation()],
    )
    git = Git(
        heads={"refs/heads/main": BASE, "refs/heads/aq/batch": OTHER, "refs/heads/aq/source": SOURCE},
        ancestry={
            (SOURCE, HEAD): True,
            (HEAD, SOURCE): False,
            (BASE, OTHER): True,
            (HEAD, OTHER): advanced,
        },
    )
    facts = await observe(data, git)
    assert facts.writer.status is expected and facts.writer.last_push_at is None
    assert ("ancestry", "repo", HEAD, OTHER) in git.calls
    assert ("ancestry", "repo", BASE, OTHER) not in git.calls
    assert ("writer_push_ancestry_unknown:repair" in facts.unknown) == (advanced is None)


@pytest.mark.parametrize(
    "change",
    [
        {"batch_id": "other-batch"},
        {"repository_id": "other-repo"},
        {"revision": 1},
        {"target_branch": "aq/other"},
        {"purpose": "repair_resolution"},
        {"purpose": "root_main"},
        {"state": "superseded"},
        {"state": "reserved", "prewrite_at": None},
    ],
)
async def test_writer_push_baseline_ignores_unrelated_or_unwritten_mutations(change):
    data = with_rows(
        claimed_candidate_writer(),
        integration_candidate_ref_mutations=[
            builder_mutation(),
            builder_mutation(id="unrelated", desired_sha=OTHER, created_at=895, **change),
        ],
    )
    git = Git(
        heads={"refs/heads/main": BASE, "refs/heads/aq/batch": OTHER, "refs/heads/aq/source": SOURCE},
        ancestry={(SOURCE, HEAD): True, (HEAD, SOURCE): False, (HEAD, OTHER): True},
    )
    assert (await observe(data, git)).writer.status is WriterStatus.WORKING


async def test_writer_push_baseline_uses_last_builder_write_not_a_late_reconciliation():
    data = with_rows(
        claimed_candidate_writer(),
        integration_candidate_ref_mutations=[
            builder_mutation(id="old", desired_sha=SOURCE, created_at=810, updated_at=999),
            builder_mutation(target_branch="refs/heads/aq/batch"),
        ],
    )
    git = Git(ancestry={(SOURCE, HEAD): True, (HEAD, SOURCE): False, (BASE, HEAD): True})
    assert (await observe(data, git)).writer.status is WriterStatus.CLAIMED


async def test_current_operation_budget_conflicts_and_proven_writer_push():
    data = writer_snapshot(
        "IN_PROGRESS", [{"id": "live", "task_id": "repair", "state": "running", "started_at": 900}]
    )
    data = replace(data, subject=data.subject.model_copy(update={"budget": None}))
    data = with_rows(
        data,
        integration_repair_operations=[{"id": "op", "active_stage": 1, "created_at": 800}],
        integration_repair_stages=[
            {
                "operation_id": "op",
                "ordinal": 1,
                "repair_task_id": "repair",
                "starting_sha": BASE,
                "intelligence_class": "deep-high",
                "started_at": 800,
                "deadline_at": 950,
                "attempts": 1,
                "dossier": {
                    "budget": {"attempt_limit": 2},
                    "supervisor_recovery": {
                        "reason": "unchanged",
                        "subject": {"candidate_sha": HEAD},
                        "attempts": 1,
                    },
                },
            }
        ],
        integration_branch_owners=[
            {
                "id": "owner",
                "owner_id": "repair",
                "ref": "aq/batch",
                "fence_token": 4,
                "session_id": "live",
                "handoff_state": "attached",
                "updated_at": 900,
            }
        ],
        integration_candidate_member_results=[
            {
                "revision": 2,
                "member_ordinal": 0,
                "result": "conflict",
                "conflict_evidence": {"files": ["z.py", "a.py"]},
            }
        ],
    )
    git = Git(ancestry={(SOURCE, HEAD): True, (HEAD, SOURCE): False, (BASE, HEAD): True})
    facts = await observe(data, git)
    assert facts.writer.status == WriterStatus.WORKING and facts.writer.last_push_at is None
    assert facts.writer.fence_token == 4 and facts.budget.intelligence_class == "deep-high"
    assert facts.budget.attempts == 1 and facts.no_progress and not facts.ladder_exhausted
    assert facts.conflicts[0].files == ("a.py", "z.py")


async def test_journals_keep_only_unresolved_writes_and_ci_requests_are_pending():
    data = snapshot()
    entries = [
        dict(
            seq=1,
            primitive="git_publish",
            mode="active",
            idempotency_key="first",
            outcome="unknown_after_push",
            recorded_at=900,
            head_sha=HEAD,
            generation=2,
            payload={
                "journal_id": "write",
                "target_ref": "refs/heads/main",
                "expected_old_sha": BASE,
            },
        ),
        dict(
            seq=2,
            primitive="ci_request",
            mode="active",
            idempotency_key="request",
            outcome="requested",
            recorded_at=910,
            head_sha=HEAD,
            generation=2,
            payload={},
        ),
    ]
    data = with_rows(
        data,
        integration_subject_journal=entries,
        integration_promotion_intents=[
            {
                "id": "intent",
                "state": "pushed",
                "target_branch": "main",
                "expected_target": BASE,
                "prepared_sha": HEAD,
                "created_at": 880,
            },
            {"id": "done", "state": "committed"},
        ],
        integration_candidate_ref_mutations=[
            {
                "id": "mutation",
                "state": "reserved",
                "purpose": "candidate",
                "target_branch": "aq/batch",
                "expected_old_sha": BASE,
                "desired_sha": HEAD,
                "prewrite_at": 890,
            }
        ],
    )
    facts = await observe(data)
    assert [write.journal_id for write in facts.unresolved_writes] == [
        "intent",
        "mutation",
        "write",
    ]
    assert facts.ci_state == CIState.PENDING and facts.ci[0].requested_at == 910
    data = with_rows(
        data,
        integration_subject_journal=[*entries, {**entries[0], "seq": 3, "outcome": "published"}],
    )
    assert [write.journal_id for write in (await observe(data)).unresolved_writes] == [
        "intent",
        "mutation",
    ]


async def test_primitive_port_handles_not_found_and_unavailable_snapshot():
    data = snapshot()
    reader = Reader(data)
    ports = PrimitivePorts(
        {Primitive.OBSERVE_SUBJECT: IntegrationObserver(reader, Git(), clock=lambda: NOW)}
    )
    result = await ports.invoke(data.subject, ObserveSubjectArgs())
    assert result.outcome == "observed" and result.detail["facts"]["subject_id"] == "subject"
    assert SubjectFacts.model_validate(result.detail["facts"]).binding()["ci_state"] == "none"
    reader.read.return_value = None
    assert (await ports.invoke(data.subject, ObserveSubjectArgs())).outcome == "not_found"
    reader.read.side_effect = ConnectionError("private database detail")
    result = await ports.invoke(data.subject, ObserveSubjectArgs())
    assert result.is_unknown and result.reason == "snapshot_unavailable"


async def test_reconciler_callable_observes_current_subject_and_reports_disappearance():
    data = snapshot()
    reader = Reader(data)
    observer = IntegrationObserver(reader, Git(), clock=lambda: NOW)
    facts = await observer.observe(data.subject)
    assert isinstance(facts, SubjectFacts)
    assert facts.subject_id == data.subject.id and facts.subject_version == data.subject.version
    reader.read.return_value = replace(data, subject=data.subject.model_copy(update={"version": 1}))
    assert (await observer.observe(data.subject)).subject_version == 1
    reader.read.return_value = None
    with pytest.raises(LookupError, match="integration subject not found"):
        await observer.observe(data.subject)


class PublisherFacts(SubjectFacts):
    publisher_fence: Fence | None = None


def publisher_owner(**overrides):
    row = dict(
        id="publisher",
        repository_id="repo",
        ref="refs/heads/main",
        owner_role="publisher",
        owner_id="repo-publisher",
        fence_token=5,
        handoff_state="reserved",
        expires_at=1100,
    )
    row.update(overrides)
    return row


async def test_publisher_fence_is_a_separate_observed_typed_extension():
    data = with_rows(snapshot(), integration_branch_owners=[publisher_owner()])
    before = deepcopy(data)
    observer = IntegrationObserver(
        Reader(data), Git(), clock=lambda: NOW, facts_type=PublisherFacts
    )
    facts = await observer.observe(data.subject)
    assert isinstance(facts, PublisherFacts) and isinstance(facts, SubjectFacts)
    assert facts.publisher_fence.target.branch == "refs/heads/main"
    assert facts.publisher_fence.owner_id == "repo-publisher" and facts.publisher_fence.token == 5
    assert facts.writer.status == WriterStatus.NONE
    assert facts.binding()["publisher_fence"]["token"] == 5
    result = await observer(data.subject, ObserveSubjectArgs())
    assert PublisherFacts.model_validate(result.detail["facts"]) == facts
    assert data == before


@pytest.mark.parametrize(
    "change",
    [
        {"repository_id": "other-repo"},
        {"ref": "refs/heads/aq/batch"},
        {"owner_role": "repair"},
        {"owner_role": "worker"},
        {"handoff_state": "released"},
        {"handoff_state": "attached"},
        {"handoff_state": "handoff_pending"},
        {"expires_at": NOW},
        {"expires_at": NOW - 1},
        {"fence_token": -1},
        {"owner_role": "collector", "owner_id": "foreign-batch"},
    ],
)
async def test_publication_authority_is_never_fabricated_from_wrong_or_stale_owner(change):
    data = with_rows(snapshot(), integration_branch_owners=[publisher_owner(**change)])
    facts = await IntegrationObserver(
        Reader(data), Git(), clock=lambda: NOW, facts_type=PublisherFacts
    ).observe(data.subject)
    assert facts.publisher_fence is None
    assert "publisher_fence_unavailable" in facts.unknown


async def test_missing_publisher_authority_stays_absent_and_collector_belongs_to_subject():
    data = snapshot()
    observer = IntegrationObserver(
        Reader(data), Git(), clock=lambda: NOW, facts_type=PublisherFacts
    )
    assert (await observer.observe(data.subject)).publisher_fence is None
    observer.reader = Reader(
        with_rows(
            data,
            integration_branch_owners=[publisher_owner(owner_role="collector", owner_id="batch")],
        )
    )
    assert (await observer.observe(data.subject)).publisher_fence.owner_id == "batch"
    plain = await IntegrationObserver(observer.reader, Git(), clock=lambda: NOW).observe(
        data.subject
    )
    assert type(plain) is SubjectFacts  # no shared contract edits


async def test_git_adapter_uses_authorized_repository_and_strict_reads_only():
    git = AsyncMock()
    git.als_remote_ref.return_value = RemoteRefResult(RemoteRefState.PRESENT, oid=HEAD)
    git.ais_ancestor.return_value = None
    adapter = GitObservationReader(git)
    repository = snapshot().repository
    assert (await adapter.remote_head(repository, "refs/heads/main")).sha == HEAD
    git.als_remote_ref.assert_awaited_once_with(
        "/checkout", "main", repository_url=repository["url"]
    )
    assert await adapter.is_ancestor(repository, BASE, HEAD) is None
    git.ais_ancestor.assert_awaited_once_with("/checkout", BASE, HEAD, strict=True)
    git.afetch_origin.assert_not_called()
    git.apush_branch.assert_not_called()


async def test_git_adapter_reads_remote_heads_in_batched_chunks(monkeypatch):
    from src.integration import observe as observe_module

    monkeypatch.setattr(observe_module, "_REMOTE_REF_CHUNK", 2)

    async def read(path, branches, *, repository_url):
        if "boom" in branches:
            raise ConnectionError("do not expose exception detail")
        return {
            branch: RemoteRefResult(RemoteRefState.ABSENT)
            if branch == "gone"
            else RemoteRefResult(RemoteRefState.PRESENT, oid=HEAD)
            for branch in branches
        }

    git = AsyncMock()
    git.als_remote_refs.side_effect = read
    repository = snapshot().repository
    refs = [
        "refs/heads/main",
        "refs/heads/gone",
        "refs/heads/-bad",
        "refs/tags/v1",
        "refs/heads/a",
        "refs/heads/boom",
    ]
    heads = await GitObservationReader(git).remote_heads(repository, refs)
    assert {ref: (head.ref, head.state) for ref, head in heads.items()} == {
        "refs/heads/main": ("refs/heads/main", "present"),
        "refs/heads/gone": ("refs/heads/gone", "absent"),
        "refs/heads/-bad": ("refs/heads/-bad", "unknown"),
        "refs/tags/v1": ("refs/tags/v1", "unknown"),
        "refs/heads/a": ("refs/heads/a", "unknown"),
        "refs/heads/boom": ("refs/heads/boom", "unknown"),
    }
    assert heads["refs/heads/main"].sha == HEAD
    assert [call.args for call in git.als_remote_refs.await_args_list] == [
        ("/checkout", ["main", "gone"]),
        ("/checkout", ["a", "boom"]),
    ]
    assert {call.kwargs["repository_url"] for call in git.als_remote_refs.await_args_list} == {
        repository["url"]
    }
    git.als_remote_ref.assert_not_called()


async def test_observer_reads_every_remote_head_in_one_batched_port_call():
    class Batched(Git):
        async def remote_head(self, repository, ref):
            raise AssertionError("the batched read covers every ref")

        async def remote_heads(self, repository, refs):
            self.calls.append(("remote_heads", repository["id"], tuple(refs)))
            heads = {
                ref: RemoteHead(
                    ref=ref,
                    state="present" if self.heads.get(ref) else "absent",
                    sha=self.heads.get(ref),
                )
                for ref in refs
            }
            heads["refs/heads/aq/source"] = RemoteHead(
                ref="refs/heads/aq/elsewhere", state="present", sha=OTHER
            )
            return heads

    git = Batched()
    facts = await observe(snapshot(), git)
    assert git.calls[0] == (
        "remote_heads",
        "repo",
        tuple(sorted(head.ref for head in facts.remote_heads)),
    )
    assert [call for call in git.calls if call[0] == "remote_heads"] == git.calls[:1]
    states = {head.ref: head.state for head in facts.remote_heads}
    assert states["refs/heads/main"] == "present"
    assert states["refs/heads/aq/source"] == "unknown"  # answered for another ref
    assert "remote_unknown:refs/heads/aq/source" in facts.unknown

    class Failed(Batched):
        async def remote_heads(self, repository, refs):
            raise ConnectionError("do not expose exception detail")

    facts = await observe(snapshot(), Failed())
    assert facts.remote_heads and all(head.state == "unknown" for head in facts.remote_heads)


async def test_observer_reads_single_remote_heads_concurrently():
    class Slow(Git):
        in_flight = peak = 0

        async def remote_head(self, repository, ref):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0.01)
            self.in_flight -= 1
            return await super().remote_head(repository, ref)

    git = Slow()
    facts = await observe(snapshot(), git)
    assert len(facts.remote_heads) > 1 and git.peak == len(facts.remote_heads)
    assert all(head.state != "unknown" for head in facts.remote_heads)


async def test_ancestry_is_cached_by_sha_across_visits_and_unknown_is_retried():
    data = snapshot()
    git = Git()
    observer = IntegrationObserver(Reader(data), git, clock=lambda: NOW)
    first = await observer.observe_subject(data.subject.id)
    # Containment settles ancestry; the reverse probe is never made.
    assert [call for call in git.calls if call[0] == "ancestry"] == [
        ("ancestry", "repo", SOURCE, HEAD)
    ]
    git.calls.clear()
    second = await observer.observe_subject(data.subject.id)
    assert not [call for call in git.calls if call[0] == "ancestry"]
    assert first.members[0].ancestry == second.members[0].ancestry == "contained"

    git = Git(ancestry={(SOURCE, HEAD): False, (HEAD, SOURCE): None})
    observer = IntegrationObserver(Reader(data), git, clock=lambda: NOW)
    for _ in range(2):
        facts = await observer.observe_subject(data.subject.id)
        assert facts.members[0].ancestry == "unknown"
    # The definitive "not contained" is reused; the unknown reverse probe is retried.
    assert [call[2:] for call in git.calls if call[0] == "ancestry"] == [
        (SOURCE, HEAD),
        (HEAD, SOURCE),
        (HEAD, SOURCE),
    ]


async def test_database_reader_enforces_read_only_snapshot_over_real_existing_tables():
    db = Database(lease_dsn("integration-observe"))
    await db.initialize()
    try:
        data = snapshot(SubjectKind.SOURCE)
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(t.projects).values(
                    id="p",
                    name="project",
                    status="ACTIVE",
                    hierarchical_integration_policy=POLICY,
                    hierarchical_integration_generation=1,
                    created_at=1,
                )
            )
            await conn.execute(
                insert(t.repos).values(
                    id="repo",
                    project_id="p",
                    url="https://example/repo.git",
                    default_branch="main",
                    checkout_base_path="/checkout",
                    source_type="clone",
                )
            )
            await conn.execute(
                insert(t.tasks).values(
                    id="source",
                    project_id="p",
                    title="source",
                    description="",
                    status="COMPLETED",
                    created_at=1,
                    updated_at=1,
                )
            )
            await conn.execute(
                insert(t.playbook_artifacts).values(
                    artifact_sha256=ARTIFACT,
                    playbook_id="train",
                    source_digest="sha256:" + "2" * 64,
                    contract_fingerprint="sha256:" + "3" * 64,
                    compiler_build="test",
                    path="/test",
                    created_at=1,
                )
            )
            await conn.execute(insert(t.integration_subjects).values(**data.subject.to_row()))
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", record)
        reader = DatabaseObservationReader(db)
        facts = await IntegrationObserver(reader, Git(), clock=lambda: NOW).observe_subject(
            "subject"
        )
        assert facts.subject_id == "subject" and facts.head.sha == SOURCE
        assert await reader.read("missing") is None
        assert "SET TRANSACTION READ ONLY" in statements
        assert all(
            statement.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY"))
            for statement in statements
        )
        async with db._engine.connect() as conn:
            row = (await conn.execute(select(t.integration_subjects))).mappings().one()
        assert Subject.from_row(row) == data.subject
        event.remove(db._engine.sync_engine, "before_cursor_execute", record)
    finally:
        await db.close()


@pytest.mark.parametrize("state", [CIState.GREEN, CIState.RED, CIState.PENDING, CIState.INFRA])
async def test_reconciler_candidate_reads_live_checks_without_evidence_rows(state):
    from src.integration.subjects import CIEvidence

    data = snapshot(engine="reconciler")
    live = AsyncMock(return_value=CIEvidence(head_sha=HEAD, state=state, observed_at=NOW))
    facts = await IntegrationObserver(Reader(data), Git(), candidate_ci=live).observe(data.subject)
    assert facts.ci_state is state
    assert facts.ci[0].evidence_id is None
    assert live.await_args.args[1].sha == HEAD
    assert live.await_args.args[1].generation == 2


async def test_live_candidate_checks_cannot_answer_another_sha_or_use_stale_green():
    from src.integration.subjects import CIEvidence

    data = with_rows(snapshot(engine="reconciler"), integration_check_evidence=[ci_row()])
    for answer in (CIEvidence(head_sha=OTHER, state=CIState.GREEN), OSError("offline")):
        live = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(
            return_value=answer
        )
        facts = await IntegrationObserver(Reader(data), Git(), candidate_ci=live).observe(data.subject)
        assert facts.ci_state is CIState.INFRA


async def test_reconciler_without_current_candidate_does_not_probe_remote_ci():
    data = with_rows(
        snapshot(engine="reconciler"), integration_candidate_revisions=[]
    )
    live = AsyncMock()
    facts = await IntegrationObserver(Reader(data), Git(), candidate_ci=live).observe(data.subject)
    live.assert_not_awaited()
    assert facts.ci_state is CIState.NONE


async def test_root_live_reader_uses_frozen_check_set_and_exact_candidate(monkeypatch):
    from types import SimpleNamespace
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.root_runtime import root_candidate_ci_reader
    from tests.test_integration_ci_producers import github

    client, trust = github(app=False)
    client.checks[0]["head_sha"] = HEAD
    client.workflows[0]["head_sha"] = HEAD
    client.jobs[0]["head_sha"] = HEAD
    data = with_rows(snapshot(engine="reconciler"), integration_batches=[{
        **snapshot().all("integration_batches")[0],
        "policy_snapshot": {"root": {"required_checks": {
            "producer_id": "15368", "version": "v1", "names": ["unit"]}}},
    }])
    service = SimpleNamespace(_load_trust=AsyncMock(return_value=(trust, client)))
    owner = SimpleNamespace(
        config=SimpleNamespace(integration=SimpleNamespace(git_first="shadow")),
        db=SimpleNamespace(get_repo=AsyncMock(return_value=object())),
        github_repository_binding_resolver=AsyncMock(
            return_value=GitHubRepositoryBinding(123, "acme/widgets")),
        integration_attestation_service=service,
    )
    facts = await IntegrationObserver(
        Reader(data), Git(), candidate_ci=root_candidate_ci_reader(owner)
    ).observe(data.subject)
    assert facts.ci_state is CIState.GREEN and facts.ci[0].evidence_id is None
    state = service._load_trust.await_args.args[0]
    assert state["candidate_sha"] == HEAD
    assert state["policy_snapshot"]["root"]["required_checks"]["names"] == ["unit"]
    assert all(HEAD in call.args[0] or "jobs" in call.args[0]
               for call in client.paged_items.await_args_list)


async def test_git_adapter_reads_many_heads_in_batched_round_trips():
    git = AsyncMock()
    refs = [f"refs/heads/aq/member-{index:03d}" for index in range(250)]

    async def batch(path, branches, *, repository_url):
        return {
            branch: RemoteRefResult(RemoteRefState.PRESENT, oid=HEAD)
            if branch.endswith("0")
            else RemoteRefResult(RemoteRefState.ABSENT)
            for branch in branches
        }

    git.als_remote_refs.side_effect = batch
    adapter = GitObservationReader(git)
    repository = snapshot().repository
    heads = await adapter.remote_heads(repository, [*refs, "refs/tags/v1"])
    assert list(heads) == [*refs, "refs/tags/v1"]
    assert heads[refs[0]].state == "present" and heads[refs[0]].sha == HEAD
    assert heads[refs[1]].state == "absent" and heads["refs/tags/v1"].state == "unknown"
    assert git.als_remote_refs.await_count == 2  # chunked, never one call per ref
    git.als_remote_ref.assert_not_called()


async def test_observer_prefers_batched_remote_heads_and_keeps_errors_unknown():
    class Batched(Git):
        batches = 0

        async def remote_heads(self, repository, refs):
            self.batches += 1
            return [await Git.remote_head(self, repository, ref) for ref in refs]

        async def remote_head(self, repository, ref):  # pragma: no cover - must not be used
            raise AssertionError("per-ref read used despite batch support")

    git = Batched()
    facts = await observe(snapshot(), git)
    assert git.batches == 1 and facts.remote_heads

    class Broken(Git):
        async def remote_heads(self, repository, refs):
            raise ConnectionError("down")

    facts = await observe(snapshot(), Broken())
    assert all(head.state == "unknown" for head in facts.remote_heads)
