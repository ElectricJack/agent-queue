"""Real Git + private PostgreSQL coverage for the development delivery path."""

import subprocess
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import gates, projects, task_gates
from src.integration.development import DevelopmentBusy, DevelopmentIntegration, DevelopmentPolicy
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus
from tests.db_fixtures import lease_dsn


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
async def setup(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "config", "user.name", "Tester")
    git(source, "config", "user.email", "tester@example.test")
    (source / "base.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    git(source, "push", "origin", "main")
    db = Database(lease_dsn("development.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Development"))
    repo = RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    await db.create_repo(repo)
    async with db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id="r")
        )
    service = DevelopmentIntegration(db, data_dir=tmp_path / "data")
    await service.configure(
        "p",
        {"validation": "focused", "commands": ["test -f base.txt"]},
        reason="test development policy",
        operator_id="local",
    )
    yield db, service, source, remote, repo
    await db.close()


async def feature(setup, task_id, *, filename=None, content="new\n"):
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / (filename or task_id + ".txt")).write_text(content)
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", task_id)
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            repo_id="r",
            title=task_id,
            description="",
            branch_name=task_id,
            status=TaskStatus.COMPLETED,
        )
    )
    return head


async def test_batch_publishes_exact_validated_sha_and_replay_is_idle(setup):
    _db, service, _source, remote, _repo = setup
    head = await feature(setup, "one")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert result["head_sha"] == head
    assert git(remote, "rev-parse", "main") == head
    rows = await service.rows("p")
    root = [r for r in rows if r["state"] == "delivered" and r["target_ref"] == "refs/heads/main"]
    assert root[0]["evidence"]["conclusion"] == "passed"
    assert root[0]["evidence"]["head_sha"] == head
    assert (await service.sweep("p"))["outcome"] == "idle"


