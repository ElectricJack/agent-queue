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
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import events, projects, tasks
from src.git.manager import GitManager
from src.integration.delivery_observer import DeliveryObserver
from src.integration.delivery_truth import DeliveryState
from src.integration.development import DevelopmentPrimitives
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
    service = DevelopmentPrimitives(db, data_dir=tmp_path / "data", git=GitManager())
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


async def test_git_first_observer_reuses_parent_target_and_revalidates_ordinary_identity(world, tmp_path):
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "reduced",
                                truth=GitTruth(transport))
    db.set_delivery_observer(observer)
    git(origin.clone, "push", "origin", "main:aq/epic")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/epic", repo_id="r"))
    view = await observer.observe(["done"])
    assert view.satisfied("done")
    assert view.targets["done"].target_ref == "refs/heads/aq/epic"
    async with db._engine.connect() as conn:
        assert (await view.verified_on(conn, ["done"]))["done"].satisfied
    # Ordinary parent retargeting invalidates the old view without an episode.
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/elsewhere"))
    async with db._engine.connect() as conn:
        assert await view.verified_on(conn, ["done"]) == {}
    assert not (await observer.observe(["done"])).satisfied("done")


async def test_git_first_nested_targets_share_one_repository_fetch(world, tmp_path):
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "reduced",
                                truth=GitTruth(transport))
    git(origin.clone, "push", "origin", "main:aq/epic")
    await db.create_task(Task(id="other-epic", project_id="p", repo_id="r", title="other",
                              description="", branch_name="aq/missing-epic"))
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/epic", repo_id="r"))
        await conn.execute(update(tasks).where(tasks.c.id == "reopened")
                           .values(parent_task_id="other-epic"))
    real_snapshot = observer.truth.snapshot
    observer.truth.snapshot = AsyncMock(wraps=real_snapshot)
    view = await observer.observe(["done", "reopened"])
    assert view.satisfied("done")
    assert view.get("reopened").state is DeliveryState.UNKNOWN
    assert len(view.snapshots) == 2
    assert observer.truth.snapshot.await_count == 1


