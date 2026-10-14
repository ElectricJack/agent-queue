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

import asyncio
import json
import subprocess
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import events, projects, tasks
from src.git.manager import GitError, GitManager
from src.integration.delivery_observer import DeliveryObserver
from src.integration.delivery_truth import DeliveryState
from src.integration.development import DevelopmentPrimitives
from src.integration.status import IntegrationStatusService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.git_seed_helpers import clone_seed, init_bare_seed

CHILDREN = ("done", "wrong", "missing", "reopened")


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class Origin:
    def __init__(self, tmp_path):
        self.url = str(tmp_path / "origin.git")
        self.clone = tmp_path / "clone"
        init_bare_seed(self.url)
        clone_seed(self.url, self.clone)
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


def test_fresh_origins_do_not_launch_auto_maintenance(tmp_path, monkeypatch):
    trace = tmp_path / "git-trace.json"
    config = tmp_path / "empty-git-config"
    config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    first_root = tmp_path / "first"
    first_root.mkdir()
    first = Origin(first_root)
    first_head = git(first.url, "rev-parse", "main")
    second = Origin(tmp_path)
    second_head = second.work("private-work")
    assert git(first.url, "rev-parse", "main") == first_head
    assert git(first.url, "for-each-ref", "refs/heads/aq/private-work") == ""
    assert git(second.url, "rev-parse", "aq/private-work") == second_head
    commands = [json.loads(line).get("argv", []) for line in trace.read_text().splitlines()]
    assert not [
        args for args in commands
        if "--auto" in args and ("maintenance" in args or "gc" in args)
    ]


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
async def world(tmp_path, reuse_database):
    origin = Origin(tmp_path)
    db = await reuse_database("delivery-consumers.db")
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
    from src.integration.service import IntegrationService
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
    orch.integration_service = IntegrationService(db, None, development_subject_runtime=object())
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


@pytest.mark.parametrize("warm", [False, True])
async def test_cached_delivery_read_never_waits_for_an_owned_fetch_lock(world, warm):
    from src.integration.delivery_observer import DeliveryTarget, _fetch_lock

    _db, origin, observer, _service = world
    if warm:
        await observer.observe(["done"])
    store = await observer._store(DeliveryTarget("p", "r", origin.url, "refs/heads/main"))
    lock = _fetch_lock(store)
    async with lock:
        # This guards a deadlock boundary, not a performance budget. The
        # held lock must remain owned until the diagnostic returns.
        view = await asyncio.wait_for(
            observer.observe(["done"], max_age=observer.READ_MAX_AGE, cached_only=True), 10,
        )
        assert lock.locked()
        if warm:
            assert view.get("done").satisfied
        else:
            assert view.get("done").reason == "snapshot_unavailable"
            assert not view.get("done").satisfied


@pytest.mark.parametrize("failure", ["paused", "train_missing", "stopped", "service_missing"])
async def test_explain_names_missing_engine_for_undelivered_prerequisite(
    world, tmp_path, failure,
):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig
    from src.integration.service import IntegrationService
    from src.orchestrator import Orchestrator

    db, _origin, observer, primitives = world
    primitives.git = observer.git
    primitives._delivery_observer = observer
    config = AppConfig(database=DatabaseConfig(url=lease_dsn("delivery-consumers.db")),
                       data_dir=str(tmp_path / "data"))
    orch = Orchestrator(config)
    orch.db, orch.git, orch.development_integration = db, observer.git, primitives
    if failure == "paused":
        config.integration.reconciler_active = False
    elif failure == "train_missing":
        config.integration.git_first = "active"
    elif failure == "stopped":
        orch.integration_service = IntegrationService(None, None, train=object())
        orch.integration_service._stop_event.set()
    await db.create_task(Task(
        id="waiting", project_id="p", repo_id="r", title="waiting", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("waiting", "missing")
    result = await CommandHandler(orch, config)._cmd_explain_task({"task_id": "waiting"})
    delivery = [reason for reason in result["reasons"]
                if reason["code"] == "delivery_engine_unavailable"]
    assert len(delivery) == 1
    assert delivery[0]["ref"] == "missing"
    assert "no delivery engine for this project" in delivery[0]["detail"]
    assert "development_dependency_delivery" not in result["reason_codes"]
    assert "delivery_evidence_unavailable" not in result["reason_codes"]


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
    world, tmp_path, monkeypatch,
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
    with monkeypatch.context() as display:
        run = AsyncMock(side_effect=AssertionError("frontier diagnostic launched Git"))
        display.setattr(transport, "arun_git_result", run)
        cached = await hierarchy_frontier_modes(db, project_ids={"p"}, cached_only=True)
        assert cached["p"].delivered_prerequisite_ids == frozenset({"done"})
        assert not any(r["code"] == "frontier_sibling_prerequisite_not_delivered"
                       for r in await db.claim_frontier_exclusions("dependent", cached_only=True))
        run.assert_not_awaited()
    # Delivered identity is fenced, including a forced reopen. Simulate an
    # out-of-band status change to retain the stale-observation regression.
    with pytest.raises(HierarchyError, match="delivery_target_fixed"):
        await db.transition_task("done", TaskStatus.IN_PROGRESS, force=True)
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "done")
                           .values(status=TaskStatus.IN_PROGRESS.value))
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
    if reduced:
        transport.arun_git_result = AsyncMock(side_effect=AssertionError("display launched Git"))
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