async def test_successor_waits_for_default_branch_delivery(setup):
    db, service, _source, remote, _repo = setup
    head = await feature(setup, "prerequisite")
    await db.create_task(Task(
        id="successor", project_id="p", title="successor", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("successor", "prerequisite")
    assert (await db.get_task("successor")).is_blocked
    notifications = []

    async def on_ready(entries):
        notifications.extend(entries)

    db.set_ready_listener(on_ready)

    await service.configure(
        "p", {"commands": ["exit 7"]}, reason="test candidate only", operator_id="local"
    )
    assert (await service.sweep("p"))["outcome"] == "parked"
    assert head in git(remote, "show-ref"), "candidate was preserved"
    assert (await db.get_task("successor")).is_blocked

    await service.configure(
        "p", {"commands": ["test -f prerequisite.txt"]},
        reason="publish prerequisite", operator_id="local",
    )
    assert (await service.sweep("p", retry=True))["outcome"] == "delivered"
    assert git(remote, "rev-parse", "main") == head
    assert not (await db.get_task("successor")).is_blocked
    assert notifications == [("successor", "unblocked")]

    await db.save_task_completion(TaskCompletion(
        id="same-revision", task_id="prerequisite", outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    assert not (await db.get_task("successor")).is_blocked
    await db.save_task_completion(TaskCompletion(
        id="new-revision", task_id="prerequisite", outcome="pass",
        commits=["a" * 40], completed_at=time.time(),
    ))
    assert (await db.get_task("successor")).is_blocked


async def test_deleted_delivered_branch_does_not_strand_later_batches(setup):
    db, service, source, remote, _repo = setup
    previous = await feature(setup, "previous")
    await db.save_task_completion(TaskCompletion(
        id="previous-close", task_id="previous", outcome="pass",
        commits=[previous], completed_at=time.time(),
    ))
    # Simulate a historical merge and normal source-branch cleanup, before
    # this publisher had a delivery receipt for it.
    git(source, "push", "origin", f"{previous}:main")
    git(source, "push", "origin", "--delete", "previous")
    later = await feature(setup, "later")
    await db.add_dependency("later", "previous")
    assert (await db.get_task("later")).is_blocked
    # An already-completed chain is assembled in one dependency-ordered pass.
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert not (await db.get_task("later")).is_blocked
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert git(remote, "merge-base", "--is-ancestor", later, "main") == ""


@pytest.mark.parametrize("blocker", ["human_gate", "unfinished_dependency"])
async def test_batch_keeps_non_delivery_blockers(setup, blocker):
    db, service, _source, remote, _repo = setup
    base = git(remote, "rev-parse", "main")
    await feature(setup, "held")
    if blocker == "human_gate":
        async with db._engine.begin() as conn:
            await conn.execute(insert(gates).values(
                id="review", project_id="p", gate_type="human", title="Review",
                question="Approve?", status="open", created_at=time.time(),
            ))
            await conn.execute(insert(task_gates).values(task_id="held", gate_id="review"))
            await db.recompute_blocked({"held"}, conn=conn)
    else:
        await db.create_task(Task(
            id="unfinished", project_id="p", title="unfinished", description="",
            status=TaskStatus.READY,
        ))
        await db.add_dependency("held", "unfinished")
    assert (await db.get_task("held")).is_blocked
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert git(remote, "rev-parse", "main") == base


async def test_failed_validation_parks_and_preserves_candidate(setup):
    _db, service, _source, remote, _repo = setup
    before = git(remote, "rev-parse", "main")
    head = await feature(setup, "one")
    await service.configure(
        "p", {"commands": ["exit 7"]}, reason="require failing test", operator_id="local"
    )
    result = await service.sweep("p")
    assert result["outcome"] == "parked"
    assert result["evidence"]["checks"][0]["exit_code"] == 7
    assert git(remote, "rev-parse", "main") == before
    assert head in git(remote, "show-ref")
    assert (await service.sweep("p"))["outcome"] == "idle"
    await service.configure("p", {"commands": ["true"]}, reason="fixed test", operator_id="local")
    assert (await service.sweep("p", retry=True))["outcome"] == "delivered"


async def test_conflicting_member_does_not_block_independent_work(setup):
    _db, service, _source, remote, _repo = setup
    await feature(setup, "one", filename="base.txt", content="one\n")
    await feature(setup, "two", filename="base.txt", content="two\n")
    await feature(setup, "three")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert {m["task_id"] for m in result["manifest"]} == {"one", "three"}
    assert git(remote, "show", "main:three.txt") == "new"
    assert any(
        r["state"] == "parked" and r["manifest"][0]["task_id"] == "two"
        for r in await service.rows("p")
    )


async def test_adopt_requires_ancestry_or_explicit_operator_equivalence(setup):
    db, service, _source, remote, _repo = setup
    await feature(setup, "one")
    main = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="not an ancestor"):
        await service.adopt(
            project_id="p",
            task_ids=["one"],
            target_ref="refs/heads/main",
            head_sha=main,
            reason="manual delivery",
            operator_id="local",
        )
    await db.transition_task("one", TaskStatus.READY, context="test")
    result = await service.adopt(
        project_id="p",
        task_ids=["one"],
        target_ref="refs/heads/main",
        head_sha=main,
        reason="report delivered by operator edit",
        operator_id="local",
        accept_equivalent=True,
    )
    assert result["outcome"] == "adopted"
    assert (await db.get_task("one")).status == TaskStatus.COMPLETED
    assert (await service.rows("p"))[-1]["evidence"]["conclusion"] == "not_ci_attested"


async def test_adopt_rejects_stale_target_and_open_children(setup):
    db, service, _source, remote, _repo = setup
    await feature(setup, "parent")
    await db.create_task(
        Task(
            id="child",
            project_id="p",
            title="child",
            description="",
            parent_task_id="parent",
            status=TaskStatus.READY,
        )
    )
    main = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="no longer"):
        await service.adopt(
            project_id="p",
            task_ids=["parent"],
            target_ref="refs/heads/main",
            head_sha="a" * 40,
            reason="test",
            operator_id="local",
            accept_equivalent=True,
        )
    with pytest.raises(ValueError, match="still open"):
        await service.adopt(
            project_id="p",
            task_ids=["parent"],
            target_ref="refs/heads/main",
            head_sha=main,
            reason="test",
            operator_id="local",
            accept_equivalent=True,
        )


async def test_repository_exclusion_prevents_second_publisher(setup):
    db, service, _source, _remote, _repo = setup
    async with service.exclusion("r"):
        other = DevelopmentIntegration(db, data_dir=service.data_dir)
        with pytest.raises(DevelopmentBusy):
            async with other.exclusion("r"):
                pytest.fail("second publisher acquired exclusion")


async def test_crash_after_push_reconciles_without_republishing(setup):
    _db, service, _source, _remote, _repo = setup
    await feature(setup, "one")
    result = await service.sweep("p")
    await service.change(result["id"], state="publishing")
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (
        next(r for r in await service.rows("p") if r["id"] == result["id"])["state"] == "delivered"
    )


def test_focused_policy_requires_commands():
    with pytest.raises(ValueError, match="at least one"):
        DevelopmentPolicy().checked()


async def test_parent_assembly_does_not_require_parent_verifier(setup):
    db, service, _source, remote, _repo = setup
    await db.create_task(
        Task(id="epic", project_id="p", title="epic", description="", status=TaskStatus.PAUSED)
    )
    await feature(setup, "child")
    async with db.immediate() as conn:
        await db.set_parent("child", "epic", conn=conn)
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert "refs/heads/aq/development/parent/" in git(remote, "show-ref")
    assert result["manifest"][0]["parent_task_id"] == "epic"


async def test_conflict_parks_dependent_but_delivers_independent(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "one", filename="base.txt", content="one\n")
    await feature(setup, "two", filename="base.txt", content="two\n")
    await feature(setup, "dependent")
    await feature(setup, "independent")
    await db.add_dependency("dependent", "two", dep_type="blocks")
    result = await service.sweep("p")
    assert {m["task_id"] for m in result["manifest"]} == {"one", "independent"}


async def test_development_status_does_not_report_strict_policy_missing(setup):
    from src.integration.status import IntegrationStatusService

    db, _service, _source, _remote, _repo = setup
    status = await IntegrationStatusService(db).status("p")
    assert status["effective_mode"] == "development"
    assert status["blockers"] == []
    assert status["policy"]["validation"] == "focused"


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("attachment", ["attached", "detached", "missing_attempt", "successor"])
async def test_stopped_writer_preserves_dirty_checkout_before_unlock(setup, confirmed, attachment):
    from unittest.mock import AsyncMock

    from sqlalchemy import insert

    from src.database.tables import (
        agents,
        integration_branch_owners,
        sessions,
        task_session_attempts,
        workspaces,
    )

    db, service, source, _remote, _repo = setup
    await feature(setup, "old")
    # A manually paused task is intentionally not requeued by preservation.
    await db.transition_task("old", TaskStatus.PAUSED, context="test", resume_after=None)
    dirty = source / "unpublished.txt"
    dirty.write_text("irreplaceable work\n")
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="w",
                project_id="p",
                workspace_path=str(source),
                locked_by_task_id="old" if attachment == "attached" else None,
                enabled=True,
                created_at=1,
            )
        )
        await conn.execute(
            insert(sessions).values(
                id="s",
                task_id="old" if attachment == "attached" else None,
                project_id="p",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="old",
                lifecycle="pool",
                state="stopped",
                desired_state="stopped",
                work_dir=str(source),
                epoch="e",
                instance_token="old-instance",
                started_at=1,
                last_claim_epoch=0,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="o",
                repository_id="r",
                ref="old",
                owner_id="old",
                owner_role="worker",
                fence_token=1,
                handoff_state="attached",
                session_id="s",
                workspace_id="w",
                created_at=1,
                updated_at=1,
            )
        )
    async with db.immediate() as conn:
        await conn.execute(insert(agents).values(
            id="a", name="worker", profile_id="worker", state="BUSY", current_task_id="old", created_at=1,
        ))
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(agent_id="a"))
        if attachment in {"detached", "successor"}:
            await conn.execute(insert(task_session_attempts).values(
                id="attempt", session_id="s", task_id="old", project_id="p", agent_id="a",
                profile_id="worker", name="old", lifecycle="pool", harness="codex",
                provider="fake", state="stopped", work_dir=str(source), started_at=1,
                session_started_at=1, ended_at=2,
            ))
        if attachment == "successor":
            await conn.execute(insert(sessions).values(
                id="successor", project_id="p", profile_id="worker", harness="codex",
                provider="fake", name="successor", lifecycle="pool", state="running",
                desired_state="running", work_dir=str(source), epoch="e2",
                instance_token="new-instance", started_at=3,
            ))
    service.confirm_stopped = AsyncMock(return_value=confirmed)
    recovered = confirmed and attachment in {"attached", "detached"}
    result = await service.preserve_stopped_owners("p")
    assert result == (["w"] if recovered else [])
    agent = await db.get_agent("a")
    assert agent.state.value == ("IDLE" if recovered and attachment == "detached" else "BUSY")
    assert dirty.read_text() == "irreplaceable work\n"
    assert git(source, "branch", "--show-current") == ("" if recovered else "old")
    async with db._engine.connect() as conn:
        workspace = (
            (await conn.execute(select(workspaces).where(workspaces.c.id == "w"))).mappings().one()
        )
        owner = (
            (
                await conn.execute(
                    select(integration_branch_owners).where(integration_branch_owners.c.id == "o")
                )
            )
            .mappings()
            .one()
        )
    assert workspace["enabled"] is not recovered
    assert owner["handoff_state"] == ("released" if recovered else "attached")
    assert (await db.get_task("old")).status == TaskStatus.PAUSED