async def test_hierarchy_prerequisites_consume_revalidated_git_view_without_receipts(world, tmp_path):
    from sqlalchemy import select

    from src.database.queries.hierarchy_queries import (
        ProjectIntegrationMode,
        delivered_same_parent_prerequisites_when_hierarchical,
    )
    from src.database.tables import task_branch_origins, task_delivery_receipts
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "reduced",
                                truth=GitTruth(transport))
    await db.create_task(Task(id="dependent", project_id="p", repo_id="r", title="dependent",
                              description="", branch_name="aq/dependent", status=TaskStatus.READY))
    await db.add_dependency("dependent", "epic", "parent-child")
    await db.add_dependency("dependent", "done")
    git(origin.clone, "push", "origin", "main:aq/epic")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/epic", repo_id="r"))
        await conn.execute(insert(task_branch_origins).values(
            id="dependent-origin", task_id="dependent", repository_id="r",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="r",
            parent_ref="aq/epic", base_sha=git(origin.clone, "rev-parse", "main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time(),
        ))
    view = await observer.prerequisite_view("p", task_id="dependent")
    assert view.satisfied("done")
    assert await view.fresh()
    async with db._engine.connect() as conn:
        verified = await view.verified_on(conn, view.evidence)
        for ids, expected in ((frozenset(), []), (frozenset(verified), ["dependent"])):
            predicate = delivered_same_parent_prerequisites_when_hierarchical(
                ProjectIntegrationMode(True, "r", ids),
            )
            assert (await conn.execute(select(tasks.c.id).where(
                tasks.c.id == "dependent", predicate,
            ))).scalars().all() == expected
        assert not (await conn.execute(select(task_delivery_receipts))).first()
    await db.transition_task("done", TaskStatus.IN_PROGRESS, force=True)
    async with db._engine.connect() as conn:
        assert await view.verified_on(conn, ["done"]) == {}


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
    assert status["projection_kind"] == "subjects"

    for tid in CHILDREN:
        projection = await IntegrationStatusService(db).task_blockers(tid)
        delivery_blockers = [
            (item["code"], item.get("cause")) for item in projection["blockers"]
            if item["code"] in {"delivery_unknown", "development_delivery_pending"}
        ]
        assert delivery_blockers == ([UNDELIVERED[tid]] if tid in UNDELIVERED else [])


@pytest.mark.parametrize("cache_state", ["cold", "warm", "expired", "stale"])
async def test_explain_and_pool_status_only_read_cached_development_delivery(
    world, tmp_path, monkeypatch, cache_state,
):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.integration.delivery_observer import DeliveryTarget
    from src.models import AgentProfile
    from src.orchestrator import Orchestrator

    db, origin, observer, service = world
    service.git = observer.git
    service._delivery_observer = observer
    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("delivery-consumers.db")),
        discord=DiscordConfig(bot_token="test", guild_id="123"), data_dir=str(tmp_path / "data"),
    )
    config.swarm.enabled = True
    orch = Orchestrator(config)
    orch.db, orch.git, orch.development_integration = db, observer.git, service
    handler = CommandHandler(orch, config)
    await db.create_profile(AgentProfile(
        id="worker", name="Worker", lifecycle="pool", default_class="fast-low", harness="claude",
    ))
    await db.create_task(Task(
        id="waiting", project_id="p", repo_id="r", title="waiting", description="",
        status=TaskStatus.READY, profile_id="worker", route_source="override",
        intelligence_class="fast-low",
    ))
    await db.add_dependency("waiting", "done")
    await db.add_dependency("waiting", "reopened")
    if cache_state != "cold":
        await observer.observe(["done", "reopened"])
    if cache_state == "expired":
        observer._recent = {target: (stamp - observer.READ_MAX_AGE - 1, snapshot)
                            for target, (stamp, snapshot) in observer._recent.items()}
    if cache_state == "stale":
        snapshot = await observer.snapshot(
            DeliveryTarget("p", "r", origin.url, "refs/heads/main"),
            max_age=observer.READ_MAX_AGE,
        )
        base = git(origin.clone, "rev-list", "--max-parents=0", "origin/main")
        git(origin.clone, "push", "--force", "origin", f"{base}:main")
        assert not await snapshot.is_fresh()

    forbidden = {}
    for name in ("afetch_origin", "acreate_checkout", "als_remote_ref", "als_remote_refs"):
        forbidden[name] = AsyncMock(side_effect=AssertionError(f"diagnostic called {name}"))
        monkeypatch.setattr(observer.git, name, forbidden[name])
    result = await handler._cmd_explain_task({"task_id": "waiting"})
    delivery = [reason for reason in result["reasons"]
                if reason["code"] in {
                    "development_dependency_delivery", "delivery_evidence_unavailable",
                }]
    assert "awaiting_pool_session" in result["reason_codes"]
    if cache_state == "warm":
        assert [reason["ref"] for reason in delivery] == ["reopened"]
        assert "pending" in delivery[0]["detail"]
    else:
        assert {reason["ref"] for reason in delivery} == {"done", "reopened"}
        assert all("snapshot_unavailable" in reason["detail"] for reason in delivery)
        assert all(reason["code"] == "delivery_evidence_unavailable" for reason in delivery)
        assert all("has not loaded yet" in reason["detail"] for reason in delivery)
    status = await handler._cmd_pool_status({})
    assert next(row for row in status["pools"] if row["profile_id"] == "worker")["ready"] == 0
    for call in forbidden.values():
        call.assert_not_awaited()


