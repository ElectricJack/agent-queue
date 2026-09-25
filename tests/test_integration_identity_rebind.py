"""``aq integration rebind-reused-identity``: prove, then retire an inherited identity.

Regression for amber-summit / amber-harbor (2026-09-25): 23 live tasks were
minted onto deleted tasks' names before ``_task_identity_exists`` reserved
integration identities, and inherited the predecessor's branch origin and
checkpoint.  Real PostgreSQL and real Git: a bare ``origin.git`` and a clone
the tests push from.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts.integration import IntegrationOperationalValue
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import Database
from src.database.tables import (
    events,
    integration_branch_owners,
    projects,
    sessions,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.doctor.integration_checks import CHECKS
from src.doctor.models import DoctorContext, Severity
from src.git.manager import GitManager
from src.integration.development import DevelopmentIntegration
from src.integration.identity_rebind import (
    ABSENT,
    CHECKPOINT_MISMATCH,
    CHECKPOINT_REWRITTEN,
    DEPENDENT_HISTORY,
    DISCARDED,
    EVENT,
    HIERARCHICAL_PROJECT,
    LIVE_WRITER,
    NOT_ON_DEFAULT_BRANCH,
    ON_DEFAULT_BRANCH,
    OWNER_HELD,
    UNAVAILABLE,
    ReusedIdentityRebind,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.profiles.capabilities import DENY_ALL
from tests.db_fixtures import lease_dsn

PRINCIPAL = "supervisor session:super-p"
PREDECESSOR_AT = 100.0
TASK_AT = 200.0


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class Env:
    def __init__(self, *, db, clone, tmp_path):
        self.db, self.clone, self.tmp_path = db, clone, tmp_path
        self.base = git(clone, "rev-parse", "HEAD")

    def push(self, branch: str, *, landed: bool) -> str:
        """Push *branch* with one commit; *landed* also fast-forwards main onto it."""
        git(self.clone, "checkout", "-q", "-B", branch, "main")
        (self.clone / f"{branch.replace('/', '-')}.txt").write_text(f"{branch}\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", branch)
        git(self.clone, "push", "-q", "-f", "origin", branch)
        tip = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "checkout", "-q", "main")
        if landed:
            git(self.clone, "merge", "-q", "--ff-only", branch)
            git(self.clone, "push", "-q", "origin", "main")
        return tip

    def remote_tip(self, branch: str) -> str | None:
        out = git(self.clone, "ls-remote", "origin", f"refs/heads/{branch}")
        return out.split()[0] if out else None

    async def reused(
        self,
        task_id: str = "reused",
        *,
        status: TaskStatus = TaskStatus.COMPLETED,
        branch: str | None = "aq/reused",
        checkpoint_sha: str | None = None,
        checkpoint_at: float = PREDECESSOR_AT,
        checkpoint_branch: str | None = None,
        owner_state: str | None = "released",
        repository_id: str = "r",
    ) -> None:
        """A live task minted onto a deleted predecessor's name and identity."""
        await self.db.create_task(
            Task(
                id=task_id, project_id="p", title=task_id, description="", repo_id="r",
                branch_name=branch,
            )
        )
        await self.db.update_task(task_id, status=status)
        async with self.db.immediate() as conn:
            # create_task stamps its own time; seed the historical snapshot.
            await conn.execute(
                update(tasks).where(tasks.c.id == task_id).values(created_at=TASK_AT)
            )
            await conn.execute(
                insert(task_branch_origins).values(
                    id=f"origin-{task_id}", task_id=task_id, repository_id=repository_id,
                    branch_name=branch, parent_ref="main", base_sha=self.base,
                    creation_generation=0, reserved=True, materialized=True,
                    created_at=PREDECESSOR_AT, materialized_at=PREDECESSOR_AT,
                )
            )
            await conn.execute(
                insert(task_integration_checkpoints).values(
                    task_id=task_id, repository_id=repository_id,
                    branch=checkpoint_branch or branch or f"aq/{task_id}",
                    checkpoint_sha=checkpoint_sha or self.base, branch_owner_id=task_id,
                    updated_at=checkpoint_at,
                )
            )
            if owner_state is not None and branch is not None:
                await conn.execute(
                    insert(integration_branch_owners).values(
                        id=f"owner-{task_id}", repository_id=repository_id, ref=branch,
                        owner_id=task_id, owner_role="worker", fence_token=3,
                        handoff_state=owner_state, created_at=PREDECESSOR_AT,
                        updated_at=150.0,
                    )
                )

    def service(self) -> ReusedIdentityRebind:
        development = DevelopmentIntegration(
            self.db, data_dir=self.tmp_path / "data", git=GitManager()
        )
        return ReusedIdentityRebind(self.db, development=development, clock=lambda: 300.0)

    async def rows(self, task_id: str = "reused") -> dict:
        async with self.db._engine.connect() as conn:
            origin = (
                await conn.execute(
                    select(task_branch_origins).where(task_branch_origins.c.task_id == task_id)
                )
            ).mappings().all()
            checkpoint = (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == task_id
                    )
                )
            ).mappings().one_or_none()
            owners = (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.owner_id == task_id
                    )
                )
            ).mappings().all()
            rebound = (
                await conn.execute(
                    select(events.c.payload).where(
                        events.c.event_type == EVENT, events.c.task_id == task_id
                    )
                )
            ).scalars().all()
        return {
            "origins": [dict(row) for row in origin],
            "checkpoint": dict(checkpoint) if checkpoint is not None else None,
            "owners": [dict(row) for row in owners],
            "events": [json.loads(payload) for payload in rebound],
        }