async def test_delivery_freshness_batches_all_targets_and_marks_moved_refs(world, tmp_path):
    from dataclasses import replace

    from src.integration.delivery_observer import DeliveryTarget, DeliveryView, PrerequisiteView
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "batched-freshness",
                                truth=GitTruth(transport))
    for branch in ("first", "second"):
        git(origin.clone, "push", "origin", f"main:aq/{branch}")
    root = DeliveryTarget("p", "r", origin.url, "refs/heads/main")
    original = await observer.snapshot(root)
    second = replace(root, target_ref="refs/heads/aq/second")
    other = await observer.snapshot(second)
    snapshots = tuple(original.for_target(ref).observation for ref in (
        "refs/heads/main", "refs/heads/aq/first",
    )) + (other.observation,)
    refs = AsyncMock(wraps=transport.als_remote_refs)
    transport.als_remote_refs = refs
    transport.als_remote_ref = AsyncMock(side_effect=AssertionError("per-target freshness"))
    view = PrerequisiteView(DeliveryView(snapshots=snapshots),
                            DeliveryView(snapshots=snapshots), {})
    assert await view.fresh()
    assert refs.await_count == 1
    assert set(refs.call_args.args[1]) == {"main", "aq/first", "aq/second"}
    git(origin.clone, "push", "origin", ":refs/heads/aq/second")
    assert not await view.fresh()
    assert refs.await_count == 2
    assert snapshots[2]._freshness["refs/heads/aq/second"] is False
    unavailable = await observer.snapshot(second, max_age=30, cached_only=True)
    assert unavailable.error == "snapshot_unavailable"


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
    assert {tid: (proof.state, proof.reason) for tid, proof in warm.evidence.items()
            if tid != "missing"} == {
        tid: (proof.state, proof.reason) for tid, proof in view.evidence.items()
        if tid != "missing"
    }
    assert view.get("missing").reason == "missing_git_provenance"
    assert warm.get("missing").state is view.get("missing").state
    assert warm.get("missing").reason == "proof_unavailable"
    assert not warm.satisfied("missing")
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
    assert (await observer.snapshot(missing)).observation.error == "target_ref_missing"
    observer.truth.snapshot.assert_awaited_once()
    warm = await observer.observe(["done"], max_age=30, cached_only=True)
    # A fetched peer ref warms its observation, but diagnostics cannot build
    # a proof that no background reader has evaluated yet.
    assert warm.get("done").reason == "proof_unavailable"
    assert not warm.satisfied("done")
    assert (await observer.observe(["done"], max_age=30)).satisfied("done")
    warm = await observer.observe(["done"], max_age=30, cached_only=True)
    assert warm.satisfied("done")
    assert (await observer.snapshot(missing, max_age=30, cached_only=True)
            ).observation.error == "target_ref_missing"
    observer.truth.snapshot.assert_awaited_once()
    assert (await observer.snapshot(missing, max_age=30)).observation.error == "target_ref_missing"
    assert observer.truth.snapshot.await_count == 2


async def test_integrity_absence_never_replaces_a_peer_with_an_incomplete_inventory(world, tmp_path):
    from src.integration.delivery_observer import DeliveryTarget, _for_target
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "incomplete-inventory",
                                truth=GitTruth(transport))
    main = DeliveryTarget("p", "r", origin.url, "refs/heads/main")
    missing = DeliveryTarget("p", "r", origin.url, "refs/heads/absent")
    assert (await observer.snapshot(main)).target_oid
    warm = observer._recent[main]
    failure = "retained Git object integrity is unavailable"
    transport.afetch_origin = AsyncMock(side_effect=GitError(failure))
    absent = (await observer.snapshot(missing)).observation
    assert absent.error == "target_ref_missing"
    assert absent.acquisition_error == "snapshot_git_error: " + failure
    assert observer._recent[main] is warm
    peer = _for_target(absent, main)
    assert peer.error == "snapshot_git_error: " + failure
    assert peer.target_oid is None
    assert _for_target(peer, missing).error == "target_ref_missing"


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
        DeliveryTarget,
        DeliveryView,
        PrerequisiteView,
        hierarchy_frontier_modes,
    )
    from src.integration.git_truth import GitTruth

    db, origin, observer, _service = world
    git(origin.clone, "push", "origin", "main:aq/peer", "main:aq/unchecked")
    main = DeliveryTarget("p", "r", origin.url, "refs/heads/main")
    peer = DeliveryTarget("p", "r", origin.url, "refs/heads/aq/peer")
    uninspected = DeliveryTarget("p", "r", origin.url, "refs/heads/aq/unchecked")
    await observer.snapshot(main)
    first = observer._recent[main][1]
    current_peer = observer._recent[peer][1]
    unchecked = observer._recent[uninspected][1]
    assert not first._freshness
    origin.work("advance-main")
    origin.land("advance-main")
    remote = AsyncMock(wraps=observer.git.als_remote_refs)
    monkeypatch.setattr(observer.git, "als_remote_refs", remote)
    observer.truth = GitTruth(observer.git)
    db.set_prerequisite_observer(observer)
    await db.update_project("p", hierarchical_integration_mode="train")
    observer.prerequisite_view = AsyncMock(return_value=PrerequisiteView(
        DeliveryView(db, snapshots=(first, current_peer)), DeliveryView(db),
    ))
    modes = await hierarchy_frontier_modes(db, project_ids={"p"})
    assert modes["p"].delivered_prerequisite_ids == frozenset()
    remote.assert_awaited_once()
    assert set(remote.call_args.args[1]) == {"main", "aq/peer"}
    assert first._freshness[main.target_ref] is False
    assert current_peer._freshness[peer.target_ref] is True
    assert uninspected.target_ref not in unchecked._freshness
    assert (await observer._snapshot(peer, max_age=30, cached_only=True)).error is None
    assert (await observer._snapshot(uninspected, max_age=30, cached_only=True)).error is None


@pytest.fixture
async def active_git_world(world, tmp_path):
    """Production observer registration, real Git, and no delivery receipts."""
    from sqlalchemy import select

    from src.database.tables import task_branch_origins, task_delivery_receipts
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "active",
                                truth=GitTruth(transport))
    db.set_prerequisite_observer(observer)
    git(origin.clone, "push", "origin", "main:aq/epic")
    base = git(origin.clone, "rev-list", "--max-parents=0", "origin/main")
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_mode="train", hierarchical_integration_desired_mode="train",
        ))
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(
            branch_name="aq/epic", repo_id="r",
        ))
        for tid in ("done", "reopened"):
            await conn.execute(insert(task_branch_origins).values(
                id=f"origin-{tid}", task_id=tid, repository_id="r", branch_name=f"aq/{tid}",
                parent_task_id="epic", parent_repository_id="r", parent_ref="aq/epic",
                base_sha=base, creation_generation=0, reserved=True, materialized=True,
                created_at=time.time(),
            ))
        assert not (await conn.execute(select(task_delivery_receipts))).first()
    yield db, origin, observer
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(task_delivery_receipts))).first()