async def test_cached_display_delivery_never_supplies_fresh_admission(world, monkeypatch):
    from src.integration.admission import observe_admission

    db, origin, observer, service = world
    service.git = observer.git
    service._delivery_observer = observer
    await db.create_task(Task(
        id="waiting", project_id="p", repo_id="r", title="waiting", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("waiting", "done")
    await observer.observe(["done"])
    base = git(origin.clone, "rev-list", "--max-parents=0", "origin/main")
    git(origin.clone, "push", "--force", "origin", f"{base}:main")

    fetch = AsyncMock(wraps=observer.git.afetch_origin)
    monkeypatch.setattr(observer.git, "afetch_origin", fetch)
    display = await observe_admission(db, ["waiting"], service, cached_only=True)
    assert "waiting" in display.allowed  # The rewind has not been observed yet.
    fetch.assert_not_awaited()

    admission = await observe_admission(db, ["waiting"], service)
    fetch.assert_awaited_once()
    assert "waiting" not in admission.allowed
    assert admission.reasons["waiting"][0]["code"] == "development_dependency_delivery"
    assert "pending" in admission.reasons["waiting"][0]["detail"]
    assert admission.snapshots[0] is not display.snapshots[0]


async def test_display_cache_miss_never_withholds_fresh_admission(world, monkeypatch):
    from src.integration.admission import observe_admission

    db, _origin, observer, service = world
    service.git = observer.git
    service._delivery_observer = observer
    await db.create_task(Task(
        id="waiting", project_id="p", repo_id="r", title="waiting", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("waiting", "done")
    fetch = AsyncMock(wraps=observer.git.afetch_origin)
    monkeypatch.setattr(observer.git, "afetch_origin", fetch)
    display = await observe_admission(db, ["waiting"], service, cached_only=True)
    assert display.allowed == set()
    assert display.reasons["waiting"][0]["code"] == "delivery_evidence_unavailable"
    fetch.assert_not_awaited()

    admission = await observe_admission(db, ["waiting"], service)
    fetch.assert_awaited_once()
    assert admission.allowed == {"waiting"}
    assert admission.reasons == {}
    assert await admission.is_fresh("waiting")
    assert await admission.matches(task_id="waiting")


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


async def test_exact_source_trailers_only_match_the_whole_identity(world):
    """A trailer proves one whole generation, reachable from the fetched target.

    A substring, an abbreviated SHA, one commit of a multi-commit source and the
    previous generation of a reopened task are each a different identity, and a
    trailer that is not reachable from the target is not delivery.
    """
    from sqlalchemy import select

    from src.database.tables import task_completion_records
    from src.integration.source_trailer import (
        SourceIdentity,
        delivered_by_source_trailer,
        reachable_source_identities,
    )

    db, origin, _observer, _service = world
    store = origin.clone
    git_manager = GitManager()

    async with db._engine.connect() as conn:
        generations = (
            await conn.execute(
                select(task_completion_records.c.commits)
                .where(task_completion_records.c.task_id == "reopened")
                .order_by(task_completion_records.c.completed_at)
            )
        ).scalars().all()
    old_source, new_source = (json.loads(commits)[-1] for commits in generations)
    assert old_source != new_source

    git(store, "fetch", "-q", "origin")
    main = git(store, "rev-parse", "origin/main")

    def merge_message(body):
        return f"Integrate reopened\n\n{body}"

    # The reachable merge names the task's first generation only.
    git(store, "checkout", "-q", "-B", "aq/trailer-probe", main)
    git(store, "merge", "-q", "--no-ff", "-m",
        merge_message(f"AQ-Source: reopened@{old_source}"), "origin/aq/reopened")
    probe = git(store, "rev-parse", "HEAD")
    assert await delivered_by_source_trailer(
        git_manager, store, "reopened", old_source, probe
    ) is True
    # The narrowed read and a full scan of the reachable history agree.
    assert SourceIdentity("reopened", old_source) in await reachable_source_identities(
        git_manager, store, probe
    )
    assert await reachable_source_identities(git_manager, store, main) == frozenset()
    # The reopened task's new completion is not satisfied by its old trailer.
    assert await delivered_by_source_trailer(
        git_manager, store, "reopened", new_source, probe
    ) is False
    # Not reachable from the fetched target, so not delivered to it.
    assert await delivered_by_source_trailer(
        git_manager, store, "reopened", old_source, main
    ) is False

    # An abbreviated SHA and a substring of a real one are not identities.
    git(store, "checkout", "-q", "-B", "aq/trailer-abbrev", main)
    git(store, "merge", "-q", "--no-ff", "-m",
        merge_message(f"AQ-Source: reopened@{old_source[:12]}"), "origin/aq/reopened")
    abbrev = git(store, "rev-parse", "HEAD")
    assert await delivered_by_source_trailer(
        git_manager, store, "reopened", old_source, abbrev
    ) is False
    git(store, "checkout", "-q", "-B", "aq/trailer-substring", main)
    git(store, "merge", "-q", "--no-ff", "-m",
        merge_message(f"AQ-Source: reopened@{old_source} and more"), "origin/aq/reopened")
    substring = git(store, "rev-parse", "HEAD")
    assert await delivered_by_source_trailer(
        git_manager, store, "reopened", old_source, substring
    ) is False

    # A two-commit source is named by its head: one of its commits is not it.
    git(store, "checkout", "-q", "-B", "aq/multi", main)
    for name in ("multi-a.txt", "multi-b.txt"):
        (store / name).write_text(name)
        git(store, "add", "-A")
        git(store, "commit", "-q", "-m", f"add {name}")
    commits = git(store, "rev-list", "--reverse", f"{main}..HEAD").split()
    assert len(commits) == 2
    git(store, "checkout", "-q", "-B", "aq/multi-target", main)
    git(store, "merge", "-q", "--no-ff", "-m",
        f"Integrate multi\n\nAQ-Source: multi@{commits[0]}", "aq/multi")
    multi = git(store, "rev-parse", "HEAD")
    assert await delivered_by_source_trailer(
        git_manager, store, "multi", commits[0], multi
    ) is True
    assert await delivered_by_source_trailer(
        git_manager, store, "multi", commits[1], multi
    ) is False


def test_no_runtime_module_reads_or_writes_a_delivery_record():
    """Retired for good: no table answers delivery and no source queries one.

    Docs and comments may still name the table in prose; a Python identifier,
    import or SQL reference to it is a reintroduced receipt reader or writer.
    """
    import re
    from pathlib import Path

    from src.database import tables

    assert "development_deliveries" not in tables.metadata.tables
    assert not hasattr(tables, "development_deliveries")
    usage = re.compile(
        r"development_deliveries(?:\.c\b|\.alias\(|\s*[,)]|\s*$)"
        r"|\bimport\s+development_deliveries\b"
        r"|(?i:\b(?:from|into|update|join|table)\s+\"?development_deliveries\b)"
    )
    src = Path(__file__).resolve().parent.parent / "src"
    offenders = sorted(
        f"{path.relative_to(src.parent)}:{number}"
        for path in src.rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if usage.search(line)
    )
    assert offenders == []


async def test_registered_prerequisite_observer_releases_pool_demand_without_receipts(
    world, tmp_path,
):
    """The daemon's delivery observer has no GitTruth; git-first registers one for prerequisites."""
    from src.database.tables import task_branch_origins
    from src.integration.delivery_observer import hierarchy_frontier_modes, prerequisite_observer
    from src.integration.git_truth import GitTruth
    from src.models import AgentProfile

    db, origin, _observer, _service = world
    await db.create_profile(AgentProfile(id="worker", name="worker"))
    await db.create_task(Task(id="dependent", project_id="p", repo_id="r", title="dependent",
                              description="", branch_name="aq/dependent", status=TaskStatus.READY))
    await db.add_dependency("dependent", "epic", "parent-child")
    await db.add_dependency("dependent", "done")
    git(origin.clone, "push", "origin", "main:aq/epic")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/epic", repo_id="r"))
        await conn.execute(update(tasks).where(tasks.c.id == "dependent").values(
            is_blocked=False, profile_id="worker", route_source="router",
        ))
        await conn.execute(insert(task_branch_origins).values(
            id="dependent-origin", task_id="dependent", repository_id="r",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="r",
            parent_ref="aq/epic", base_sha=git(origin.clone, "rev-parse", "main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time(),
        ))
    await db.update_project("p", hierarchical_integration_mode="train")

    # Production shape: a truthless general observer (shadow) only.
    transport = GitManager()
    db.set_delivery_observer(DeliveryObserver(db, git=transport, data_dir=tmp_path / "general"))
    assert prerequisite_observer(db) is None
    assert "worker" not in await db.count_ready_by_profile("p")

    truthful = DeliveryObserver(db, git=transport, data_dir=tmp_path / "prereq",
                                truth=GitTruth(transport))
    db.set_prerequisite_observer(truthful)
    assert prerequisite_observer(db) is truthful
    # A diagnostic miss describes uncertainty without fetching or changing
    # the actual claim frontier. The next decision independently loads Git.
    truthful._store = AsyncMock(wraps=truthful._store)
    cold = await db.claim_frontier_exclusions("dependent", cached_only=True)
    assert [reason["code"] for reason in cold] == ["delivery_evidence_unavailable"]
    truthful._store.assert_not_awaited()
    assert (await db.count_ready_by_profile("p")).get("worker") == 1
    truthful._store.assert_awaited()
    modes = await hierarchy_frontier_modes(db, project_ids={"p"})
    assert modes["p"].delivered_prerequisite_ids == frozenset({"done"})
    assert (await db.count_ready_by_profile("p")).get("worker") == 1
    assert not any(r["code"] == "frontier_sibling_prerequisite_not_delivered"
                   for r in await db.claim_frontier_exclusions("dependent"))
    assert await db.hierarchy_prerequisite_delivery_head("dependent") == git(
        origin.clone, "rev-parse", "main",
    )
    # Reopening after observation must not return a former parent head.
    await db.transition_task("done", TaskStatus.IN_PROGRESS, force=True)
    with pytest.raises(ValueError, match="delivery is not current"):
        await db.hierarchy_prerequisite_delivery_head("dependent")


@pytest.mark.parametrize("reduced", [False, True])
async def test_cached_graph_observation_never_fetches_and_rechecks_identity(world, tmp_path, reduced):
    from src.integration.git_truth import GitTruth

    db, _origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(
        db, git=transport, data_dir=tmp_path / "cached-only",
        truth=GitTruth(transport) if reduced else None,
    )
    # A cold interactive read cannot even create a Git store, much less fetch.
    original_store = observer._store
    observer._store = AsyncMock(side_effect=AssertionError("graph attempted network Git"))
    cold = await observer.observe(["done"], max_age=30, cached_only=True)
    assert cold.get("done").state is DeliveryState.UNKNOWN
    assert cold.get("done").reason == "snapshot_unavailable"
    observer._store.assert_not_awaited()

    # A normal background/guarded observation populates the existing cache.
    observer._store = original_store
    await observer.observe(["done"])
    observer._store = AsyncMock(side_effect=AssertionError("graph attempted network Git"))
    warm = await observer.observe(["done"], max_age=30, cached_only=True)
    assert warm.satisfied("done")
    async with db._engine.connect() as conn:
        assert (await warm.verified_on(conn, ["done"]))["done"].satisfied
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "done").values(status="READY"))
    async with db._engine.connect() as conn:
        assert "done" not in await warm.verified_on(conn, ["done"])

    # Expiry never serves an old delivered answer and never refreshes on read.
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "done").values(status="COMPLETED"))
    observer._recent = {key: (0, value[1]) for key, value in observer._recent.items()}
    expired = await observer.observe(["done"], max_age=30, cached_only=True)
    assert expired.get("done").reason == "snapshot_unavailable"
    assert not expired.satisfied("done")
    observer._store.assert_not_awaited()


async def test_multi_target_fetch_warms_every_target_without_renewing_age(world, tmp_path):
    from src.integration.delivery_observer import delivery_targets
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    git(origin.clone, "push", "origin", "main:aq/epic", "main:aq/other")
    await db.create_task(Task(
        id="other-epic", project_id="p", repo_id="r", title="other", description="",
        branch_name="aq/other",
    ))
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/epic", repo_id="r"))
        await conn.execute(update(tasks).where(tasks.c.id == "reopened")
                           .values(parent_task_id="other-epic"))
        await conn.execute(update(tasks).where(tasks.c.id == "missing")
                           .values(parent_task_id=None))
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "multi-target",
                                truth=GitTruth(transport))
    observer.truth.snapshot = AsyncMock(wraps=observer.truth.snapshot)
    ids = {"done", "reopened", "missing"}
    view = await observer.observe(ids)
    assert len(view.snapshots) == 3
    observer.truth.snapshot.assert_awaited_once()
    assert view.satisfied("done")
    async with db._engine.connect() as conn:
        targets = await delivery_targets(conn, ids, reduced=True)
    stamps = {observer._recent[target][0] for target in targets.values()}
    assert len(stamps) == 1

    observer._store = AsyncMock(side_effect=AssertionError("display attempted fetch"))
    warm = await observer.observe(ids, max_age=30, cached_only=True)
    assert {tid: (proof.state, proof.reason) for tid, proof in warm.evidence.items()} == {
        tid: (proof.state, proof.reason) for tid, proof in view.evidence.items()
    }
    assert {observer._recent[target][0] for target in targets.values()} == stamps
    observer._store.assert_not_awaited()
    observer.truth.snapshot.assert_awaited_once()


