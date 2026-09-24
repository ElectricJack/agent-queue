"""``StaleOwnerRelease`` -- the bulk release a project's integration drain waits on.

Real PostgreSQL and real Git: a bare ``origin.git`` and a base clone registered
as the project's checkout.  Owner rows, tasks, batches, operations and leases
are inserted directly, so each case states exactly the durable facts the
release reads.
"""

from __future__ import annotations

import subprocess
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    events,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_owner_recoveries,
    integration_repair_operations,
    integration_review_evidence,
    project_integration_leases,
    projects,
    sessions,
    task_comments,
    workspaces,
)
from src.git.manager import GitManager
from src.integration.owner_recovery import RECOVERED_EVENT
from src.integration.stale_owners import LEASE_RELEASED_EVENT, StaleOwnerRelease
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.db_fixtures import lease_dsn

NOW = 10_000.0
PRINCIPAL = "human:local-operator"


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
async def env(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    base = tmp_path / "base"
    git(tmp_path, "clone", str(origin), str(base))
    git(base, "config", "user.name", "Tester")
    git(base, "config", "user.email", "tester@example.test")
    (base / "base.txt").write_text("base\n")
    git(base, "add", ".")
    git(base, "commit", "-m", "base")
    git(base, "push", "origin", "main")
    db = Database(lease_dsn("stale-owners.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    )
    async with db.immediate() as conn:
        # The state a drain requested from development leaves behind.
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                integration_repository_id="r",
                hierarchical_integration_mode="development",
                hierarchical_integration_desired_mode="disabled",
                hierarchical_integration_draining=True,
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="ws-base", project_id="p", workspace_path=str(base), created_at=1.0
            )
        )
    yield Env(db=db, origin=origin, base=base)
    await db.close()


class Env:
    def __init__(self, *, db, origin, base):
        self.db, self.origin, self.base = db, origin, base

    def branch(self, name: str, *, landed: bool) -> str:
        """Push *name* with one commit; *landed* also fast-forwards main onto it."""
        git(self.base, "checkout", "-q", "-b", name, "main")
        (self.base / f"{name.replace('/', '-')}.txt").write_text("work\n")
        git(self.base, "add", ".")
        git(self.base, "commit", "-q", "-m", name)
        git(self.base, "push", "-q", "origin", name)
        tip = git(self.base, "rev-parse", "HEAD")
        git(self.base, "checkout", "-q", "main")
        if landed:
            git(self.base, "merge", "-q", "--ff-only", name)
            git(self.base, "push", "-q", "origin", "main")
        return tip

    def delete(self, name: str) -> None:
        """Remove *name* from origin and from the base clone."""
        git(self.base, "push", "-q", "origin", "--delete", name)
        git(self.base, "branch", "-q", "-D", name)

    async def task(self, task_id: str, status: TaskStatus) -> None:
        await self.db.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
        await self.db.update_task(task_id, status=status)

    async def owner(
        self,
        row_id: str,
        ref: str,
        owner_id: str,
        *,
        state: str = "reserved",
        role: str = "worker",
        session_id: str | None = None,
        workspace_id: str | None = None,
        updated_at: float = 1.0,
    ) -> None:
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id=row_id,
                    repository_id="r",
                    ref=ref,
                    owner_id=owner_id,
                    owner_role=role,
                    fence_token=3,
                    handoff_state=state,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    created_at=1.0,
                    updated_at=updated_at,
                )
            )

    async def batch(self, batch_id: str, lifecycle: str, *, cleanup_state: str = "pending"):
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(integration_batches).values(
                    id=batch_id,
                    project_id="p",
                    repository_id="r",
                    request_id=f"{batch_id}-request",
                    trigger="manual",
                    source_manifest_digest="sha256:" + "d" * 64,
                    base_sha="a" * 40,
                    lifecycle=lifecycle,
                    integration_branch=f"refs/heads/aq/integration/{batch_id}",
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state=cleanup_state,
                    created_at=1.0,
                    updated_at=1.0,
                )
            )

    async def row(self, row_id: str) -> dict:
        async with self.db._engine.connect() as conn:
            return dict(
                (
                    await conn.execute(
                        select(integration_branch_owners).where(
                            integration_branch_owners.c.id == row_id
                        )
                    )
                )
                .mappings()
                .one()
            )

    async def audits(self) -> list[dict]:
        async with self.db._engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(integration_owner_recoveries).order_by(
                            integration_owner_recoveries.c.owner_row_id
                        )
                    )
                ).mappings()
            ]

    async def events(self, event_type: str) -> list[dict]:
        async with self.db._engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(select(events).where(events.c.event_type == event_type))
                ).mappings()
            ]

    async def comments(self, task_id: str) -> list[str]:
        async with self.db._engine.connect() as conn:
            return list(
                (
                    await conn.execute(
                        select(task_comments.c.body).where(task_comments.c.task_id == task_id)
                    )
                ).scalars()
            )

    async def run(self, **kwargs) -> dict:
        service = StaleOwnerRelease(self.db, GitManager(), None, clock=lambda: NOW)
        return await service.run("p", principal=PRINCIPAL, **kwargs)


