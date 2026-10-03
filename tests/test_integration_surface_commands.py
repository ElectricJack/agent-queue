"""The consolidated integration operator surface against a real database.

Spec §5.1 (``gate answer``, ``policy activate``, ``hold``, ``explain``,
``status`` subjects, ``flush`` wake) and §5.5 (``subjects_overdue``,
``subjects_held``).  The command-tree and registry shape is
``tests/test_integration_surface.py``.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import insert, update

from src.commands.integration_surface_commands import (
    IntegrationSurfaceCommandsMixin,
    integration_gate_subject,
)
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import integration_subjects, playbook_artifacts
from src.doctor.integration_subject_checks import (
    OVERDUE_AFTER_SECONDS,
    STALE_HOLD_SECONDS,
    _check_subjects_held,
    _check_subjects_overdue,
)
from src.doctor.models import DoctorContext, Severity
from src.integration.records import human_hold_on
from src.integration.subjects import (
    OPERATOR_HOLD_META_KEY,
    PolicyArtifactPin,
    Subject,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    subject_key,
)
from src.models import Project, Task, TaskStatus
from src.profiles.capabilities import DENY_ALL

NOW = time.time()
HEAD = "a" * 40
BASE = "b" * 40
PIN = PolicyArtifactPin(playbook_id="surface-test", artifact_sha256="sha256:" + "1" * 64)


class Handler(IntegrationSurfaceCommandsMixin):
    def __init__(self, db):
        self.db = db
        self.orchestrator = None
        self.develop_calls: list[dict] = []
        self.controls = None

    async def _cmd_integration_develop(self, args: dict) -> dict:
        self.develop_calls.append(args)
        return {"success": True, "outcome": "configured", "project_id": args["project_id"]}

    def _integration_control_service(self):
        return self.controls


def _subject(subject_id: str, *, kind=SubjectKind.SOURCE, task_id="t1", project_id="p",
             schedule=None, phase=SubjectPhase.BUILDING) -> dict:
    parts = ("request-" + subject_id,) if kind is SubjectKind.ROOT_BATCH else (task_id, 0)
    subject = Subject(
        id=subject_id,
        project_id=project_id,
        repository_id="repo",
        kind=kind,
        subject_key=subject_key(kind, "repo", *parts),
        phase=phase,
        policy=PIN,
        task_id=None if kind is SubjectKind.ROOT_BATCH else task_id,
        target_ref="refs/heads/main",
        head_sha=HEAD,
        base_sha=BASE,
        generation=0,
        schedule=schedule or SubjectSchedule.progress(now=NOW + 300, max_wait_seconds=600),
        created_at=NOW,
        updated_at=NOW,
    )
    row = subject.to_row()
    for column in ("last_journal_seq", "version", "wake_requested_at", "last_visit_at"):
        row.pop(column)
    return row


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("integration-surface")
    for project_id in ("p", "other"):
        await database.create_project(Project(id=project_id, name=project_id))
    for task_id, project_id in (("t1", "p"), ("t2", "p"), ("t9", "other")):
        await database.create_task(Task(
            id=task_id, project_id=project_id, title=task_id, description=task_id,
            status=TaskStatus.COMPLETED, created_at=NOW, updated_at=NOW,
        ))
    async with database._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=PIN.artifact_sha256,
                playbook_id=PIN.playbook_id,
                source_digest="sha256:" + "2" * 64,
                contract_fingerprint="sha256:" + "3" * 64,
                compiler_build="test",
                path="/test/surface.json",
                created_at=1.0,
            )
        )
    await database.ensure_integration_subject(_subject("source-1"))
    await database.ensure_integration_subject(_subject("root-1", kind=SubjectKind.ROOT_BATCH))
    await database.ensure_integration_subject(
        _subject("source-9", task_id="t9", project_id="other")
    )
    return database


def _due(row: dict) -> float | None:
    return row["next_due_at"]


# ------------------------------------------------------------------ listing


async def test_listing_filters_by_task_and_root(db):
    by_task = await db.list_integration_subjects(project_id="p", task_ids=["t1"])
    assert [row["id"] for row in by_task] == ["source-1"]
    roots = await db.list_integration_subjects(project_id="p", roots_only=True)
    assert [row["id"] for row in roots] == ["root-1"]
    assert [row["id"] for row in await db.list_integration_subjects(project_id="other")] == [
        "source-9"
    ]


async def test_flush_makes_only_its_projects_subjects_due(db):
    handler = Handler(db)
    before = time.time()
    assert await handler._wake_project_integration_subjects("p") == 2
    assert _due(await db.get_integration_subject("source-1")) <= time.time()
    assert _due(await db.get_integration_subject("root-1")) >= before - 1
    assert _due(await db.get_integration_subject("source-9")) > time.time() + 200


# ------------------------------------------------------------------ hold


async def test_hold_binds_at_mutation_time_and_releases(db):
    handler = Handler(db)
    held = await handler._cmd_integration_hold({"target": "source-1", "reason": "bad release"})
    assert held["success"] and held["outcome"] == "held"
    assert held["task_ids"] == ["t1"] and held["subject_id"] == "source-1"
    # The subject and the project's roots are made due so the observer sees it.
    assert held["woken_subjects"] == 2
    assert _due(await db.get_integration_subject("source-1")) <= time.time()
    stored = await db.get_task_meta("t1", OPERATOR_HOLD_META_KEY)
    assert stored["reason"] == "bad release" and stored["held_by"] == "human:local-operator"
    subject = Subject.from_row(await db.get_integration_subject("source-1"))
    async with db._engine.begin() as conn:
        assert await human_hold_on(conn, subject) == "operator_hold"

    again = await handler._cmd_integration_hold({"target": "t1", "reason": "twice"})
    assert again["outcome"] == "already_held" and again["reason"] == "bad release"

    released = await handler._cmd_integration_hold({"target": "t1", "release": True})
    assert released["outcome"] == "released" and released["reason"] == "bad release"
    assert await db.get_task_meta("t1", OPERATOR_HOLD_META_KEY) is None
    async with db._engine.begin() as conn:
        assert await human_hold_on(conn, subject) is None
    not_held = await handler._cmd_integration_hold({"target": "t1", "release": True})
    assert not_held["success"] and not_held["outcome"] == "not_held"


async def test_hold_refuses_roots_unknown_targets_and_other_callers(db):
    handler = Handler(db)
    root = await handler._cmd_integration_hold({"target": "root-1", "reason": "x"})
    assert not root["success"] and root["outcome"] == "invalid"
    missing = await handler._cmd_integration_hold({"target": "nope", "reason": "x"})
    assert missing["outcome"] == "not_found"
    crossed = await handler._cmd_integration_hold(
        {"target": "t9", "reason": "x", "project_id": "p"}
    )
    assert crossed["outcome"] == "unauthorized"
    with principal_context(ExecutionPrincipal.service("integration loop")):
        service = await handler._cmd_integration_hold({"target": "t1", "reason": "x"})
    assert service["outcome"] == "unauthorized"
    assert await db.get_task_meta("t1", OPERATOR_HOLD_META_KEY) is None


# ------------------------------------------------------------------ explain/status


async def test_explain_shows_the_latest_decisions_newest_first(db):
    for seq in range(3):
        await db.append_integration_subject_journal({
            "subject_id": "source-1",
            "entry_kind": "decision",
            "idempotency_key": f"visit-{seq}:decision",
            "visit_id": f"visit-{seq}",
            "mode": "shadow",
            "policy_artifact_sha256": PIN.artifact_sha256,
            "subject_version": 0,
            "phase": "building",
            "head_sha": HEAD,
            "generation": 0,
            "rule": f"source/building/rule-{seq}",
            "primitive": "wait",
            "facts_digest": "sha256:" + "e" * 64,
            "payload": {"visit": seq},
            "recorded_at": NOW + seq,
        })
    handler = Handler(db)
    explained = await handler._cmd_integration_explain({"target": "t1", "limit": 2})
    assert explained["success"] and explained["project_id"] == "p"
    [view] = explained["subjects"]
    assert view["id"] == "source-1" and view["phase"] == "building"
    assert [entry["rule"] for entry in view["decisions"]] == [
        "source/building/rule-2", "source/building/rule-1",
    ]
    by_subject = await handler._cmd_integration_explain({"target": "root-1"})
    assert [view["id"] for view in by_subject["subjects"]] == ["root-1"]
    assert by_subject["subjects"][0]["decisions"] == []
    none = await handler._cmd_integration_explain({"target": "t2"})
    assert none["outcome"] == "not_found"


async def test_status_attaches_the_subject_view(db):
    handler = Handler(db)
    base = {"success": True, "outcome": "ok", "project_id": "p"}
    result = await handler._integration_status_subjects(dict(base), "p", None)
    assert sorted(view["id"] for view in result["subjects"]) == ["root-1", "source-1"]
    assert all(view["overdue_seconds"] == 0.0 for view in result["subjects"])
    one = await handler._integration_status_subjects(dict(base), "p", "source-1")
    assert [view["id"] for view in one["subjects"]] == ["source-1"]
    foreign = await handler._integration_status_subjects(dict(base), "p", "source-9")
    assert foreign["outcome"] == "not_found"


# ------------------------------------------------------------------ gate answer


async def _root_adapter_gate(db) -> str:
    gate_id, _ = await db.create_gate(
        "p", "human", "Publish the batch?",
        question="Publish the batch?\nChoices: approve, hold",
        await_id="integration-subject:gated-1:" + "d" * 64,
    )
    await db.ensure_integration_subject(
        _subject(
            "gated-1", task_id="t2",
            schedule=SubjectSchedule.hold(now=NOW, gate_id=gate_id, max_wait_seconds=600),
        )
    )
    return gate_id


def test_gate_identity_parses_both_gate_shapes():
    assert integration_gate_subject("integration-subject:s1") == ("s1", True)
    assert integration_gate_subject("integration-subject:s1:" + "d" * 64) == ("s1", False)
    assert integration_gate_subject("integration-subject:") is None
    assert integration_gate_subject("task:t1") is None


async def test_the_local_operator_answers_a_root_adapter_gate(db):
    gate_id = await _root_adapter_gate(db)
    handler = Handler(db)
    wrong = await handler._cmd_integration_gate_answer({"gate_id": gate_id, "choice": "ship"})
    assert not wrong["success"] and wrong["reason"] == "invalid_gate_answer"
    assert (await db.get_gate(gate_id))["status"] == "open"

    answered = await handler._cmd_integration_gate_answer(
        {"gate_id": gate_id, "choice": "approve"}
    )
    assert answered["success"] and answered["subject_id"] == "gated-1"
    assert answered["answered_by"] == "human:local-operator"
    gate = await db.get_gate(gate_id)
    assert gate["status"] == "resolved" and gate["resolution"] == "approve"
    assert _due(await db.get_integration_subject("gated-1")) <= time.time()
    # The same answer again is idempotent; a different one is immutable.
    same = await handler._cmd_integration_gate_answer({"gate_id": gate_id, "choice": "approve"})
    assert same["success"]
    changed = await handler._cmd_integration_gate_answer({"gate_id": gate_id, "choice": "hold"})
    assert changed["reason"] == "answer_immutable"


async def test_a_supervisor_relays_a_gate_instead_of_answering(db, monkeypatch):
    import src.commands.integration_surface_commands as surface

    gate_id = await _root_adapter_gate(db)

    async def live_supervisor(_db, project_id):
        return f"supervisor session:{project_id}-sup", None

    monkeypatch.setattr(surface, "integration_operator", live_supervisor)
    supervisor = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="p-sup", project_id="p",
        profile_id="supervisor", elevated=True,
    )
    with principal_context(supervisor):
        refused = await Handler(db)._cmd_integration_gate_answer(
            {"gate_id": gate_id, "choice": "approve"}
        )
    assert refused["reason"] == "verified_human_required"
    assert f"aq integration gate answer {gate_id}" in refused["error"]
    assert (await db.get_gate(gate_id))["status"] == "open"


async def test_gate_answer_refuses_a_gate_that_is_not_an_integration_gate(db):
    gate_id, _ = await db.create_gate("p", "human", "Other", await_id="task:t1")
    result = await Handler(db)._cmd_integration_gate_answer({"gate_id": gate_id, "choice": "x"})
    assert result["outcome"] == "not_found"


# ------------------------------------------------------------------ policy activate


class FakeControls:
    def __init__(self, configure_generation=4):
        self.calls: list[tuple[str, dict]] = []
        self.configure_generation = configure_generation

    async def configure(self, project_id, **kwargs):
        self.calls.append(("configure", kwargs))
        return {"success": True, "outcome": "configured", "project_id": project_id,
                "generation": self.configure_generation}

    async def enable(self, project_id, **kwargs):
        self.calls.append(("enable", kwargs))
        return {"success": True, "outcome": "enabled", "project_id": project_id,
                "generation": kwargs["expected_generation"] + 1}


async def test_policy_activate_pins_the_policy_then_enables_on_its_generation(db):
    handler = Handler(db)
    handler.controls = FakeControls()
    policy = {"root": {"route": {"artifact": {"playbook_id": "x"}}}}
    result = await handler._cmd_integration_policy_activate({
        "project_id": "p", "mode": "train", "expected_generation": 3, "reason": "cutover",
        "policy": policy,
    })
    assert result["success"] and result["outcome"] == "enabled"
    assert result["configured_generation"] == 4
    [(first, configure), (second, enable)] = handler.controls.calls
    assert (first, second) == ("configure", "enable")
    assert configure["updates"] == {"hierarchical_integration_policy": policy}
    assert configure["expected_generation"] == 3
    assert enable["expected_generation"] == 4 and enable["mode"] == "train"
    assert enable["operator_id"] == "human:local-operator"


async def test_policy_activate_without_a_policy_only_switches_mode(db):
    handler = Handler(db)
    handler.controls = FakeControls()
    result = await handler._cmd_integration_policy_activate({
        "project_id": "p", "mode": "disabled", "expected_generation": 7, "reason": "stop",
    })
    assert result["outcome"] == "enabled"
    assert [name for name, _ in handler.controls.calls] == ["enable"]
    assert handler.controls.calls[0][1]["expected_generation"] == 7


async def test_policy_activate_disabled_with_a_policy_stops_after_configure(db):
    handler = Handler(db)
    handler.controls = FakeControls()
    result = await handler._cmd_integration_policy_activate({
        "project_id": "p", "mode": "disabled", "expected_generation": 7, "reason": "stage",
        "policy": {"root": {}},
    })
    assert result["success"] and result["outcome"] == "configured"
    assert [name for name, _ in handler.controls.calls] == ["configure"]


async def test_policy_activate_development_routes_to_develop(db):
    handler = Handler(db)
    result = await handler._cmd_integration_policy_activate({
        "project_id": "p", "mode": "development", "reason": "dev", "policy": {"x": 1},
    })
    assert result["outcome"] == "configured"
    assert handler.develop_calls == [{"project_id": "p", "policy": {"x": 1}, "reason": "dev"}]


# ------------------------------------------------------------------ doctor


async def test_subjects_overdue_judges_only_reconciler_subjects(db):
    ctx = DoctorContext(config=None, db=db)
    legacy_only = await _check_subjects_overdue(ctx)
    assert legacy_only.severity is Severity.INFO
    async with db._engine.begin() as conn:
        await conn.execute(
            update(integration_subjects)
            .where(integration_subjects.c.id.in_(["source-1", "root-1"]))
            .values(engine="reconciler")
        )
    fine = await _check_subjects_overdue(ctx)
    assert fine.severity is Severity.OK and fine.data == {"live": 2, "overdue": 0}
    async with db._engine.begin() as conn:
        await conn.execute(
            update(integration_subjects)
            .where(integration_subjects.c.id == "source-1")
            .values(next_due_at=time.time() - OVERDUE_AFTER_SECONDS - 120)
        )
    late = await _check_subjects_overdue(ctx)
    assert late.severity is Severity.WARN
    assert late.data["overdue"] == 1
    assert [row["subject_id"] for row in late.data["subjects"]] == ["source-1"]


async def test_subjects_held_lists_gates_and_operator_holds(db):
    ctx = DoctorContext(config=None, db=db)
    assert (await _check_subjects_held(ctx)).severity is Severity.OK
    gate_id = await _root_adapter_gate(db)
    await Handler(db)._cmd_integration_hold({"target": "t1", "reason": "freeze"})
    held = await _check_subjects_held(ctx)
    assert held.severity is Severity.INFO
    assert held.data["gates"] == 1 and held.data["operator_holds"] == 1
    by_kind = {entry["hold"]: entry for entry in held.data["held"]}
    assert by_kind["gate"]["answer"] == f"aq integration gate answer {gate_id} CHOICE"
    assert by_kind["operator"]["reason"] == "freeze"
    assert by_kind["operator"]["answer"] == "aq integration hold t1 --release"
    stale = {"reason": "old", "held_by": "human:local-operator",
             "held_at": time.time() - STALE_HOLD_SECONDS - 60}
    await db.set_task_meta("t1", OPERATOR_HOLD_META_KEY, stale)
    assert (await _check_subjects_held(ctx)).severity is Severity.WARN