async def test_git_first_archive_uses_default_containment_without_receipts(active_git_world):
    db, origin, _observer = active_git_world
    assert await db.archive_task("done")
    # Its parent now contains the new child source, but main still does not.
    git(origin.clone, "checkout", "-q", "-B", "aq/epic", "origin/main")
    git(origin.clone, "merge", "-q", "--no-ff", "-m", "Collect reopened", "aq/reopened")
    git(origin.clone, "push", "origin", "aq/epic")
    with pytest.raises(HierarchyError) as refused:
        await db.archive_task("reopened")
    assert refused.value.code == "integration_undelivered"
    assert (await db.get_task("reopened")).status is TaskStatus.COMPLETED


async def test_git_first_archive_revalidates_target_without_receipts(active_git_world):
    db, _origin, _observer = active_git_world
    view = await db.observe_removal_delivery(["done"])
    await db.update_repo("r", default_branch="elsewhere")
    with pytest.raises(HierarchyError) as refused:
        await db.archive_task("done", delivery=view)
    assert refused.value.code == "integration_undelivered"


async def test_git_first_mutation_fences_delivered_subtrees_without_receipts(active_git_world):
    db, _origin, _observer = active_git_world
    with pytest.raises(HierarchyError) as refused:
        await db.transition_task("done", TaskStatus.IN_PROGRESS, force=True)
    assert refused.value.code == "delivery_target_fixed"
    assert (await db.get_task("done")).status is TaskStatus.COMPLETED
    # An undelivered current completion remains reopenable.
    await db.transition_task("reopened", TaskStatus.IN_PROGRESS, force=True)
    assert (await db.get_task("reopened")).status is TaskStatus.IN_PROGRESS


async def test_git_first_mutation_rejects_stale_view_without_receipts(active_git_world):
    db, _origin, _observer = active_git_world
    view = await db.observe_removal_delivery(["reopened"], for_mutation=True)
    await db.update_task("epic", branch_name="aq/changed")
    async with db.immediate() as conn:
        with pytest.raises(HierarchyError) as refused:
            await db.guard_integration_mutation("reopened", "reopen", conn=conn, delivery=view)
    assert refused.value.code == "delivery_unknown"


async def test_git_first_mutation_rejects_unknown_proof_without_receipts(active_git_world):
    db, _origin, _observer = active_git_world
    # Git contains the source, but the task is bound to a foreign repository.
    with pytest.raises(HierarchyError) as refused:
        await db.transition_task("wrong", TaskStatus.IN_PROGRESS, force=True)
    assert refused.value.code == "delivery_unknown"
    assert (await db.get_task("wrong")).status is TaskStatus.COMPLETED


async def reopen_command(db, task_id):
    from src.commands.task_commands import TaskCommandsMixin

    handler = TaskCommandsMixin()
    handler.db, handler._current_scope = db, {}
    return await handler._cmd_reopen_with_feedback({
        "task_id": task_id, "feedback": "Recover the unlabelled completion; retain the branch",
    })


@pytest.mark.parametrize("caller", [
    "operator", "supervisor", "reviewer", "service", "stopped_supervisor", "foreign_supervisor",
])
async def test_missing_provenance_reopen_requires_operator_control(
    active_git_world, monkeypatch, caller,
):
    from types import SimpleNamespace

    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    db, origin, _observer = active_git_world
    head = origin.work("missing")
    if caller.endswith("supervisor"):
        monkeypatch.setattr(db, "get_session", AsyncMock(return_value=SimpleNamespace(
            id=caller, profile_id="supervisor", lifecycle="named",
            state="stopped" if caller == "stopped_supervisor" else "running",
            desired_state="running", project_id="other" if caller == "foreign_supervisor" else "p",
        )))
    principal = ExecutionPrincipal(
        kind=(PrincipalKind.LOCAL if caller == "operator" else
              PrincipalKind.SERVICE if caller == "service" else PrincipalKind.SESSION),
        policy=DENY_ALL, session_id=caller, project_id="p", elevated=caller.endswith("supervisor"),
    )
    before = await db.get_task("missing")
    # Generic lifecycle writes still cannot make the missing close claimable.
    with pytest.raises(HierarchyError, match="delivery_unknown"):
        await db.transition_task("missing", TaskStatus.READY, context="reopen_with_feedback")
    with principal_context(principal):
        if caller in {"operator", "supervisor"}:
            assert (await reopen_command(db, "missing"))["status"] == "READY"
        else:
            with pytest.raises(HierarchyError, match="delivery_unknown"):
                await reopen_command(db, "missing")
    task = await db.get_task("missing")
    assert task.status is (TaskStatus.READY if caller in {"operator", "supervisor"}
                           else TaskStatus.COMPLETED)
    assert task.branch_name == before.branch_name
    assert git(origin.url, "rev-parse", "aq/missing") == head
    assert (await db.get_task_completion("missing")).id == "close-missing"


async def test_unlabelled_subepic_reopens_preserving_collected_children(active_git_world):
    from src.database.tables import task_integration_checkpoints

    db, origin, observer = active_git_world
    origin.work("subepic")
    await db.create_task(Task(
        id="subepic", project_id="p", repo_id="r", title="Subepic", description="",
        branch_name="aq/subepic", parent_task_id="epic", status=TaskStatus.COMPLETED,
    ))
    child_head = git(origin.clone, "rev-parse", "origin/aq/reopened")
    git(origin.clone, "merge", "-q", "--no-ff", "-m", "Collect child", "origin/aq/reopened")
    git(origin.clone, "push", "-q", "origin", "aq/subepic")
    head = git(origin.clone, "rev-parse", "HEAD")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "reopened").values(
            parent_task_id="subepic",
        ))
        for tid in ("epic", "subepic"):
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id=tid, repository_id="r", branch=f"aq/{tid}", checkpoint_sha=head,
                generation=4, verified_sha=head, verified_generation=4, updated_at=1.0,
            ))
    view = await observer.observe(["subepic", "reopened"])
    assert view.get("subepic").reason == "missing_git_provenance"
    assert view.get("reopened").state is DeliveryState.CONTAINED
    assert (await reopen_command(db, "subepic"))["status"] == "READY"
    assert (await db.get_task("subepic")).status is TaskStatus.READY
    assert (await db.get_task("reopened")).status is TaskStatus.COMPLETED
    assert git(origin.url, "rev-parse", "aq/subepic") == head
    assert git(origin.url, "rev-parse", "aq/reopened") == child_head
    for tid in ("epic", "subepic"):
        checkpoint = await db.get_integration_checkpoint(tid)
        assert checkpoint["generation"] == 5
        assert checkpoint["verified_sha"] is None
        assert checkpoint["verified_generation"] is None
    # Reopening has not manufactured provenance or a passing new completion.
    assert await db.get_task_completion("subepic") is None