def _by_row(result: dict) -> dict[str, dict]:
    return {item["owner_row_id"]: item for item in result["outcomes"]}


async def test_only_provably_safe_reserved_rows_are_released(env):
    env.branch("aq/landed", landed=True)
    await env.task("landed", TaskStatus.COMPLETED)
    await env.owner("o-landed", "aq/landed", "landed")

    env.branch("aq/gone", landed=False)
    env.delete("aq/gone")
    await env.task("gone", TaskStatus.COMPLETED)
    await env.owner("o-gone", "aq/gone", "gone")

    # The owning task was deleted: nothing can ever act for it again.
    env.branch("aq/deleted", landed=False)
    env.delete("aq/deleted")
    await env.owner("o-deleted", "aq/deleted", "deleted")

    env.branch("aq/failed-landed", landed=True)
    await env.task("failed-landed", TaskStatus.FAILED)
    await env.owner("o-failed-landed", "aq/failed-landed", "failed-landed")

    env.branch("aq/unlanded", landed=False)
    await env.task("unlanded", TaskStatus.COMPLETED)
    await env.owner("o-unlanded", "aq/unlanded", "unlanded")

    env.branch("aq/running", landed=True)
    await env.task("running", TaskStatus.IN_PROGRESS)
    await env.owner("o-running", "aq/running", "running")

    # A FAILED task may be retried, so its gone branch proves nothing.
    env.branch("aq/failed-gone", landed=False)
    env.delete("aq/failed-gone")
    await env.task("failed-gone", TaskStatus.FAILED)
    await env.owner("o-failed-gone", "aq/failed-gone", "failed-gone")

    env.branch("aq/attached", landed=True)
    await env.task("attached", TaskStatus.COMPLETED)
    await env.owner("o-attached", "aq/attached", "attached", state="attached")

    result = await env.run()

    assert (result["outcome"], result["dry_run"], result["count"]) == ("released", False, 4)
    verdicts = {
        row_id: (item["outcome"], item["reason"]) for row_id, item in _by_row(result).items()
    }
    assert verdicts == {
        "o-landed": ("released", "tip_on_default_branch"),
        "o-gone": ("released", "ref_gone"),
        "o-deleted": ("released", "ref_gone"),
        "o-failed-landed": ("released", "tip_on_default_branch"),
        "o-unlanded": ("not_eligible", "branch_not_on_default"),
        "o-running": ("not_eligible", "owner_active"),
        "o-failed-gone": ("not_eligible", "failed_owner_ref_gone"),
        "o-attached": ("not_eligible", "not_reserved"),
    }
    assert "release-owner --owner-row-id o-attached" in _by_row(result)["o-attached"]["detail"]

    released = {"o-landed", "o-gone", "o-deleted", "o-failed-landed"}
    for row_id in released:
        row = await env.row(row_id)
        # The same compare-and-swap ``release-owner`` makes: a fresh fence.
        assert (row["handoff_state"], row["fence_token"]) == ("released", 4), row_id
        assert _by_row(result)[row_id]["released_fence_token"] == 4
    for row_id in ("o-unlanded", "o-running", "o-failed-gone", "o-attached"):
        row = await env.row(row_id)
        assert row["fence_token"] == 3, row_id
        assert row["handoff_state"] in {"reserved", "attached"}, row_id

    audits = await env.audits()
    assert [audit["owner_row_id"] for audit in audits] == sorted(released)
    assert {audit["outcome"] for audit in audits} == {"released"}
    assert {audit["principal"] for audit in audits} == {PRINCIPAL}
    assert {audit["reason"] for audit in audits} == {None}
    assert {audit["evidence"]["control"] for audit in audits} == {"release_stale_owners"}
    assert len(await env.events(RECOVERED_EVENT)) == 4
    assert any("release-stale-owners" in body for body in await env.comments("landed"))

    # Idempotent: released rows are not selected again, and the kept rows
    # keep their verdicts.
    again = await env.run()
    assert (again["outcome"], again["count"]) == ("nothing_to_release", 0)
    assert set(_by_row(again)) == {"o-unlanded", "o-running", "o-failed-gone", "o-attached"}
    assert len(await env.audits()) == 4


