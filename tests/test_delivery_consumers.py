"""Every delivery consumer asks git the same question and gets the same answer.

Stale-container settlement, integration status, task explanations, doctor,
archive and branch cleanup read development delivery through
:class:`~src.integration.delivery_observer.DeliveryObserver`, never a delivery
record.  Real PostgreSQL and a real bare ``origin``: one development epic holds
a child for each shape that must never read as delivered (a wrong repository,
an unlabelled close whose ref is missing, a reopened task) beside one that is,
plus misleading finished-operation and retired-journal history, and every
surface is checked against the same fixture.
"""

from __future__ import annotations

import json
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import events, projects, tasks
from src.doctor.integration_checks import run_check
from src.doctor.models import Severity
from src.git.manager import GitManager
from src.integration.controls import IntegrationControlService
from src.integration.delivery_observer import DeliveryObserver
from src.integration.delivery_truth import DeliveryState
from src.integration.development import DevelopmentIntegration
from src.integration.status import IntegrationStatusService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus
from tests.db_fixtures import lease_dsn

CHILDREN = ("done", "wrong", "missing", "reopened")


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class Origin:
    def __init__(self, tmp_path):
        self.url = str(tmp_path / "origin.git")
        git(tmp_path, "init", "--bare", "--initial-branch=main", self.url)
        self.clone = tmp_path / "clone"
        git(tmp_path, "clone", self.url, str(self.clone))
        git(self.clone, "config", "user.name", "Tester")
        git(self.clone, "config", "user.email", "tester@example.test")
        (self.clone / "base.txt").write_text("base\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", "base")
        git(self.clone, "push", "-q", "origin", "main")

    def work(self, tid: str, name: str = "work") -> str:
        git(self.clone, "fetch", "-q", "origin")
        remote = git(self.clone, "branch", "-r")
        start = f"origin/aq/{tid}" if f"origin/aq/{tid}" in remote else "origin/main"
        git(self.clone, "checkout", "-q", "-B", f"aq/{tid}", start)
        (self.clone / f"{tid}-{name}.txt").write_text(name + "\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", f"{tid} {name}")
        git(self.clone, "push", "-q", "origin", f"aq/{tid}")
        return git(self.clone, "rev-parse", "HEAD")

    def land(self, tid: str) -> None:
        git(self.clone, "fetch", "-q", "origin")
        git(self.clone, "checkout", "-q", "-B", "main", "origin/main")
        git(self.clone, "merge", "-q", "--no-ff", "-m", f"deliver {tid}", f"origin/aq/{tid}")
        git(self.clone, "push", "-q", "origin", "main")


async def close(db, tid: str, commits: list[str], *, close_id: str | None = None,
                origin: Origin | None = None) -> None:
    """Close *tid*; with *origin*, retain the final source in git as a worker close does."""
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    close_id = close_id or f"close-{tid}"
    await db.save_task_completion(
        TaskCompletion(id=close_id, task_id=tid, outcome="pass",
                       commits=commits, completed_at=time.time())
    )
    if origin is not None and commits:
        await GitProvenance(GitManager(), str(origin.clone), repository_url=origin.url
                            ).write_completion(
            CompletedSource(CompletionIdentity("p", "r", tid, close_id), commits[-1])
        )
    await db.transition_task(tid, TaskStatus.COMPLETED)