@pytest.mark.parametrize("obstacle", ["stale", "wrong_repository", "descendant"])
async def test_missing_provenance_recovery_preserves_delivery_fences(
    active_git_world, obstacle,
):
    db, origin, _observer = active_git_world
    origin.work("missing")
    view = await db.observe_removal_delivery(["missing"], for_mutation=True)
    if obstacle == "stale":
        await db.update_task("epic", branch_name="aq/retargeted")
    elif obstacle == "wrong_repository":
        await db.update_task("missing", repo_id="web")
        view = await db.observe_removal_delivery(["missing"], for_mutation=True)
    elif obstacle == "descendant":
        await db.create_task(Task(
            id="unknown-child", project_id="p", repo_id="r", title="Child", description="",
            branch_name="aq/unknown-child", parent_task_id="missing", status=TaskStatus.COMPLETED,
        ))
        view = await db.observe_removal_delivery(["missing"], for_mutation=True)
    with pytest.raises(HierarchyError) as refused:
        await db.transition_task(
            "missing", TaskStatus.READY, context="reopen_with_feedback",
            integration_delivery=view, allow_missing_provenance_reopen=True,
        )
    assert refused.value.code == "delivery_unknown"
    assert (await db.get_task("missing")).status is TaskStatus.COMPLETED


async def test_historical_receipt_does_not_fix_missing_source_identity(active_git_world):
    from sqlalchemy import select

    from src.database.tables import task_delivery_receipts

    db, origin, _observer = active_git_world
    origin.work("missing")
    view = await db.observe_removal_delivery(["missing"], for_mutation=True)
    # Roll back the audit row after proving it has no eligibility authority.
    with pytest.raises(RuntimeError, match="rollback audit fixture"):
        async with db.immediate() as conn:
            await conn.execute(insert(task_delivery_receipts).values(
                id="external", domain_key="external", source_task_id="missing",
                target_task_id="epic", repository_id="r", target_branch="aq/epic",
                disposition="code", created_at=1.0,
            ))
            await db._apply_transition(
                conn, "missing", TaskStatus.READY, context="reopen_with_feedback",
                integration_delivery=view, allow_missing_provenance_reopen=True,
            )
            assert await conn.scalar(select(tasks.c.status).where(tasks.c.id == "missing")) == "READY"
            raise RuntimeError("rollback audit fixture")



async def test_git_first_reparent_guard_refuses_completed_work_without_receipts(active_git_world):
    """Completed work fixes the target even before the delivery evidence fence."""
    from src.integration.hierarchy import HierarchyIntegration

    db, _origin, observer = active_git_world
    view = await observer.observe(["done"])
    hierarchy = HierarchyIntegration(db)
    repo = await db.get_repo("r")
    assert repo is not None
    assert (await db.get_task("done")).status is TaskStatus.COMPLETED
    async with db.immediate() as conn:
        with pytest.raises(HierarchyError) as refused:
            await hierarchy._assert_reparentable(conn, "done", repo, delivery=view)
    assert refused.value.code == "delivery_target_fixed"
    assert refused.value.detail == "task has a holder or completed work"


@pytest.mark.parametrize("contained", [True, False])
async def test_git_first_child_redrive_reads_parent_without_receipts(active_git_world, contained):
    from src.integration.child_delivery import ChildDelivery

    db, _origin, _observer = active_git_world
    # The parent has no PAUSED collection episode; no legacy service is needed.
    assert (await db.get_task("epic")).status is not TaskStatus.PAUSED
    delivery = ChildDelivery(db, None, collect=AsyncMock(side_effect=AssertionError("legacy collect")))
    result = await delivery.run("done" if contained else "reopened", dry_run=False)
    assert result["outcome"] == "nothing_to_redrive"
    assert ("already delivered" if contained else "train owns delivery") in result["reason"]
    delivery.collect.assert_not_awaited()
    with pytest.raises(HierarchyError, match="never reissues receipts"):
        await delivery._reissue_receipt({}, "old")


async def _make_delivered_root(db, origin):
    from sqlalchemy import delete

    from src.database.tables import task_dependencies, task_integration_checkpoints

    head = git(origin.clone, "rev-parse", "origin/aq/done")
    async with db._engine.begin() as conn:
        await conn.execute(delete(task_dependencies).where(
            task_dependencies.c.task_id == "done", task_dependencies.c.dep_type == "parent-child",
        ))
        await conn.execute(update(tasks).where(tasks.c.id == "done").values(
            parent_task_id=None, pr_url="https://example.test/pull/1", updated_at=time.time() - 7200,
        ))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="done", repository_id="r", branch="aq/done", checkpoint_sha=head,
            generation=0, updated_at=time.time() - 7200,
        ))
    await db.create_task(Task(id="done-child", project_id="p", title="organizational",
                              description="", status=TaskStatus.COMPLETED, parent_task_id="done"))


@pytest.mark.parametrize("warm", [True, False])
async def test_git_first_task_show_reports_delivered_root_without_receipts(
    active_git_world, tmp_path, monkeypatch, warm,
):
    from types import SimpleNamespace

    from src.commands.handler import CommandHandler
    from src.config import AppConfig

    db, origin, observer = active_git_world
    await _make_delivered_root(db, origin)
    if warm:
        await observer.observe(["done"])
    run = AsyncMock(side_effect=AssertionError("task show launched Git"))
    monkeypatch.setattr(observer.git, "arun_git_result", run)
    config = AppConfig(data_dir=str(tmp_path))
    config.integration.git_first = "active"
    handler = CommandHandler(SimpleNamespace(db=db, config=config), config)
    db.list_integration_delivery_receipts = AsyncMock(side_effect=AssertionError("receipt read"))
    result = await handler._cmd_task_show({"task_id": "done"})
    assert result["integration_delivery"]["outcome"] == ("delivered" if warm else "working")
    assert result["integration_delivery"]["receipts"] == []
    db.list_integration_delivery_receipts.assert_not_awaited()
    run.assert_not_awaited()