async def test_dry_run_reports_the_verdicts_and_changes_nothing(env):
    env.branch("aq/landed", landed=True)
    await env.task("landed", TaskStatus.COMPLETED)
    await env.owner("o-landed", "aq/landed", "landed")
    env.branch("aq/unlanded", landed=False)
    await env.task("unlanded", TaskStatus.COMPLETED)
    await env.owner("o-unlanded", "aq/unlanded", "unlanded")

    result = await env.run(dry_run=True)

    assert (result["outcome"], result["dry_run"], result["count"]) == ("released", True, 1)
    landed = _by_row(result)["o-landed"]
    assert (landed["outcome"], landed["reason"], landed["dry_run"]) == (
        "released",
        "tip_on_default_branch",
        True,
    )
    assert _by_row(result)["o-unlanded"]["reason"] == "branch_not_on_default"
    row = await env.row("o-landed")
    assert (row["handoff_state"], row["fence_token"]) == ("reserved", 3)
    assert await env.audits() == []
    assert await env.events(RECOVERED_EVENT) == []
    assert await env.comments("landed") == []


async def test_a_live_writer_or_a_named_session_keeps_a_finished_owners_row(env):
    env.branch("aq/live", landed=True)
    await env.task("live", TaskStatus.COMPLETED)
    await env.owner("o-live", "aq/live", "live")
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(sessions).values(
                id="s-live",
                task_id="live",
                project_id="p",
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="n-s-live",
                lifecycle="pool",
                state="running",
                desired_state="running",
                work_dir=str(env.base),
                epoch="e",
                instance_token="tok-live",
                started_at=1.0,
            )
        )
    env.branch("aq/named", landed=True)
    await env.task("named", TaskStatus.COMPLETED)
    await env.owner("o-named", "aq/named", "named", session_id="s-old")

    verdicts = _by_row(await env.run())

    assert (verdicts["o-live"]["outcome"], verdicts["o-live"]["reason"]) == (
        "not_eligible",
        "owner_blocked",
    )
    assert "s-live" in verdicts["o-live"]["detail"]
    assert verdicts["o-named"]["reason"] == "not_reserved"
    assert (await env.row("o-live"))["handoff_state"] == "reserved"


async def test_fences_a_batch_still_relies_on_are_kept(env):
    # A completed operation's collector row owns a promoted batch's
    # integration branch; that branch's local-ref cleanup checks its fence.
    await env.batch("b", "promoted", cleanup_state="pending")
    env.branch("aq/integration/b", landed=True)
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op-b",
                target_kind="batch",
                batch_id="b",
                episode_id="b",
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    await env.owner("o-collector", "refs/heads/aq/integration/b", "op-b", role="collector")
    # A promoted batch's member is released: its work landed, and cleanup
    # will not delete a source ref whose owner is not released.
    env.branch("aq/promoted-member", landed=True)
    await env.task("promoted-member", TaskStatus.COMPLETED)
    await env.owner("o-promoted-member", "aq/promoted-member", "promoted-member")
    # A running batch's member is kept.  Membership is written while the
    # batch seals; a trigger freezes it after.
    await env.batch("a", "sealing")
    env.branch("aq/active-member", landed=True)
    await env.task("active-member", TaskStatus.COMPLETED)
    await env.owner("o-active-member", "aq/active-member", "active-member")
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review",
                source_task_id="active-member",
                repository_id="r",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                review_kind="human",
                generation=0,
                verdict="approved",
                evidence={},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="a",
                ordinal=0,
                task_id="active-member",
                repository_id="r",
                source_base_sha="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                source_ref="refs/heads/aq/active-member",
                source_ref_retention="delete",
                review_evidence_id="review",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "a")
            .values(lifecycle="building")
        )

    verdicts = _by_row(await env.run())

    assert verdicts["o-collector"]["reason"] == "batch_cleanup_pending"
    assert "retry-cleanup b" in verdicts["o-collector"]["detail"]
    assert verdicts["o-promoted-member"]["outcome"] == "released"
    assert verdicts["o-active-member"]["reason"] == "batch_active"

    # Once the batch's cleanup completes, its collector row goes too.
    async with env.db.immediate() as conn:
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "b")
            .values(cleanup_state="complete")
        )
    collector = _by_row(await env.run())["o-collector"]
    assert (collector["outcome"], collector["reason"]) == ("released", "tip_on_default_branch")
    audit = next(a for a in await env.audits() if a["owner_row_id"] == "o-collector")
    assert audit["task_id"] is None