@pytest.fixture
async def env(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.name", "Tester")
    git(clone, "config", "user.email", "tester@example.test")
    (clone / "base.txt").write_text("base\n")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "base")
    git(clone, "push", "-q", "origin", "main")
    db = Database(lease_dsn("identity-rebind.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                integration_repository_id="r",
                hierarchical_integration_mode="development",
                hierarchical_integration_desired_mode="development",
            )
        )
    yield Env(db=db, clone=clone, tmp_path=tmp_path)
    await db.close()


def _states(result) -> dict[str, str]:
    """``{sha or 'absent': state}`` across the result's origins."""
    return {
        proof["sha"] or "absent": proof["state"]
        for origin in result["outcomes"]
        for proof in origin["proofs"]
    }


async def test_delivered_predecessor_is_proved_then_rebound_keeping_its_evidence(env):
    # The successor kept pushing to the shared branch name; all of it landed.
    tip = env.push("aq/reused", landed=True)
    await env.reused()
    before = await env.rows()
    service = env.service()

    preview = await service.run("reused", principal=PRINCIPAL)

    assert preview["outcome"] == "would_rebind"
    assert preview["dry_run"] is True and preview["count"] == 1
    assert preview["unproven"] == ()
    assert _states(preview) == {env.base: ON_DEFAULT_BRANCH, tip: ON_DEFAULT_BRANCH}
    [origin] = preview["outcomes"]
    assert origin["id"] == "origin-reused" and origin["default_sha"] == tip
    assert preview["head_sha"] == tip
    assert preview["evidence"]["checkpoint"]["checkpoint_sha"] == env.base
    assert "not observable on origin" in preview["evidence"]["proof_limit"]
    assert await env.rows() == before  # a dry run writes nothing

    applied = await service.run(
        "reused", principal=PRINCIPAL, dry_run=False,
        expected_origin_ids=["origin-reused"], reason="amber-harbor incident",
    )

    assert applied["outcome"] == "rebound" and applied["count"] == 1
    after = await env.rows()
    [retired] = after["origins"]
    # The origin row stays, identical but for retired_at.
    assert retired == {**before["origins"][0], "retired_at": 300.0}
    assert after["checkpoint"] is None
    assert after["owners"] == before["owners"]  # the fence history is untouched
    [event] = after["events"]
    assert applied["id"] and event["principal"] == PRINCIPAL
    assert event["reason"] == "amber-harbor incident"
    assert event["checkpoint"] == before["checkpoint"]  # kept verbatim
    assert event["origins"][0]["id"] == "origin-reused"
    assert event["discarded_tips"] == []
    assert env.remote_tip("aq/reused") == tip  # no ref is touched
    comments = await env.db.list_task_comments("reused")
    assert any("rebind-reused-identity" in c["body"] for c in comments["comments"])

    # The doctor check no longer reports it, and the command is idempotent.
    check = next(c for c in CHECKS if c.id == "integration.reused_task_identity")
    report = await check.run(DoctorContext(config=None, db=env.db))
    assert report.severity is Severity.OK
    again = await service.run("reused", principal=PRINCIPAL)
    assert again["outcome"] == "nothing_to_rebind"


async def test_a_gone_branch_with_a_delivered_record_is_provable(env):
    await env.reused()

    result = await env.service().run("reused", principal=PRINCIPAL)

    assert result["outcome"] == "would_rebind"
    assert _states(result) == {env.base: ON_DEFAULT_BRANCH, "absent": ABSENT}