async def test_new_commands_reject_session_principals_without_mutation():
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    handler = IntegrationCommandsMixin()
    with principal_context(
        ExecutionPrincipal(
            kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="s", project_id="p"
        )
    ):
        for command in ("develop", "adopt", "cancel_preserving", "development_sweep"):
            result = await getattr(handler, "_cmd_integration_" + command)({})
            assert result["outcome"] == "unauthorized"


async def test_cancel_detached_repair_does_not_require_human_hold(setup):
    from sqlalchemy import insert

    from src.database.tables import integration_branch_owners as owners
    from src.database.tables import (
        integration_parent_episodes,
    )
    from src.database.tables import (
        integration_repair_operations as operations,
    )
    from src.database.tables import integration_repair_stages as stages

    db, service, _source, remote, _repo = setup
    await feature(setup, "parent")
    await db.create_task(Task(id="repair", project_id="p", title="repair", description=""))
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="r",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1,
            )
        )
        await conn.execute(
            insert(operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="old",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(stages).values(
                operation_id="op",
                ordinal=0,
                policy={},
                state="active",
                starting_sha="a" * 40,
                repair_task_id="repair",
                writer_kind="repair_delegate",
                attempts=0,
            )
        )
        await conn.execute(
            insert(owners).values(
                id="o",
                repository_id="r",
                ref="parent",
                owner_id="repair",
                owner_role="repair",
                fence_token=1,
                handoff_state="reserved",
                created_at=1,
                updated_at=1,
            )
        )
    before = git(remote, "show-ref")
    result = await service.cancel_preserving("op", reason="already manually delivered")
    assert result["outcome"] == "cancelled"
    assert git(remote, "show-ref") == before
    async with db._engine.connect() as conn:
        assert (
            await conn.scalar(select(owners.c.handoff_state).where(owners.c.id == "o"))
            == "released"
        )
        assert (
            await conn.scalar(select(operations.c.state).where(operations.c.id == "op"))
            == "cancelled"
        )
    assert (await service.rows("p"))[-1]["evidence"]["kind"] == "cancel_preserving"


async def test_repair_generation_budget_stops_recursive_dispatch(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    previous = "original"
    for generation in range(1, 4):
        previous = await service.ensure_repair(
            "p",
            "r",
            [{"task_id": previous, "source_sha": "a" * 40}],
            "b" * 40,
            reason="validation failed",
        )
        assert (
            f"Development repair generation: {generation}"
            in (await db.get_task(previous)).description
        )
    assert (
        await service.ensure_repair(
            "p",
            "r",
            [{"task_id": previous, "source_sha": "a" * 40}],
            "b" * 40,
            reason="validation failed",
        )
        is None
    )


def test_development_prime_omits_strict_review_protocol():
    from src.prime.sections import build_completion_protocol_section

    body = build_completion_protocol_section("example", development=True).body
    assert "no squash, PR, hosted CI, or parent verifier is required" in body
    assert "squash its" not in body
    assert "Name that PR" not in body


async def test_untracked_test_output_does_not_block_publication(setup):
    _db, service, _source, _remote, _repo = setup
    await feature(setup, "artifact")
    await service.configure(
        "p",
        {"commands": ["mkdir -p test-cache; echo generated > test-cache/output"]},
        reason="test artifacts are not candidate source",
        operator_id="operator",
    )
    assert (await service.sweep("p"))["outcome"] == "delivered"


async def test_later_consolidation_resolves_conflict_without_starting_repair(setup):
    from sqlalchemy import select

    from src.database.tables import tasks

    db, service, source, _remote, _repo = setup
    await feature(setup, "one", filename="shared", content="one")
    await feature(setup, "two", filename="shared", content="two")
    await feature(setup, "consolidation")
    git(source, "merge", "--no-edit", "one")
    git(source, "merge", "-s", "ours", "--no-edit", "two")
    git(source, "push", "origin", "consolidation")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    async with db._engine.connect() as conn:
        assert (
            await conn.scalar(select(tasks.c.id).where(tasks.c.id.like("development-repair-%")))
            is None
        )
    assert not [row for row in await service.rows("p") if row["state"] == "parked"]