async def test_git_first_doctor_ignores_delivered_root_without_receipts(active_git_world, monkeypatch):
    import importlib

    from src.config import AppConfig
    from src.doctor.models import DoctorContext
    from src.doctor.stall_checks import _find_unadmitted_parents

    db, origin, _observer = active_git_world
    await _make_delivered_root(db, origin)
    observed = AsyncMock(side_effect=AssertionError("delivered root probed for admission"))
    monkeypatch.setattr(importlib.import_module("src.doctor.stall_checks"),
                        "_parent_pr_observation", observed)
    config = AppConfig()
    config.integration.git_first = "active"
    ctx = DoctorContext(db=db, config=config)
    assert await _find_unadmitted_parents(ctx) == []
    observed.assert_not_awaited()


async def test_git_first_epic_worker_frontier_uses_cached_git_modes_without_receipts(active_git_world):
    from src.database.tables import task_branch_origins
    from src.integration.delivery_observer import hierarchy_frontier_modes
    from src.integration.epic_delivery import EpicDeliveryProjection

    db, origin, _observer = active_git_world
    await db.create_task(Task(id="dependent", project_id="p", title="dependent", description="",
                              repo_id="r", branch_name="aq/dependent", status=TaskStatus.READY))
    async with db._engine.begin() as conn:
        await db.set_parent("dependent", "epic", conn=conn, integration_authorized=True)
    await db.add_dependency("dependent", "done")
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="dependent-origin", task_id="dependent", repository_id="r",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="r",
            parent_ref="aq/epic", base_sha=git(origin.clone, "rev-parse", "main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time(),
        ))
    await hierarchy_frontier_modes(db, project_ids={"p"})
    async with db._engine.connect() as conn:
        workers = await EpicDeliveryProjection(db)._workers_on(conn, ["dependent"])
    assert "sibling_prerequisite_not_delivered" not in workers["dependent"].exclusions


async def test_missing_git_observer_warns_offline_frontier_reader(active_git_world, caplog):
    db, _origin, _observer = active_git_world
    db.set_prerequisite_observer(None)
    await db.claim_frontier_exclusions("done")
    assert "No Git prerequisite observer" in caplog.text
    assert "unavailable without receipt fallback" in caplog.text


# --- Shared store observations -------------------------------------------------
#
# Concurrent guarded callers of one repository store share a bounded flight. A
# controlled fetch counts every observation and blocks on an event, so these
# regressions order callers with barriers, never with host time.


class ControlledFetches:
    """A counted ``delivery_snapshot`` whose fetch N blocks until released."""

    def __init__(self, heads=None):
        from collections import defaultdict

        self.calls: list[dict] = []
        self.started = defaultdict(asyncio.Event)
        self.released = defaultdict(asyncio.Event)
        self.failures: dict[int, BaseException | str] = {}
        self.heads = heads or (lambda n: {"refs/remotes/origin/main": f"{n:040x}"})
        self.gated = True

    @property
    def count(self) -> int:
        return len(self.calls)

    def release(self, n: int) -> None:
        self.released[n].set()

    async def __call__(self, git, store, *, project_id, repository_id, repository_url,
                      target_ref):
        from types import MappingProxyType

        from src.integration.delivery_truth import DeliverySnapshot

        self.calls.append({"project_id": project_id, "repository_id": repository_id,
                           "repository_url": repository_url, "target_ref": target_ref})
        n = self.count
        self.started[n].set()
        if self.gated:
            await self.released[n].wait()
        failure = self.failures.get(n)
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            return DeliverySnapshot(git, str(store), project_id, repository_id, repository_url,
                                    target_ref, None, MappingProxyType({}), failure)
        heads = dict(self.heads(n))
        oid = heads.get("refs/remotes/origin/" + target_ref.removeprefix("refs/heads/"))
        return DeliverySnapshot(git, str(store), project_id, repository_id, repository_url,
                                target_ref, oid, MappingProxyType(heads),
                                None if oid else "target_ref_missing")


def oid(n: int) -> str:
    return f"{n:040x}"


