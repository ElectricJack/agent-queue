"""Explicit unheld-root no-op completion over real PostgreSQL and Git."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.task_commands import TaskCommandsMixin
from src.database.tables import task_branch_origins, tasks
from src.git.manager import GitError, GitManager
from src.integration.delivery_truth import DeliveryState, load_delivery_requests
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.ownership import BranchBusy
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.train_controls import TrainControls
from src.models import RepoSourceType, TaskCompletion, TaskStatus, Workspace
from src.profiles.capabilities import DENY_ALL
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import MAIN, completed, world as fixture_world

world = fixture_world


async def empty_root(world, *, status=TaskStatus.READY):
    base = git(world.origin.clone, "rev-parse", "origin/main")
    git(world.origin.clone, "push", "-q", "origin", f"{base}:refs/heads/aq/noop")
    await completed(world, "noop", done=False, head=base, source_base=base)
    await world.db.transition_task("noop", status)
    return base


async def record(world, *, dry_run=True, head=None):
    return await TrainControls(world.db).record_root_noop(
        "noop",
        dry_run=dry_run,
        expected_head_sha=head,
        operator_id="human:test",
        reason="No code was produced",
    )


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.COMPLETED])
async def test_root_noop_preview_apply_retains_exact_evidence_and_is_idempotent(world, status):
    base = await empty_root(world, status=status)
    preview = await record(world)
    assert preview["head_sha"] == preview["base_sha"] == base
    assert (await world.db.get_task("noop")).status is status
    assert await world.db.get_task_completion("noop") is None
    proof = GitProvenance(GitManager(), str(world.origin.clone), repository_url=world.origin.url)
    identity = CompletionIdentity("p", "r", "noop", preview["completion_id"])
    assert await proof.read_completion(identity) is None

    applied = await record(world, dry_run=False, head=base)
    assert applied["completion_id"] == preview["completion_id"]
    assert await record(world, dry_run=False, head=base) == applied
    task = await world.db.get_task("noop")
    assert task.status is TaskStatus.COMPLETED and task.branch_name == "aq/noop"
    completions = await world.db.get_task_completions("noop")
    assert len(completions) == 1
    assert completions[0].outcome == "pass" and completions[0].work_outcome == "no-op"
    assert completions[0].commits == []
    await proof.git.afetch_origin(
        str(world.origin.clone), repository_url=world.origin.url, all_heads=True
    )
    retained = await proof.read_completion(identity)
    assert retained["artifact"] is False and retained["source_oid"] == base
    async with world.db._engine.connect() as conn:
        assert (await conn.scalar(select(task_branch_origins.c.branch_name))) == "aq/noop"
    evidence = (await world.db._delivery_observer.observe(["noop"])).get("noop")
    assert evidence.state is DeliveryState.NO_CHANGE
    requests = await load_delivery_requests(
        world.db, ["noop"], repository_id="r", target_ref=MAIN.target_ref
    )
    assert requests["noop"].completion_id == applied["completion_id"]


async def test_root_noop_reuses_existing_passing_noop_generation(world):
    base = await empty_root(world, status=TaskStatus.COMPLETED)
    await world.db.save_task_completion(
        TaskCompletion(
            id="existing-noop",
            task_id="noop",
            outcome="pass",
            work_outcome="no-op",
            completed_at=10,
        )
    )
    result = await record(world, dry_run=False, head=base)
    assert result["completion_id"] == "existing-noop"
    completions = await world.db.get_task_completions("noop")
    assert len(completions) == 1
    assert completions[0].commits == []
    proof = GitProvenance(GitManager(), str(world.origin.clone), repository_url=world.origin.url)
    await proof.git.afetch_origin(
        str(world.origin.clone), repository_url=world.origin.url, all_heads=True
    )
    retained = await proof.read_completion(CompletionIdentity("p", "r", "noop", "existing-noop"))
    assert retained["artifact"] is False and retained["source_oid"] == base
    evidence = (await world.db._delivery_observer.observe(["noop"])).get("noop")
    assert evidence.state is DeliveryState.NO_CHANGE
    assert evidence.source_oid == evidence.source_base == base


async def test_root_noop_reopened_root_gets_a_new_generation(world):
    base = await empty_root(world)
    first = await record(world, dry_run=False, head=base)
    await world.db.transition_task("noop", TaskStatus.READY)
    second = await record(world, dry_run=False, head=base)
    assert first["completion_id"] != second["completion_id"]
    assert len(await world.db.get_task_completions("noop")) == 2
    assert (await world.db._delivery_observer.observe(["noop"])).get(
        "noop"
    ).state is DeliveryState.NO_CHANGE


@pytest.mark.parametrize(
    "guard",
    [
        "changes",
        "owner",
        "claim",
        "workspace",
        "writer",
        "deliverable",
        "child",
        "checklist",
        "batch",
        "completion",
        "generation",
    ],
)
async def test_root_noop_refuses_unsafe_identity_without_completion_writes(
    world, monkeypatch, guard
):
    base = await empty_root(world)
    expected = {
        "changes": "contains changes",
        "owner": "branch ownership",
        "claim": "worker claim",
        "workspace": "claimed workspace",
        "writer": "live writer",
        "deliverable": "deliverables",
        "child": "container",
        "checklist": "open checklist",
        "batch": "open batch",
        "completion": "not a passing no-op",
        "generation": "generation",
    }[guard]
    if guard == "changes":
        base = world.origin.work("noop")
    elif guard == "owner":
        await BranchLock(world.db).acquire(BranchKey(repository_id="r", branch="aq/noop"), "writer")
    elif guard == "claim":
        await world.db.set_task_meta("noop", "claimed_by_session", "stopped-worker")
    elif guard == "workspace":
        await world.db.create_workspace(
            Workspace(
                id="worker",
                project_id="p",
                workspace_path=str(world.origin.clone),
                source_type=RepoSourceType.LINK,
                locked_by_task_id="noop",
            )
        )
    elif guard == "writer":
        from src.models import SessionRecord

        await world.db.create_session(
            SessionRecord(
                id="writer",
                project_id="p",
                profile_id="worker-codex",
                harness="codex",
                provider="openai",
                name="writer",
                lifecycle="pool",
                work_dir=str(world.origin.clone),
                epoch="test",
                instance_token="test",
                started_at=1,
                task_id="noop",
                state="running",
            )
        )
    elif guard == "deliverable":
        async with world.db._engine.begin() as conn:
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == "noop")
                .values(
                    deliverables='[{"type":"file","path":"answer.md","required":true}]',
                )
            )
    elif guard == "child":
        await completed(world, "child", parent="noop", done=False)
    elif guard == "checklist":
        await world.db.add_task_subtasks("noop", "p", [{"title": "Unfinished work"}])
    elif guard == "batch":
        await BatchStore(world.db).freeze(
            Batch("open", "p", "r", MAIN.target_ref),
            (BatchMember("noop", base, base),),
            trees={"noop": git(world.origin.clone, "rev-parse", base + "^{tree}")},
        )
    else:
        await world.db.transition_task("noop", TaskStatus.COMPLETED)
        await world.db.save_task_completion(
            TaskCompletion(
                id="existing",
                task_id="noop",
                outcome="pass",
                work_outcome="no-op" if guard == "generation" else "implemented",
                completed_at=1,
            )
        )
        if guard == "generation":
            await world.db.set_task_meta("noop", "development_completion_id", "missing-current")
    writer = AsyncMock()
    monkeypatch.setattr(GitProvenance, "write_completion", writer)
    before = await world.db.get_task("noop")
    with pytest.raises(ValueError, match=expected):
        await record(world, dry_run=False, head=base)
    writer.assert_not_awaited()
    assert (await world.db.get_task("noop")).status is before.status
    assert len(await world.db.get_task_completions("noop")) == (
        guard in {"completion", "generation"}
    )


async def test_root_noop_refuses_changed_head_and_unavailable_evidence(world):
    base = await empty_root(world)
    with pytest.raises(ValueError, match="changed from preview"):
        await record(world, dry_run=False, head="f" * 40)
    controls = TrainControls(world.db, snapshot=AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="evidence is unavailable"):
        await controls.record_root_noop(
            "noop", dry_run=False, expected_head_sha=base, operator_id="operator", reason="no code"
        )
    assert (await world.db.get_task("noop")).status is TaskStatus.READY
    assert await world.db.get_task_completion("noop") is None


async def test_root_noop_provenance_push_failure_rolls_back_terminal_database_writes(
    world, monkeypatch
):
    base = await empty_root(world)
    monkeypatch.setattr(
        GitProvenance, "write_completion", AsyncMock(side_effect=GitError("push failed"))
    )
    with pytest.raises(GitError, match="push failed"):
        await record(world, dry_run=False, head=base)
    assert (await world.db.get_task("noop")).status is TaskStatus.READY
    assert await world.db.get_task_completion("noop") is None


async def test_root_noop_does_not_replace_existing_artifact_provenance(world):
    base = await empty_root(world, status=TaskStatus.COMPLETED)
    preview = await record(world)
    proof = GitProvenance(GitManager(), str(world.origin.clone), repository_url=world.origin.url)
    await proof.write_completion(
        CompletedSource(
            CompletionIdentity("p", "r", "noop", preview["completion_id"]),
            base,
        ),
        artifact=True,
    )
    with pytest.raises(ValueError, match="retained completion is an artifact"):
        await record(world, dry_run=False, head=base)
    assert await world.db.get_task_completion("noop") is None


@pytest.mark.parametrize("movement", ["identity", "remote"])
async def test_root_noop_rechecks_identity_and_remote_under_lock(world, movement):
    base = await empty_root(world)

    async def moved_snapshot(db, target):
        from src.integration.train_sources import project_snapshot

        observed = await project_snapshot(db, target)
        if movement == "identity":
            await db.update_task("noop", title="Changed while fetching")
        else:
            world.origin.work("noop")
        return observed

    controls = TrainControls(world.db, snapshot=moved_snapshot)
    with pytest.raises(ValueError, match="changed; preview again"):
        await controls.record_root_noop(
            "noop", dry_run=False, expected_head_sha=base, operator_id="operator", reason="no code"
        )
    assert (await world.db.get_task("noop")).status is TaskStatus.READY
    assert await world.db.get_task_completion("noop") is None


@pytest.mark.parametrize(
    "authorized",
    [
        "local",
        "own-supervisor",
        "global-supervisor",
        "worker",
        "foreign-supervisor",
        "stopped-supervisor",
        "playbook",
    ],
)
async def test_root_noop_handler_authorizes_only_local_or_live_owning_supervisor(
    world, monkeypatch, authorized
):
    base = await empty_root(world)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    project = (
        None
        if authorized == "global-supervisor"
        else ("other" if authorized == "foreign-supervisor" else "p")
    )
    monkeypatch.setattr(
        world.db,
        "get_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id="supervisor",
                profile_id="supervisor",
                lifecycle="named",
                project_id=project,
                desired_state="running",
                state="stopped" if authorized == "stopped-supervisor" else "running",
            )
        ),
    )
    kind = (
        PrincipalKind.LOCAL
        if authorized == "local"
        else PrincipalKind.PLAYBOOK
        if authorized == "playbook"
        else PrincipalKind.SESSION
    )
    with principal_context(
        ExecutionPrincipal(
            kind=kind,
            session_id="supervisor",
            project_id=project,
            elevated=authorized != "worker",
            policy=DENY_ALL,
        )
    ):
        result = await handler._cmd_integration_record_root_noop(
            {
                "task_id": "noop",
                "dry_run": False,
                "expected_head_sha": base,
                "reason": "no code",
            }
        )
    allowed = authorized in {"local", "own-supervisor", "global-supervisor"}
    assert result["success"] is allowed, result
    assert result["outcome"] == ("recorded" if allowed else "unauthorized")
    assert (await world.db.get_task("noop")).status is (
        TaskStatus.COMPLETED if allowed else TaskStatus.READY
    )


async def test_root_noop_handler_refuses_ambiguous_branch_ownership(world, monkeypatch):
    await empty_root(world)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    monkeypatch.setattr(
        BranchLock,
        "lock_on",
        AsyncMock(side_effect=BranchBusy("multiple rows name the same ref")),
    )
    with principal_context(ExecutionPrincipal(kind=PrincipalKind.LOCAL, policy=DENY_ALL)):
        result = await handler._cmd_integration_record_root_noop({"task_id": "noop"})
    assert result["success"] is False and result["outcome"] == "refused"
    assert "multiple rows" in result["error"]
    assert (await world.db.get_task("noop")).status is TaskStatus.READY
    assert await world.db.get_task_completion("noop") is None


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.COMPLETED])
async def test_admin_completed_status_requires_root_completion_provenance(world, status):
    await empty_root(world, status=status)
    handler = TaskCommandsMixin()
    handler.db = world.db
    result = await handler._cmd_set_task_status({"task_id": "noop", "status": "COMPLETED"})
    assert result["code"] == "integration.completion_provenance_required"
    assert "record-root-noop" in result["error"]
    assert (await world.db.get_task("noop")).status is status
    await world.db.update_task("noop", branch_name=None)
    result = await handler._cmd_set_task_status({"task_id": "noop", "status": "COMPLETED"})
    assert result["code"] == "integration.completion_provenance_required"
    async with world.db._engine.begin() as conn:
        await conn.execute(update(task_branch_origins).values(retired_at=1))
    result = await handler._cmd_set_task_status({"task_id": "noop", "status": "COMPLETED"})
    assert result["new_status"] == "COMPLETED"


@pytest.mark.parametrize("changed", [False, True])
async def test_admin_nochange_releases_dependent_only_at_exact_origin_base(world, changed):
    base = await empty_root(world)
    await completed(world, "epic", done=False)
    await completed(world, "dependent", parent="epic", done=False, needs=("noop",))
    await world.db.transition_task("dependent", TaskStatus.READY)
    assert (await world.db.get_task("dependent")).is_blocked
    if changed:
        # Even an empty commit must use the ordinary completion path.
        git(world.origin.clone, "checkout", "-B", "aq/noop", base)
        git(world.origin.clone, "commit", "--allow-empty", "-m", "empty authored commit")
        head = git(world.origin.clone, "rev-parse", "HEAD")
        git(world.origin.clone, "push", "origin", "aq/noop")
        with pytest.raises(ValueError, match="differs from its recorded origin base"):
            await record(world, dry_run=False, head=head)
        assert await world.db.get_task_completion("noop") is None
        assert (await world.db.get_task("dependent")).is_blocked
    else:
        await record(world, dry_run=False, head=base)
        view = await world.db._delivery_observer.prerequisite_view("p", task_id="dependent")
        assert view.default.get("noop").satisfied, view.default.get("noop")
        assert await world.db.is_hierarchy_task_runnable("dependent"), await world.db.claim_frontier_exclusions("dependent")


async def test_admin_completed_status_also_refuses_branched_children_and_legacy_modes(world):
    await empty_root(world)
    await completed(world, "child", parent="noop", done=False)
    await world.db.update_project("p", hierarchical_integration_mode="disabled")
    handler = TaskCommandsMixin()
    handler.db = world.db
    result = await handler._cmd_set_task_status({"task_id": "child", "status": "COMPLETED"})
    assert result["code"] == "integration.completion_provenance_required"
    assert (await world.db.get_task("child")).status is TaskStatus.IN_PROGRESS