async def test_cache_miss_does_not_hide_another_targets_warm_evidence(world, tmp_path):
    from dataclasses import replace

    from src.integration.delivery_observer import DeliveryTarget
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "mixed-targets",
                                truth=GitTruth(transport))
    await observer.observe(["done"])
    main = DeliveryTarget("p", "r", origin.url, "refs/heads/main")

    async def targets(_conn, _ids):
        # The cold target deliberately comes first in this repository.
        return {"missing": replace(main, target_ref="refs/heads/absent"), "done": main}

    observer._store = AsyncMock(side_effect=AssertionError("display attempted fetch"))
    view = await observer.observe(["missing", "done"], target_loader=targets,
                                  max_age=30, cached_only=True)
    assert view.get("missing").reason == "snapshot_unavailable"
    assert view.satisfied("done")
    observer._store.assert_not_awaited()


async def test_missing_target_fetch_warms_peers_but_decisions_refetch_it(world, tmp_path):
    from src.integration.delivery_observer import DeliveryTarget
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "missing-target",
                                truth=GitTruth(transport))
    missing = DeliveryTarget("p", "r", origin.url, "refs/heads/absent")
    observer.truth.snapshot = AsyncMock(wraps=observer.truth.snapshot)
    assert (await observer.snapshot(missing)).observation.error == "missing_target"
    observer.truth.snapshot.assert_awaited_once()
    warm = await observer.observe(["done"], max_age=30, cached_only=True)
    assert warm.satisfied("done")
    assert (await observer.snapshot(missing, max_age=30, cached_only=True)
            ).observation.error == "missing_target"
    observer.truth.snapshot.assert_awaited_once()
    assert (await observer.snapshot(missing, max_age=30)).observation.error == "missing_target"
    assert observer.truth.snapshot.await_count == 2