async def test_an_undelivered_tip_needs_an_exact_discard(env):
    tip = env.push("aq/reused", landed=False)
    await env.reused()
    service = env.service()

    unproven = await service.run("reused", principal=PRINCIPAL)

    assert unproven["outcome"] == "unproven"
    assert _states(unproven)[tip] == NOT_ON_DEFAULT_BRANCH
    [line] = unproven["unproven"]
    assert line.startswith(f"{NOT_ON_DEFAULT_BRANCH}: aq/reused (branch_tip) {tip}")
    assert f"--discard-tip {tip}" in line
    refused = await service.run(
        "reused", principal=PRINCIPAL, dry_run=False,
        expected_origin_ids=["origin-reused"], reason="try anyway",
    )
    assert refused["outcome"] == "unproven"
    assert (await env.rows())["origins"][0]["retired_at"] is None

    stray = await service.run("reused", principal=PRINCIPAL, discard_tips=[env.base])
    assert stray["outcome"] == "invalid"
    assert env.base in stray["error"]

    discarded = await service.run(
        "reused", principal=PRINCIPAL, dry_run=False, discard_tips=[tip],
        expected_origin_ids=["origin-reused"], reason="predecessor work abandoned",
    )

    assert discarded["outcome"] == "rebound"
    [proof] = [p for o in discarded["outcomes"] for p in o["proofs"] if p["sha"] == tip]
    assert proof == {
        "sha": tip, "sources": ["branch_tip"], "state": DISCARDED,
        "observed": NOT_ON_DEFAULT_BRANCH,
    }
    [event] = (await env.rows())["events"]
    assert event["discarded_tips"] == [tip]
    assert env.remote_tip("aq/reused") == tip  # a discard records; it deletes nothing


async def test_a_checkpoint_commit_origin_lacks_is_unavailable(env):
    missing = "c" * 40
    await env.reused(checkpoint_sha=missing)

    result = await env.service().run("reused", principal=PRINCIPAL)

    assert result["outcome"] == "unproven"
    assert _states(result)[missing] == UNAVAILABLE
    [origin] = result["outcomes"]
    [proof] = [p for p in origin["proofs"] if p["sha"] == missing]
    assert proof["sources"] == ["checkpoint"]
    settled = await env.service().run(
        "reused", principal=PRINCIPAL, discard_tips=[missing]
    )
    assert settled["outcome"] == "would_rebind"


@pytest.mark.parametrize(
    ("setup", "cause"),
    [
        ({"status": TaskStatus.IN_PROGRESS}, LIVE_WRITER),
        ({"owner_state": "reserved"}, OWNER_HELD),
        ({"checkpoint_at": TASK_AT}, CHECKPOINT_REWRITTEN),
        ({"checkpoint_branch": "aq/elsewhere"}, CHECKPOINT_MISMATCH),
    ],
)
async def test_database_refusals_are_listed_and_block_apply(env, setup, cause):
    await env.reused(**setup)
    before = await env.rows()
    service = env.service()

    preview = await service.run("reused", principal=PRINCIPAL)
    applied = await service.run(
        "reused", principal=PRINCIPAL, dry_run=False,
        expected_origin_ids=["origin-reused"], reason="incident",
    )

    assert preview["outcome"] == applied["outcome"] == "unproven"
    assert any(line.startswith(f"{cause}: ") for line in preview["unproven"])
    assert await env.rows() == before


async def test_live_sessions_history_and_hierarchical_projects_refuse(env):
    await env.reused()
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(sessions).values(
                id="s-1", task_id="reused", project_id="p", profile_id="worker-claude",
                harness="claude", provider="fake", name="s-reused", lifecycle="task",
                state="running", work_dir="/tmp/w", epoch="e", instance_token="i",
                started_at=250.0,
            )
        )
        # A child branched off the inherited identity depends on it.
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-child", task_id="reused.1", repository_id="r",
                branch_name="aq/reused.1", parent_task_id="reused", parent_repository_id="r",
                parent_ref="aq/reused", base_sha=env.base, creation_generation=0,
                reserved=True, materialized=False, created_at=250.0,
            )
        )
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_mode="train",
                hierarchical_integration_desired_mode="train",
            )
        )

    result = await env.service().run("reused", principal=PRINCIPAL)

    causes = {line.split(":", 1)[0] for line in result["unproven"]}
    assert result["outcome"] == "unproven"
    assert {LIVE_WRITER, DEPENDENT_HISTORY, HIERARCHICAL_PROJECT} <= causes
    assert any("live child origins" in line for line in result["unproven"])