@pytest.fixture
async def world(tmp_path):
    from src.database import Database

    origin = Origin(tmp_path)
    db = Database(lease_dsn("delivery-consumers.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Development"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=origin.url)
    )
    await db.create_project(Project(id="p-web", name="Web"))
    await db.create_repo(
        RepoConfig(id="web", project_id="p-web", source_type=RepoSourceType.CLONE,
                   url="https://example.test/web.git")
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(
                integration_repository_id="r", hierarchical_integration_mode="development",
                hierarchical_integration_desired_mode="development",
            )
        )
    observer = DeliveryObserver(db, git=GitManager(), data_dir=tmp_path / "data")
    db.set_delivery_observer(observer)

    await db.create_task(Task(id="epic", project_id="p", title="epic", description="",
                              status=TaskStatus.IN_PROGRESS))
    for tid in CHILDREN:
        await db.create_task(Task(
            id=tid, project_id="p", repo_id="r", title=tid, description="",
            branch_name=f"aq/{tid}", status=TaskStatus.IN_PROGRESS,
        ))
        await db.add_dependency(tid, "epic", "parent-child")
    await db.transition_task("epic", TaskStatus.BLOCKED, context="restart_recovery", force=True)

    # done: its work is on main.
    await close(db, "done", [origin.work("done")], origin=origin)
    origin.land("done")
    # wrong: its work is on main, but it names another project's repository.
    await close(db, "wrong", [origin.work("wrong")], origin=origin)
    origin.land("wrong")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "wrong").values(repo_id="web"))
    # missing: an unlabelled close (no generation retained in git) whose
    # branch was never pushed: nothing can stand in for its provenance.
    await close(db, "missing", [])
    # reopened: delivered once, then reopened and closed again on new work.
    old = origin.work("reopened")
    await close(db, "reopened", [old], close_id="close-reopened-1", origin=origin)
    origin.land("reopened")
    await db.transition_task("reopened", TaskStatus.IN_PROGRESS, force=True)
    await close(db, "reopened", [origin.work("reopened", "again")], close_id="close-reopened-2",
                origin=origin)
    # History that claims the recorded work delivered: a finished publisher
    # action and a retired journal row.  Neither answers delivery.
    now = time.time()
    history = {
        "project_id": "p", "repository_id": "r", "target_ref": "refs/heads/main",
        "expected_sha": None, "prepared_sha": None, "created_at": now, "updated_at": now,
        "manifest": [{"task_id": tid, "source_sha": old} for tid in CHILDREN],
        "evidence": {}, "reason": "misleading history",
    }
    async with db._engine.begin() as conn:
        for event_type, payload in (
            ("development.operation", {**history, "id": "misleading", "state": "finished"}),
            ("development.legacy_provenance",
             {**history, "id": "legacy-provenance:old", "legacy_id": "old"}),
        ):
            await conn.execute(insert(events).values(
                event_type=event_type, project_id="p", payload=json.dumps(payload),
                timestamp=now,
            ))
    service = DevelopmentIntegration(db, data_dir=tmp_path / "data", git=GitManager())
    yield db, origin, observer, service
    await db.close()


UNDELIVERED = {
    "wrong": ("delivery_unknown", "scope_mismatch"),
    "missing": ("delivery_unknown", "missing_git_provenance"),
    "reopened": ("development_delivery_pending", None),
}


async def test_git_answers_each_shape_once(world):
    _db, _origin, observer, _service = world
    view = await observer.observe(CHILDREN)
    assert {tid: (view.get(tid).state, view.get(tid).reason) for tid in CHILDREN} == {
        "done": (DeliveryState.CONTAINED, "git_completion"),
        "wrong": (DeliveryState.UNKNOWN, "scope_mismatch"),
        # Unlabelled: no branch head, reported commit or history stands in.
        "missing": (DeliveryState.UNKNOWN, "missing_git_provenance"),
        "reopened": (DeliveryState.PENDING, "git_completion"),
    }


async def test_status_and_explanations_agree_with_git(world):
    db, _origin, _observer, _service = world
    status = await IntegrationStatusService(db).status("p")
    delivery = status["delivery"]
    assert delivery["available"] and delivery["evaluated"] == 4
    assert delivery["pending"] == ["reopened"]
    assert {item["task_id"]: item["reason"] for item in delivery["unknown"]} == {
        "wrong": "scope_mismatch", "missing": "missing_git_provenance",
    }
    assert {
        (item["ref"], item["cause"]) for item in status["blockers"]
        if item["code"] == "delivery_unknown"
    } == {("wrong", "scope_mismatch"), ("missing", "missing_git_provenance")}
    # Readiness still means "no publication in flight"; nothing is persisted.
    assert status["ready"] is True

    for tid in CHILDREN:
        projection = await IntegrationStatusService(db).task_blockers(tid)
        delivery_blockers = [
            (item["code"], item.get("cause")) for item in projection["blockers"]
            if item["code"] in {"delivery_unknown", "development_delivery_pending"}
        ]
        assert delivery_blockers == ([UNDELIVERED[tid]] if tid in UNDELIVERED else [])


async def test_doctor_reports_the_same_unknown_work(world):
    db, _origin, _observer, _service = world
    handler = MagicMock()

    async def execute(command, args):
        assert command == "integration_status"
        return await IntegrationControlService(db).status(args["project_id"])

    handler.execute = AsyncMock(side_effect=execute)
    result = await run_check(db, "integration.operational", handler=handler)

    assert result.severity is Severity.WARN
    project = next(item for item in result.data["projects"] if item["project_id"] == "p")
    assert {
        item["ref"] for item in project["blockers"] if item["code"] == "delivery_unknown"
    } == {"wrong", "missing"}


async def test_branch_cleanup_holds_what_git_cannot_prove(world):
    _db, _origin, _observer, service = world
    holds = await service.branch_holds(
        ["aq/done", "aq/wrong", "aq/missing", "aq/reopened"]
    )
    assert "aq/done" not in holds
    assert holds["aq/wrong"] == "task wrong delivery is unknown (scope_mismatch)"
    assert holds["aq/missing"] == "task missing delivery is unknown (missing_git_provenance)"
    assert holds["aq/reopened"] == "task reopened is not delivered yet"

    report = await service.stale_branches("p")
    stale = {entry["branch"] for entry in report["stale"]}
    held = {entry["branch"]: entry["held_by"] for entry in report["held"]}
    # Both landed; only the one git proves for its own task may go.
    assert "aq/done" in stale
    assert held["aq/wrong"] == "task wrong delivery is unknown (scope_mismatch)"