async def test_frontier_cache_uncertainty_preserves_known_delivery_exclusions(world, tmp_path):
    from src.database.tables import task_branch_origins
    from src.integration.delivery_observer import DeliveryTarget, hierarchy_frontier_modes
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    git(origin.clone, "push", "origin", "main:aq/epic")
    await db.create_task(Task(
        id="dependent", project_id="p", repo_id="r", title="dependent", description="",
        status=TaskStatus.READY, branch_name="aq/dependent",
    ))
    await db.add_dependency("dependent", "epic", "parent-child")
    await db.add_dependency("dependent", "done")
    await db.add_dependency("dependent", "reopened")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic")
                           .values(branch_name="aq/epic", repo_id="r"))
        await conn.execute(update(tasks).where(tasks.c.id == "done").values(parent_task_id=None))
        await conn.execute(update(tasks).where(tasks.c.id == "dependent").values(is_blocked=False))
        await conn.execute(insert(task_branch_origins).values(
            id="dependent-origin", task_id="dependent", repository_id="r",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="r",
            parent_ref="aq/epic", base_sha=git(origin.clone, "rev-parse", "main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time(),
        ))
    await db.update_project("p", hierarchical_integration_mode="train")
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "frontier-mixed",
                                truth=GitTruth(transport))
    db.set_prerequisite_observer(observer)
    await hierarchy_frontier_modes(db, project_ids={"p"})
    main = DeliveryTarget("p", "r", origin.url, "refs/heads/main")
    observer._recent[main] = (0, observer._recent[main][1])
    observer._store = AsyncMock(side_effect=AssertionError("display attempted fetch"))
    reasons = await db.claim_frontier_exclusions("dependent", cached_only=True)
    codes = {reason["code"] for reason in reasons}
    assert "frontier_sibling_prerequisite_not_delivered" in codes
    assert "frontier_prerequisite_not_on_default_branch" not in codes
    assert "prerequisite_not_on_default_branch" not in codes
    assert "delivery_evidence_unavailable" in codes
    assert any(reason["ref"] == "done" and reason["code"] == "delivery_evidence_unavailable"
               for reason in reasons)
    observer._store.assert_not_awaited()