async def test_an_origin_left_by_another_projects_task_is_proved_in_its_repository(env):
    """bold-harbor/stark-bridge: an agent-queue task inherited a matter-engine origin."""
    origin_url = git(env.clone, "remote", "get-url", "origin")
    await env.db.create_project(Project(id="q", name="Q"))
    await env.db.create_repo(
        RepoConfig(id="rq", project_id="q", source_type=RepoSourceType.CLONE, url=origin_url)
    )
    await env.reused(repository_id="rq")
    service = env.service()

    development = await service.run("reused", principal=PRINCIPAL)
    assert development["outcome"] == "would_rebind"
    assert development["evidence"]["modes"] == {"p": "development", "q": "disabled"}

    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "q")
            .values(
                hierarchical_integration_mode="train",
                hierarchical_integration_desired_mode="train",
            )
        )
    train = await service.run("reused", principal=PRINCIPAL)
    assert train["outcome"] == "unproven"
    [line] = train["unproven"]
    assert line.startswith(f"{HIERARCHICAL_PROJECT}: project q is in train mode")


async def test_apply_compares_the_origin_set_and_reproves_under_the_lock(env):
    await env.reused()
    service = env.service()

    wrong = await service.run(
        "reused", principal=PRINCIPAL, dry_run=False,
        expected_origin_ids=["origin-other"], reason="incident",
    )
    assert wrong["outcome"] == "changed"
    assert "origin-reused" in wrong["error"]

    # An owner that reappears between the proof and the apply stops it.
    prove = service._prove

    async def prove_then_reserve(identity):
        proofs = await prove(identity)
        async with env.db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.id == "owner-reused")
                .values(handoff_state="reserved", fence_token=4)
            )
        return proofs

    service._prove = prove_then_reserve
    moved = await service.run(
        "reused", principal=PRINCIPAL, dry_run=False,
        expected_origin_ids=["origin-reused"], reason="incident",
    )

    assert moved["outcome"] == "changed"
    rows = await env.rows()
    assert rows["origins"][0]["retired_at"] is None
    assert rows["checkpoint"] is not None and rows["events"] == []


async def test_nothing_to_rebind_and_not_found(env):
    await env.db.create_task(
        Task(id="fresh", project_id="p", title="fresh", description="", repo_id="r")
    )
    service = env.service()

    assert (await service.run("fresh", principal=PRINCIPAL))["outcome"] == "nothing_to_rebind"
    assert (await service.run("ghost", principal=PRINCIPAL))["outcome"] == "not_found"
    unaudited = await service.run(
        "fresh", principal=PRINCIPAL, dry_run=False, expected_origin_ids=["x"]
    )
    assert unaudited["outcome"] == "invalid"


def _worker(project_id: str) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="worker",
        project_id=project_id,
        elevated=False,
    )


async def test_command_validates_authorizes_and_applies(env):
    tip = env.push("aq/reused", landed=True)
    await env.reused()
    handler = IntegrationCommandsMixin()
    handler.db = env.db
    handler.orchestrator = SimpleNamespace(
        git=GitManager(),
        development_integration=DevelopmentIntegration(
            env.db, data_dir=env.tmp_path / "data", git=GitManager()
        ),
    )

    preview = await handler._cmd_integration_rebind_reused_identity({"task_id": "reused"})
    assert (preview["success"], preview["outcome"], preview["head_sha"]) == (
        True, "would_rebind", tip,
    )
    for bad in (
        {"task_id": "reused", "dry_run": False, "reason": "incident"},
        {"task_id": "reused", "dry_run": False, "expected_origin_ids": ["origin-reused"]},
        {"task_id": "reused", "discard_tips": ["abc123"]},
    ):
        refused = await handler._cmd_integration_rebind_reused_identity(bad)
        assert (refused["success"], refused["outcome"]) == (False, "invalid"), bad
    missing = await handler._cmd_integration_rebind_reused_identity({"task_id": "ghost"})
    assert missing["outcome"] == "not_found"
    with principal_context(_worker("p")):
        unauthorized = await handler._cmd_integration_rebind_reused_identity(
            {"task_id": "reused"}
        )
    assert unauthorized["outcome"] == "unauthorized"

    applied = await handler._cmd_integration_rebind_reused_identity(
        {
            "task_id": "reused",
            "dry_run": False,
            "expected_origin_ids": ["origin-reused"],
            "reason": "incident",
        }
    )
    assert (applied["success"], applied["outcome"]) == (True, "rebound")
    [event] = (await env.rows())["events"]
    assert event["principal"] == "human:local-operator"
    # Every result travels through the shared operational value unchanged.
    for result in (preview, applied, missing):
        fields = IntegrationOperationalValue.model_fields
        value = IntegrationOperationalValue(**{k: result[k] for k in fields if k in result})
        assert set(result) - set(fields) <= {"success", "outcome", "error"}
        assert value.task_id == result.get("task_id")