async def settle() -> None:
    """Run every ready task to its next real suspension point (no host time)."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def shared_store(tmp_path, monkeypatch):
    from src.integration import delivery_observer as module

    fetches = ControlledFetches()
    monkeypatch.setattr(module, "delivery_snapshot", fetches)

    def make(git=None, data_dir=None):
        observer = DeliveryObserver(None, git=git or object(),
                                    data_dir=data_dir or tmp_path / "shared")
        observer._store = AsyncMock(side_effect=lambda target: observer.store_path(
            target.repository_id))
        return observer

    return fetches, make


def _target(ref="refs/heads/main", *, project="p", repository="r",
            url="https://example.test/r.git"):
    from src.integration.delivery_observer import DeliveryTarget

    return DeliveryTarget(project, repository, url, ref)


def _flight_state(observer, target):
    from src.integration.delivery_observer import _FLIGHTS

    key = (str(observer.store_path(target.repository_id)), target.project_id,
           target.repository_id, target.repository_url)
    return _FLIGHTS.get(asyncio.get_running_loop(), {}).get(key)


async def test_simultaneous_guarded_burst_shares_one_store_observation(shared_store):
    fetches, make = shared_store
    observer = make()
    target = _target()
    callers = [asyncio.create_task(observer.snapshot(target)) for _ in range(12)]
    await fetches.started[1].wait()
    await settle()
    assert fetches.count == 1
    assert _flight_state(observer, target).readers == 12
    fetches.release(1)
    results = await asyncio.gather(*callers)
    assert fetches.count == 1
    assert {(r.target_oid, r.error) for r in results} == {(oid(1), None)}
    assert _flight_state(observer, target) is None


async def test_callers_arriving_mid_fetch_share_exactly_one_successor(shared_store):
    fetches, make = shared_store
    observer = make()
    target = _target()
    first = asyncio.create_task(observer.snapshot(target))
    await fetches.started[1].wait()
    # Fetch 1 began before these callers asked: it is no proof for them.
    late = [asyncio.create_task(observer.snapshot(target)) for _ in range(8)]
    await settle()
    assert fetches.count == 1
    fetches.release(1)
    assert (await first).target_oid == oid(1)
    await fetches.started[2].wait()
    # A caller arriving once fetch 2 began needs a newer observation still.
    later = asyncio.create_task(observer.snapshot(target))
    await settle()
    assert fetches.count == 2
    fetches.release(2)
    assert {r.target_oid for r in await asyncio.gather(*late)} == {oid(2)}
    await fetches.started[3].wait()
    assert not later.done()
    fetches.release(3)
    assert (await later).target_oid == oid(3)
    assert fetches.count == 3
    assert _flight_state(observer, target) is None


async def test_required_later_observation_fetches_and_readers_alone_reuse_age(shared_store):
    fetches, make = shared_store
    fetches.gated = False
    observer = make()
    target = _target()
    assert (await observer.snapshot(target)).target_oid == oid(1)
    assert (await observer.snapshot(target)).target_oid == oid(2)
    assert fetches.count == 2
    # Only a read-only max_age caller may reuse a completed observation.
    assert (await observer.snapshot(target, max_age=30)).target_oid == oid(2)
    assert fetches.count == 2


async def test_one_observation_covers_other_refs_of_the_same_repository(shared_store):
    fetches, make = shared_store
    fetches.heads = lambda n: {"refs/remotes/origin/main": oid(n),
                               "refs/remotes/origin/aq/epic": oid(100 + n)}
    observer = make()
    refs = ("refs/heads/main", "refs/heads/aq/epic", "refs/heads/aq/absent",
            "refs/heads/bad..name", "refs/tags/v1")
    callers = [asyncio.create_task(observer.snapshot(_target(ref))) for ref in refs]
    await fetches.started[1].wait()
    await settle()
    fetches.release(1)
    main, epic, absent, malformed, tag = await asyncio.gather(*callers)
    assert fetches.count == 1
    assert (main.target_ref, main.target_oid, main.error) == ("refs/heads/main", oid(1), None)
    assert (epic.target_ref, epic.target_oid, epic.error) == ("refs/heads/aq/epic", oid(101), None)
    assert (absent.target_oid, absent.error) == (None, "target_ref_missing")
    # A name its own fetch would have rejected stays unknown, never "missing".
    for refused in (malformed, tag):
        assert refused.target_oid is None
        assert refused.error == "snapshot_git_error: invalid_target"
    assert {target.target_ref for target in observer._recent} >= {
        "refs/heads/main", "refs/heads/aq/epic", "refs/heads/aq/absent"}


async def test_repositories_urls_and_projects_never_share_an_observation(shared_store):
    fetches, make = shared_store
    fetches.gated = False
    observer = make()
    targets = [_target(), _target(repository="r2"), _target(url="https://example.test/fork.git"),
               _target(project="p2")]
    results = await asyncio.gather(*(observer.snapshot(target) for target in targets))
    assert fetches.count == 4
    assert sorted((c["project_id"], c["repository_id"], c["repository_url"])
                  for c in fetches.calls) == sorted(
        (t.project_id, t.repository_id, t.repository_url) for t in targets)
    for target, result in zip(targets, results, strict=True):
        assert (result.project_id, result.repository_id, result.repository_url) == (
            target.project_id, target.repository_id, target.repository_url)
        assert result.error is None


async def test_moved_and_vanished_refs_are_seen_by_the_observation_after_them(shared_store):
    fetches, make = shared_store
    fetches.gated = False
    fetches.heads = lambda n: {"refs/remotes/origin/main": oid(n)} | (
        {"refs/remotes/origin/aq/x": oid(50)} if n == 1 else {})
    observer = make()
    main, x = _target(), _target("refs/heads/aq/x")
    assert (await observer.snapshot(main)).target_oid == oid(1)
    assert observer._recent[x][1].target_oid == oid(50)
    fetches.gated = True
    held = asyncio.create_task(observer.snapshot(main))
    await fetches.started[2].wait()
    moved = asyncio.create_task(observer.snapshot(x))
    await settle()
    fetches.release(2)
    assert (await held).target_oid == oid(2)
    # The vanished ref's cached evidence is retired by the covering fetch.
    assert observer._recent[x][1].error == "target_ref_missing"
    await fetches.started[3].wait()
    fetches.release(3)
    assert ((await moved).target_oid, (await moved).error) == (None, "target_ref_missing")
    assert (await observer.snapshot(x, max_age=30, cached_only=True)).error == "target_ref_missing"
    assert fetches.count == 3


@pytest.mark.parametrize("failure", [
    "snapshot_git_error: fatal: Authentication failed", RuntimeError("transport"),
])
async def test_failed_observation_is_unknown_for_every_joiner_until_recovery(
    shared_store, failure,
):
    fetches, make = shared_store
    fetches.heads = lambda n: {"refs/remotes/origin/main": oid(n),
                               "refs/remotes/origin/aq/epic": oid(100 + n)}
    fetches.failures[1] = failure
    observer = make()
    callers = [asyncio.create_task(observer.snapshot(_target(ref)))
               for ref in ("refs/heads/main", "refs/heads/aq/epic", "refs/heads/main")]
    await fetches.started[1].wait()
    await settle()
    fetches.release(1)
    results = await asyncio.gather(*callers)
    assert fetches.count == 1
    for result in results:
        assert result.target_oid is None
        assert result.error not in {None, "target_ref_missing"}
    assert observer._recent == {}
    fetches.release(2)
    recovered = await observer.snapshot(_target("refs/heads/aq/epic"))
    assert (recovered.target_oid, recovered.error) == (oid(102), None)
    assert fetches.count == 2


async def test_cached_only_diagnostics_never_wait_for_or_start_a_shared_fetch(shared_store):
    fetches, make = shared_store
    observer = make()
    target = _target()
    cold = asyncio.create_task(observer.snapshot(target))
    await fetches.started[1].wait()
    unavailable = asyncio.create_task(observer.snapshot(target, max_age=30, cached_only=True))
    await settle()
    assert unavailable.done()
    assert (await unavailable).error == "snapshot_unavailable"
    fetches.release(1)
    await cold
    guarded = asyncio.create_task(observer.snapshot(target))
    await fetches.started[2].wait()
    warm = asyncio.create_task(observer.snapshot(target, max_age=30, cached_only=True))
    await settle()
    assert warm.done()
    assert (await warm).target_oid == oid(1)
    assert fetches.count == 2
    fetches.release(2)
    assert (await guarded).target_oid == oid(2)


async def test_cancelled_creator_leaves_its_flight_and_peers_running(shared_store):
    fetches, make = shared_store
    observer = make()
    target = _target()
    creator = asyncio.create_task(observer.snapshot(target))
    peers = [asyncio.create_task(observer.snapshot(target)) for _ in range(3)]
    await fetches.started[1].wait()
    await settle()
    flight = _flight_state(observer, target).flight.task
    creator.cancel()
    await settle()
    assert creator.cancelled()
    assert not flight.done()
    assert _flight_state(observer, target).readers == 3
    fetches.release(1)
    assert {r.target_oid for r in await asyncio.gather(*peers)} == {oid(1)}
    assert fetches.count == 1
    assert _flight_state(observer, target) is None


async def test_abandoned_callers_leave_no_fetch_backlog_or_state(shared_store):
    fetches, make = shared_store
    observer = make()
    target = _target()
    early = [asyncio.create_task(observer.snapshot(target)) for _ in range(5)]
    await fetches.started[1].wait()
    late = [asyncio.create_task(observer.snapshot(target)) for _ in range(5)]
    await settle()
    flight = _flight_state(observer, target).flight.task
    for caller in early + late:
        caller.cancel()
    await settle()
    assert all(caller.cancelled() for caller in early + late)
    assert _flight_state(observer, target).readers == 0
    fetches.release(1)
    await asyncio.wait((flight,))
    await settle()
    # Nobody still asks: the late callers' successor fetch never starts.
    assert fetches.count == 1
    assert _flight_state(observer, target) is None
    fetches.release(2)
    assert (await observer.snapshot(target)).target_oid == oid(2)
    assert fetches.count == 2


async def test_cancelled_flight_is_an_unknown_observation_not_a_peer_cancellation(shared_store):
    fetches, make = shared_store
    observer = make()
    target = _target()
    callers = [asyncio.create_task(observer.snapshot(target)) for _ in range(3)]
    await fetches.started[1].wait()
    await settle()
    _flight_state(observer, target).flight.task.cancel()
    results = await asyncio.gather(*callers)
    assert {r.error for r in results} == {"observer_error: CancelledError"}
    assert all(r.target_oid is None for r in results)
    assert observer._recent == {}
    assert _flight_state(observer, target) is None
    fetches.release(2)
    assert (await observer.snapshot(target)).target_oid == oid(2)


async def test_observers_sharing_a_store_share_flights_but_not_mutable_state(shared_store):
    fetches, make = shared_store
    monitoring_git, development_git = object(), object()
    monitoring, development = make(monitoring_git), make(development_git)
    target = _target()
    callers = [asyncio.create_task(observer.snapshot(target))
               for observer in (monitoring, development, development)]
    await fetches.started[1].wait()
    await settle()
    fetches.release(1)
    first, second, third = await asyncio.gather(*callers)
    assert fetches.count == 1
    assert (first.git, second.git, third.git) == (monitoring_git, development_git,
                                                  development_git)
    assert monitoring._recent[target][1].git is monitoring_git
    assert development._recent[target][1].git is development_git
    # Each caller's freshness marks are its own, as if it had fetched alone.
    first._freshness["refs/heads/main"] = False
    assert second._freshness == third._freshness == {}
    assert development._recent[target][1]._freshness == {}
    # A different store is a different flight even for the same repository.
    elsewhere = make(data_dir=monitoring.data_dir.parent / "elsewhere")
    fetches.release(2)
    await elsewhere.snapshot(target)
    assert fetches.count == 2


async def test_concurrent_guarded_writers_share_fetches_and_need_current_proof(
    active_git_world, monkeypatch,
):
    """A real race: writers that asked after a fetch began never consume it."""
    import src.integration.repository_recovery as recovery

    db, origin, _observer = active_git_world
    real_fetch = recovery.fetch_retained_origin
    fetched, captured, release = [], asyncio.Event(), asyncio.Event()

    async def controlled_fetch(*args, **kwargs):
        result = await real_fetch(*args, **kwargs)
        fetched.append(kwargs.get("repository_url"))
        if len(fetched) == 1:
            captured.set()
            await release.wait()
        return result

    monkeypatch.setattr(recovery, "fetch_retained_origin", controlled_fetch)
    def epic_head() -> str:
        return git(origin.url, "rev-parse", "refs/heads/aq/epic")

    stale_epic = epic_head()

    def guarded_view():
        return asyncio.create_task(db.observe_removal_delivery(["reopened"], for_mutation=True))

    async def joined(readers: int) -> None:
        # Writers reach the store after their own database reads; wait until each
        # has really asked the shared observation rather than guessing a delay.
        from src.integration.delivery_observer import _FLIGHTS

        async with asyncio.timeout(30):
            while sum(state.readers for state in _FLIGHTS.get(
                    asyncio.get_running_loop(), {}).values()) < readers:
                await asyncio.sleep(0.005)

    early = [guarded_view() for _ in range(4)]
    await captured.wait()
    await joined(4)
    # Fetch 1 holds aq/epic without the reopened work; now it is collected.
    git(origin.clone, "fetch", "-q", "origin")
    git(origin.clone, "checkout", "-q", "-B", "aq/epic", "origin/aq/epic")
    git(origin.clone, "merge", "-q", "--no-ff", "-m", "Collect reopened", "origin/aq/reopened")
    git(origin.clone, "push", "-q", "origin", "aq/epic")
    git(origin.clone, "push", "-q", "origin", "aq/epic:main")
    current_epic = epic_head()
    assert current_epic != stale_epic
    late = [guarded_view() for _ in range(4)]
    await joined(8)
    release.set()
    views = await asyncio.gather(*early, *late)
    # Eight guarded writers, no serial per-caller backlog. Fetch 1 began before
    # all but its creator asked, and its stale epic is caught by that creator's
    # ls-remote; the seven writers that asked mid-fetch share fetch 2, and the
    # creator's retry asked after fetch 2 began, so it alone takes fetch 3.
    assert len(fetched) == 3
    for view in views:
        assert view is not None
        assert {s.target_oid for s in view.snapshots} == {current_epic}
        async with db.immediate() as conn:
            with pytest.raises(HierarchyError) as refused:
                await db.guard_integration_mutation("reopened", "reopen", conn=conn,
                                                    delivery=view)
        assert refused.value.code == "delivery_target_fixed"
    assert (await db.get_task("reopened")).status is TaskStatus.COMPLETED


async def test_strict_shared_observation_ignores_disposition_and_receipts(world):
    from dataclasses import replace

    from src.integration.delivery_truth import (
        DeliveryDisposition, delivered, load_delivery_requests,
    )
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    source = git(origin.clone, "rev-parse", "origin/aq/reopened")
    disposition = DeliveryDisposition(
        "p", "r", "reopened", "close-reopened-2", "refs/heads/main", source,
        git(origin.clone, "rev-parse", "origin/main"), "operator historical evidence", "a" * 64,
    )
    requests = await load_delivery_requests(db, CHILDREN, repository_id="r",
                                            target_ref="refs/heads/main")
    # Exercise the evaluator's lifecycle/ancestry separation. Persistence and
    # operator authorization are covered by test_integration_containment_cutover.
    requests["reopened"] = replace(requests["reopened"], disposition=disposition)
    observed = await GitTruth(GitManager()).snapshot(
        str(origin.clone), project_id="p", repository_id="r", repository_url=origin.url,
        target_ref="refs/heads/main",
    )
    answers = {tid: await delivered(request, request.target_ref, observation=observed)
               for tid, request in requests.items()}
    assert answers["done"].state is DeliveryState.CONTAINED
    assert answers["wrong"].state is DeliveryState.UNKNOWN
    assert answers["missing"].state is DeliveryState.UNKNOWN
    assert answers["reopened"].state is DeliveryState.PENDING
    assert answers["reopened"].disposition == disposition
    assert len({answer.observation_token for answer in answers.values()}) == 1
    assert len({answer.observed_at for answer in answers.values()}) == 1
    assert (await delivered(replace(requests["reopened"], completion_id="next-generation"),
                            "refs/heads/main", observation=observed)).disposition is None
    assert (await delivered(replace(requests["reopened"], target_ref="refs/heads/other"),
                            "refs/heads/other", observation=observed)).disposition is None
    # Lifecycle retirement is separate even when consumers receive legacy settlements.
    settled = replace(requests["reopened"], settled_repository_id="r",
                      settled_target_ref="refs/heads/main", settled_completion_id="close-reopened-2",
                      settled_reason="historical settlement")
    assert (await observed.delivered(settled)).state is DeliveryState.PENDING


async def test_strict_disposition_parser_never_widens_damaged_identity():
    from src.integration.delivery_truth import delivery_disposition

    for value in (None, "broken json", {}, {"completion_id": None}, {"unknown": "field"}):
        assert delivery_disposition(value) is None


def test_receipt_consumer_inventory_covers_every_direct_source_reference():
    """Keep the S1 section 2 inventory exhaustive while legacy authority retires."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    inventory = (root / "docs/specs/design/strict-git-containment.md").read_text()
    references = {
        str(path.relative_to(root))
        for path in (root / "src").rglob("*.py")
        if any(marker in path.read_text() for marker in (
            "task_delivery_receipts", "list_integration_delivery_receipts",
        ))
    }
    missing = sorted(path for path in references if f"`{path}`" not in inventory)
    assert not missing, f"Assign receipt reader/writer retirement owners: {missing}"