async def test_frontier_freshness_failure_does_not_poison_unchecked_targets(
    world, monkeypatch,
):
    from src.integration.delivery_observer import (
        DeliveryTarget, DeliveryView, PrerequisiteView, hierarchy_frontier_modes,
    )
    from src.integration.delivery_truth import DeliverySnapshot
    from src.integration.git_truth import GitTruth

    db, origin, observer, _service = world
    git(origin.clone, "push", "origin", "main:aq/peer")
    main = DeliveryTarget("p", "r", origin.url, "refs/heads/main")
    peer = DeliveryTarget("p", "r", origin.url, "refs/heads/aq/peer")
    await observer.snapshot(main)
    first = observer._recent[main][1]
    unchecked = observer._recent[peer][1]
    assert not first._freshness
    remote = AsyncMock(return_value=False)
    monkeypatch.setattr(DeliverySnapshot, "_remote_is_fresh", remote)
    observer.truth = GitTruth(observer.git)
    db.set_prerequisite_observer(observer)
    await db.update_project("p", hierarchical_integration_mode="train")
    observer.prerequisite_view = AsyncMock(return_value=PrerequisiteView(
        DeliveryView(db, snapshots=(first, unchecked)), DeliveryView(db),
    ))
    modes = await hierarchy_frontier_modes(db, project_ids={"p"})
    assert modes["p"].delivered_prerequisite_ids == frozenset()
    remote.assert_awaited_once()
    assert first._freshness == {main.target_ref: False}
    assert peer.target_ref not in unchecked._freshness
    assert (await observer._snapshot(peer, max_age=30, cached_only=True)).error is None