async def test_settlement_and_archive_refuse_what_git_cannot_prove(world):
    db, _origin, observer, _service = world
    view = await observer.observe(CHILDREN)
    async with db._engine.begin() as conn:
        result = await db.settle_containers({"epic"}, conn=conn, delivery=view)
    assert result.settled == []
    assert (await db.get_task("epic")).status == TaskStatus.BLOCKED

    holders = {}
    for tid in ("wrong", "missing", "reopened"):
        with pytest.raises(HierarchyError) as refused:
            await db.archive_task(tid)
        assert refused.value.code == "integration_undelivered"
        holders[tid] = [
            (row["holder"], row["detail"]) for row in refused.value.context["undelivered"]
        ]
    assert holders == {
        "wrong": [("delivery_unknown", "scope_mismatch")],
        "missing": [("delivery_unknown", "missing_git_provenance")],
        "reopened": [("awaiting_publication", "not yet published")],
    }
    # What git proves archives, and archiving changes no answer about it.
    assert await db.archive_task("done") is True
    view = await observer.observe(["done"])
    assert view.get("done").state is DeliveryState.CONTAINED
    assert view.get("done").request.archived is True


async def test_every_surface_follows_git_once_the_work_lands(world):
    db, origin, observer, service = world
    # Rebinding the repository and landing the reopened work leave only the
    # missing ref: git never turns an absent source into a delivery.
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "wrong").values(repo_id="r"))
    origin.land("reopened")

    status = await IntegrationStatusService(db).status("p")
    assert status["delivery"]["pending"] == []
    assert [item["task_id"] for item in status["delivery"]["unknown"]] == ["missing"]
    holds = await service.branch_holds(["aq/wrong", "aq/reopened", "aq/missing"])
    assert set(holds) & {"aq/wrong", "aq/reopened"} == set()
    assert "aq/missing" in holds
    assert await db.archive_task("reopened") is True
    with pytest.raises(HierarchyError):
        await db.archive_task("missing")

    # Once the missing task is closed obsolete it needs no proof, and the
    # epic settles on the next pass: every remaining child is proven in git.
    await db.set_task_meta("missing", "obsolete", json.dumps({"reason": "superseded"}))
    view = await observer.observe(["done", "wrong"])
    async with db._engine.begin() as conn:
        result = await db.settle_containers({"epic"}, conn=conn, delivery=view)
    assert result.settled == ["epic"]


async def test_a_retired_manifest_source_keeps_a_branchless_close_unknown(world):
    """The retirement marker keeps a branchless legacy artifact unknown, fenced to its generation.

    Without it a branchless task a retired manifest delivered code for would
    read as an organizational container: "no artifact", i.e. satisfied.
    """
    from src.integration.delivery_observer import delivery_sensitive_ids
    from src.integration.publishable_artifact import LEGACY_ARTIFACT_KEY

    db, _origin, observer, _service = world
    for tid in ("legacy-artifact", "organizational"):
        await db.create_task(Task(id=tid, project_id="p", repo_id="r", title=tid,
                                  description="", status=TaskStatus.COMPLETED))
    await db.set_task_meta("legacy-artifact", LEGACY_ARTIFACT_KEY, {
        "legacy_id": "old", "source_sha": "a" * 40, "completion_id": None,
        "reason": "retired manifest named an unrecorded source",
    })
    async with db._engine.connect() as conn:
        assert await delivery_sensitive_ids(
            conn, ["legacy-artifact", "organizational"]
        ) == {"legacy-artifact"}
    view = await observer.observe(["legacy-artifact", "organizational"])
    evidence = view.get("legacy-artifact")
    assert (evidence.state, evidence.reason) == (DeliveryState.UNKNOWN, "missing_git_provenance")
    assert view.get("organizational").state is DeliveryState.NO_ARTIFACT
    blockers = (await IntegrationStatusService(db).task_blockers("legacy-artifact"))["blockers"]
    assert ("delivery_unknown", "missing_git_provenance") in {
        (item["code"], item.get("cause")) for item in blockers
    }

    # A later (code-free, branchless) generation replaces the fenced marker.
    await db.save_task_completion(TaskCompletion(
        id="code-free", task_id="legacy-artifact", outcome="pass", commits=[],
        completed_at=time.time(),
    ))
    async with db._engine.connect() as conn:
        assert await delivery_sensitive_ids(conn, ["legacy-artifact"]) == set()
    view = await observer.observe(["legacy-artifact"])
    assert view.get("legacy-artifact").state is DeliveryState.NO_ARTIFACT