async def test_delivery_edge_uses_immutable_view_and_rechecks_rewind(world, tmp_path):
    from dataclasses import FrozenInstanceError

    from src.integration.admission import observe_admission
    from src.integration.git_truth import GitTruth

    db, origin, observer, service = world
    db.set_prerequisite_observer(DeliveryObserver(
        db, git=observer.git, data_dir=tmp_path / "strict-admission", truth=GitTruth(observer.git),
    ))
    for tid in ("waiting", "independent"):
        await db.create_task(Task(
            id=tid, project_id="p", title=tid, description="", status=TaskStatus.READY,
        ))
    await db.add_dependency("waiting", "done", "delivery-of")
    assert not (await db.get_task("waiting")).is_blocked  # Structural projection only.
    view = await observe_admission(db, ["waiting", "independent"], service)
    assert view.allowed == {"waiting", "independent"}
    with pytest.raises(FrozenInstanceError):
        view.allowed = frozenset()
    with pytest.raises(TypeError):
        view.reasons["waiting"] = ()
    base = git(origin.clone, "rev-list", "--max-parents=0", "origin/main")
    git(origin.clone, "push", "--force", "origin", f"{base}:main")
    assert not await view.is_fresh("waiting")
    current = await observe_admission(db, ["waiting", "independent"], service)
    assert current.allowed == {"independent"}
    assert current.reasons["waiting"][0]["ref"] == "done"
    assert "independent" not in current.reasons