async def test_older_than_leaves_recently_changed_rows_alone(env):
    env.branch("aq/old", landed=True)
    await env.task("old", TaskStatus.COMPLETED)
    await env.owner("o-old", "aq/old", "old", updated_at=1.0)
    env.branch("aq/recent", landed=True)
    await env.task("recent", TaskStatus.COMPLETED)
    await env.owner("o-recent", "aq/recent", "recent", updated_at=NOW - 60.0)

    verdicts = _by_row(await env.run(older_than_seconds=3600.0))

    assert verdicts["o-old"]["outcome"] == "released"
    assert verdicts["o-recent"]["reason"] == "too_recent"
    assert (await env.row("o-recent"))["handoff_state"] == "reserved"


async def test_an_unreachable_origin_releases_nothing(env):
    env.branch("aq/landed", landed=True)
    await env.task("landed", TaskStatus.COMPLETED)
    await env.owner("o-landed", "aq/landed", "landed")
    env.origin.rename(env.origin.with_name("moved.git"))

    result = await env.run()

    assert result["outcome"] == "nothing_to_release"
    assert _by_row(result)["o-landed"]["reason"] == "origin_unreachable"
    assert (await env.row("o-landed"))["handoff_state"] == "reserved"


async def test_the_expired_lease_of_a_finished_batch_is_released(env):
    await env.batch("old", "aborted")
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="r",
                batch_id="old",
                owner_id="sealer-old",
                fence_token=2,
                heartbeat_at=1.0,
                expires_at=NOW + 60.0,
            )
        )

    live = await env.run()
    assert live["outcome"] == "nothing_to_release"
    assert [(lease["outcome"], lease["reason"]) for lease in live["leases"]] == [
        ("not_eligible", "lease_live")
    ]

    async with env.db.immediate() as conn:
        await conn.execute(
            update(project_integration_leases)
            .where(project_integration_leases.c.project_id == "p")
            .values(expires_at=NOW - 60.0)
        )
    dry = await env.run(dry_run=True)
    assert [(lease["outcome"], lease["dry_run"]) for lease in dry["leases"]] == [("released", True)]

    released = await env.run()
    assert (released["outcome"], released["count"]) == ("released", 1)
    assert released["leases"][0]["batch_lifecycle"] == "aborted"
    async with env.db._engine.connect() as conn:
        remaining = (
            (await conn.execute(select(project_integration_leases.c.project_id))).scalars().all()
        )
    assert remaining == []
    assert len(await env.events(LEASE_RELEASED_EVENT)) == 1
    assert (await env.run())["leases"] == []


async def test_an_expired_lease_of_a_running_batch_is_kept(env):
    await env.batch("running", "building")
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="r",
                batch_id="running",
                owner_id="sealer-running",
                fence_token=2,
                heartbeat_at=1.0,
                expires_at=2.0,
            )
        )
    leases = (await env.run())["leases"]
    assert [(lease["outcome"], lease["reason"]) for lease in leases] == [
        ("not_eligible", "batch_active")
    ]


async def test_an_unknown_project_is_not_found(env):
    service = StaleOwnerRelease(env.db, GitManager(), None, clock=time.time)
    result = await service.run("nope", principal=PRINCIPAL)
    assert result == {"outcome": "not_found", "project_id": "nope"}