async def test_unavailable_delivery_observer_withholds_only_edge_consumers(world):
    from src.integration.admission import observe_admission

    db, _origin, _observer, service = world
    db.set_prerequisite_observer(None)
    for tid in ("waiting", "independent"):
        await db.create_task(Task(
            id=tid, project_id="p", title=tid, description="", status=TaskStatus.READY,
        ))
    await db.add_dependency("waiting", "done", "delivery-of")
    view = await observe_admission(db, ["waiting", "independent"], service)
    assert view.allowed == {"independent"}
    assert view.reasons["waiting"][0]["code"] == "delivery_evidence_unavailable"


async def test_cleanup_guard_rechecks_current_target_without_receipts(world, tmp_path):
    from src.integration.cleanup import IntegrationCleanupService
    from src.integration.git_truth import GitTruth

    db, origin, _observer, _service = world
    transport = GitManager()
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "cleanup-proof",
                                truth=GitTruth(transport))
    db.set_prerequisite_observer(observer)
    completion = await db.get_task_completion("done")
    source = completion.commits[-1]
    base = git(origin.clone, "rev-parse", source + "^")
    service = IntegrationCleanupService(db, data_dir=tmp_path)
    row = {"target_ref": "refs/heads/aq/done", "expected_sha": source}
    batch = {"id": "historical-batch", "project_id": "p", "repository_id": "r"}
    assert await service._delivery_hold(row, batch) is None
    git(origin.clone, "push", "--force", "origin", base + ":main")
    assert "unknown or pending" in await service._delivery_hold(row, batch)
    db.set_prerequisite_observer(None)
    assert "requires current Git" in await service._delivery_hold(row, batch)
