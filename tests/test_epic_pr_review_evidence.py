"""A GitHub pull-request verdict becomes exact train review evidence."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_source_ci,
    task_labels,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    project_integration_schedules,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.doctor.stall_checks import _unmaterialized_pr_findings
from src.git.manager import GitManager
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.root_materialization import RootMaterialization
from src.integration.scheduler import TrainService
from src.integration.settling import settled
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn
from tests.test_integration_sealing import _policy


async def _continuous_policy(case):
    policy = _policy()
    policy["root"]["admission"] = "authorized"
    policy["root"]["required_checks"]["producer_id"] = "7"
    policy["root"]["repair"].update(source_ci=True, on_exhausted="continue", conflict_scope="batch")
    await case["db"].update_project("p", hierarchical_integration_policy=policy)
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(task_type="feature"))
    return policy


async def test_authorized_admission_is_exact_audited_and_preserves_reviewer_hold(case):
    await _continuous_policy(case)
    evidence = await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0)
    assert evidence["evidence"]["decision_path"] == "authorized_task"
    assert evidence["reviewed_tree_sha"] == case["tree"]
    assert (await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0))["id"] == evidence["id"]
    assert await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=1) is None
    async with case["db"].immediate() as conn:
        await conn.execute(insert(task_labels).values(task_id="e1", label="hold:product"))
    assert await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0) is None


async def test_authorization_does_not_override_rejected_human_review(case):
    await _continuous_policy(case)
    await case["producer"].snapshot_from_pull_request(
        "e1", verdict="rejected", reviewer_login="reviewer", reviewed_sha=case["first"])
    assert await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0) is None


async def test_explicit_authorization_admits_chore_without_retagging_parent(case):
    policy = await _continuous_policy(case)
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(task_type="chore"))
    assert await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0
    ) is None
    policy["root"]["authorized_task_ids"] = ["e1"]
    await case["db"].update_project("p", hierarchical_integration_policy=policy)
    evidence = await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0
    )
    assert evidence["review_kind"] == "parent"
    assert evidence["evidence"]["verification_id"] == "verification-e1"
    assert (await case["db"].get_task("e1")).task_type.value == "chore"
    async with case["db"].immediate() as conn:
        await conn.execute(insert(task_labels).values(task_id="e1", label="hold:product"))
    assert await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0
    ) is None


@pytest.mark.parametrize("conclusion,state", [("failure", "red"), ("cancelled", "cancelled")])
async def test_source_ci_repair_is_deduplicated_and_replaced_only_after_failure(case, tmp_path, conclusion, state):
    import asyncio
    from unittest.mock import AsyncMock
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.orchestrator import Orchestrator
    from src.integration.models import HierarchicalIntegrationPolicy
    from src.integration.source_ci import SourceCIObservation, classify_source_checks
    from src.vault import ensure_default_intelligence_classes
    from src.models import Task, TaskStatus, TaskType

    policy = HierarchicalIntegrationPolicy.model_validate(await _continuous_policy(case))
    async with case["db"]._engine.connect() as conn:
        source = await case["producer"]._pull_request_source_on(conn, "e1")
    entries = [{"id": 1, "name": policy.root.required_checks.names[0],
                "head_sha": source["head"], "app": {"id": int(policy.root.required_checks.producer_id)},
                "status": "completed", "conclusion": conclusion,
                "html_url": "https://github.com/o/r/actions/runs/1",
                "output": {"summary": "tests/test_source.py::test_delivery failed"}}]
    verdict, checks = classify_source_checks(entries, head=source["head"], required=policy.root.required_checks)
    assert verdict == state
    data = str(tmp_path / "handler-data")
    ensure_default_intelligence_classes(data)
    config = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                       database=DatabaseConfig(url=lease_dsn("source-ci.db")), data_dir=data)
    orch = Orchestrator(config)
    orch.db = case["db"]
    handler = CommandHandler(orch, config)
    # Isolate only normal filing; the source command, locks, durable history,
    # authority checks and replay run against PostgreSQL.
    async def ensure(args):
        existing = await case["db"].find_task_by_dedup_key("p", args["dedup_key"])
        if existing:
            return {"success": True, "task_id": existing.id, "created": False}
        task_id = f"source-repair-{len(filing.call_args_list)}"
        await case["db"].create_task(Task(id=task_id, project_id="p", repo_id="repo",
            title=args["title"], description=args["description"], dedup_key=args["dedup_key"],
            task_type=TaskType.BUGFIX, status=TaskStatus.READY))
        return {"success": True, "task_id": task_id, "created": True}
    filing = AsyncMock(side_effect=ensure)
    handler._cmd_ensure_task = filing
    observation = SourceCIObservation("e1", source, 0, state, checks)
    first, replay = await asyncio.gather(*(handler._cmd_observe_integration_source_ci(observation) for _ in range(2)))
    assert first["repair_task_id"] == replay["repair_task_id"]
    assert filing.await_count == 1
    args = filing.call_args.args[0]
    assert args["repo_id"] == "repo" and args["task_type"] == "bugfix" and args["root"] is True
    assert source["head"] in args["description"] and "tests/test_source.py::test_delivery" in args["description"]
    await case["db"].transition_task(first["repair_task_id"], TaskStatus.FAILED, force=True)
    successor = await handler._cmd_observe_integration_source_ci(observation)
    assert successor["repair_task_id"] != first["repair_task_id"]
    async with case["db"]._engine.connect() as conn:
        record = (await conn.execute(select(integration_source_ci))).mappings().one()
    assert record["repair_attempt"] == 2 and record["repair_history"][0]["task_id"] == first["repair_task_id"]
    # A new verified generation is a new source identity even at the same
    # Git head; its assignment must not reuse a predecessor's dedup key.
    async with case["db"].immediate() as conn:
        # Verification evidence is append-only. Create the next completed
        # episode instead of rewriting the first generation's receipts.
        for table, values in (
            (integration_parent_episodes, {
                "id": "episode-e1-next", "generation": 2,
            }),
            (integration_repair_operations, {
                "id": "operation-e1-next", "episode_id": "episode-e1-next",
            }),
            (integration_parent_verifications, {
                "id": "verification-e1-next", "operation_id": "operation-e1-next",
                "episode_id": "episode-e1-next", "generation": 2,
            }),
            (integration_parent_operation_completions, {
                "operation_id": "operation-e1-next", "verification_id": "verification-e1-next",
                "episode_id": "episode-e1-next",
            }),
        ):
            previous = (await conn.execute(select(table))).mappings().one()
            await conn.execute(insert(table).values({**previous, **values}))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "e1"
        ).values(
            generation=2, verified_generation=2, episode_id="episode-e1-next",
            current_verification_id="verification-e1-next",
            last_completed_operation_id="operation-e1-next",
            last_completed_verification_id="verification-e1-next",
        ))
    async with case["db"]._engine.connect() as conn:
        next_source = await case["producer"]._pull_request_source_on(conn, "e1")
    assert next_source["generation"] == 2 and next_source["head"] == source["head"]
    changed = await handler._cmd_observe_integration_source_ci(
        SourceCIObservation("e1", next_source, 0, state, checks))
    assert changed["repair_task_id"] not in {first["repair_task_id"], successor["repair_task_id"]}


def test_cancelled_old_check_cannot_supersede_newer_run():
    from src.integration.models import RequiredCheckSet
    from src.integration.source_ci import classify_source_checks
    required = RequiredCheckSet(version="v1", names=("Tests",), producer_id="7")
    def run(number, status, conclusion, **extra):
        return {"id": number, "name": "Tests", "head_sha": "a" * 40,
                "app": {"id": 7}, "status": status, "conclusion": conclusion, **extra}
    for status, conclusion, expected in [("in_progress", None, "pending"), ("completed", "success", "green")]:
        assert classify_source_checks([run(1, "completed", "cancelled"), run(2, status, conclusion)],
                                      head="a" * 40, required=required)[0] == expected
    assert classify_source_checks([run(3, "completed", "success", app={"id": 9})],
                                  head="a" * 40, required=required)[0] == "pending"
    assert classify_source_checks([run(4, "in_progress", "success")],
                                  head="a" * 40, required=required)[0] == "pending"


async def test_green_repair_readmits_exact_failed_source_with_cleanup_coverage(case):
    await _continuous_policy(case)
    db = case["db"]
    branch = "aq/source-repair"
    _git("push", "origin", f"{case['second']}:refs/heads/{branch}", cwd=case["work"])
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="repair-source", project_id="p", repo_id="repo", title="CI repair", description="",
            status="COMPLETED", task_type="bugfix", branch_name=branch,
            pr_url="https://github.com/o/r/pull/8", created_at=1.0, updated_at=1.0))
        await conn.execute(insert(task_branch_origins).values(
            id="origin-repair-source", task_id="repair-source", repository_id="repo",
            base_sha=case["base"], creation_generation=0, reserved=True, created_at=1.0))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="repair-source", repository_id="repo", branch=branch,
            checkpoint_sha=case["second"], generation=0, updated_at=1.0))
        await conn.execute(insert(integration_source_ci).values(
            task_id="e1", repository_id="repo", source_base=case["base"], source_head=case["first"],
            generation=1, policy_generation=0, state="red", evidence={"failed_check": "unit"},
            repair_task_id="repair-source", repair_attempt=1, observed_at=1000.0))
        await conn.execute(insert(integration_source_ci).values(
            task_id="repair-source", repository_id="repo", source_base=case["base"], source_head=case["second"],
            generation=0, policy_generation=0, state="pending", evidence={}, observed_at=1000.0))
    for task_id, head in (("e1", case["first"]), ("repair-source", case["second"])):
        assert await case["producer"].snapshot_authorized(task_id, reviewed_sha=head, policy_generation=0)
    async def members():
        async with db.immediate() as conn:
            return await TrainService(db)._eligible_members(
                conn, project_id="p", repository_id="repo", project_mode="pull_request")
    assert await members() == []
    async with db.immediate() as conn:
        await conn.execute(update(integration_source_ci).where(
            integration_source_ci.c.task_id == "repair-source").values(state="green"))
    assert {item["task_id"] for item in await members()} == {"e1", "repair-source"}
    # A second repair retains the complete ancestry and cleanup coverage.
    _git("checkout", "--detach", case["second"], cwd=case["work"])
    (case["work"] / "final-fix.txt").write_text("second CI repair\n")
    _git("add", "final-fix.txt", cwd=case["work"])
    _git("commit", "-m", "second CI repair", cwd=case["work"])
    third = _git("rev-parse", "HEAD", cwd=case["work"])
    final_branch = "aq/final-source-repair"
    _git("push", "origin", f"HEAD:refs/heads/{final_branch}", cwd=case["work"])
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="final-repair", project_id="p", repo_id="repo", title="Second CI repair",
            description="", status="COMPLETED", task_type="bugfix", branch_name=final_branch,
            pr_url="https://github.com/o/r/pull/9", created_at=1.0, updated_at=1.0))
        await conn.execute(insert(task_branch_origins).values(
            id="origin-final-repair", task_id="final-repair", repository_id="repo",
            base_sha=case["base"], creation_generation=0, reserved=True, created_at=1.0))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="final-repair", repository_id="repo", branch=final_branch,
            checkpoint_sha=third, generation=0, updated_at=1.0))
        await conn.execute(update(integration_source_ci).where(
            integration_source_ci.c.task_id == "repair-source"
        ).values(state="red", repair_task_id="final-repair", repair_attempt=1))
        await conn.execute(insert(integration_source_ci).values(
            task_id="final-repair", repository_id="repo", source_base=case["base"],
            source_head=third, generation=0, policy_generation=0, state="green",
            evidence={}, observed_at=1001.0))
    assert await case["producer"].snapshot_authorized(
        "final-repair", reviewed_sha=third, policy_generation=0)
    assert {item["task_id"] for item in await members()} == {"e1", "repair-source", "final-repair"}
    # Revocation invalidates authorization and CI tied to the older policy generation.
    async with db.immediate() as conn:
        assert await db.cas_project_integration_control_on(
            conn, project_id="p", expected_generation=0, effective_mode="train",
            desired_mode="train", draining=False)
    assert await members() == []


async def test_source_repair_admission_refuses_missing_source_ancestry(case):
    await _continuous_policy(case)
    async with case["db"].immediate() as conn:
        await conn.execute(insert(integration_source_ci).values(
            task_id="source", repository_id="repo", source_base=case["base"],
            source_head=case["second"], generation=0, policy_generation=0, state="red",
            evidence={}, repair_task_id="e1", observed_at=1000.0))
    with pytest.raises(HierarchyError, match="source ancestry"):
        await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0)


def _git(*args: str, cwd=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def case(tmp_path):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    _git("clone", str(remote), str(work))
    _git("config", "user.name", "GitHub Review Test", cwd=work)
    _git("config", "user.email", "review@example.test", cwd=work)
    (work / "source.txt").write_text("base\n")
    _git("add", "source.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", "main", cwd=work)
    _git("switch", "-c", "aq/epic/retire-the-publisher", cwd=work)
    (work / "source.txt").write_text("first\n")
    _git("commit", "-am", "first epic head", cwd=work)
    first = _git("rev-parse", "HEAD", cwd=work)
    tree = _git("rev-parse", "HEAD^{tree}", cwd=work)
    _git("push", "origin", "HEAD", cwd=work)
    (work / "source.txt").write_text("second\n")
    _git("commit", "-am", "second epic head", cwd=work)
    second = _git("rev-parse", "HEAD", cwd=work)

    db = Database(lease_dsn("epic-pr-review-evidence.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="PR review project"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        integration_mode="pull_request",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="p", interval_seconds=300, next_due_at=1300.0, updated_at=1000.0
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="e1",
                project_id="p",
                repo_id="repo",
                title="Epic",
                description="",
                status="COMPLETED",
                branch_name="aq/epic/retire-the-publisher",
                pr_url="https://github.com/o/r/pull/7",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="c1",
                project_id="p",
                repo_id="repo",
                title="Child",
                description="",
                status="COMPLETED",
                parent_task_id="e1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-e1",
                task_id="e1",
                repository_id="repo",
                base_sha=base,
                creation_generation=1,
                reserved=True,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-e1",
                parent_task_id="e1",
                repository_id="repo",
                generation=1,
                pre_collection_checkpoint_sha=base,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-e1",
                target_kind="parent",
                parent_task_id="e1",
                episode_id="episode-e1",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1},
                artifact_snapshot={"version": 1},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-e1",
                operation_id="operation-e1",
                parent_task_id="e1",
                episode_id="episode-e1",
                generation=1,
                head_sha=first,
                required_check_version="checks-v1",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-e1",
                verification_id="verification-e1",
                parent_task_id="e1",
                episode_id="episode-e1",
                completed_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="e1",
                repository_id="repo",
                branch="aq/epic/retire-the-publisher",
                checkpoint_sha=first,
                generation=1,
                verified_sha=first,
                verified_generation=1,
                episode_id="episode-e1",
                current_verification_id="verification-e1",
                last_completed_operation_id="operation-e1",
                last_completed_verification_id="verification-e1",
                updated_at=1.0,
            )
        )
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    yield {
        "db": db,
        "producer": ReviewEvidenceProducer(db, promotion, clock=lambda: 1000.0),
        "work": work,
        "first": first,
        "second": second,
        "tree": tree,
        "base": base,
    }
    await db.close()


async def _rows(db):
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_review_evidence))).mappings().all()
    return [dict(row) for row in rows]


class _ReviewClient:
    def __init__(self, head: str, reviews: list[dict], *, moved: bool = False):
        self.head = head
        self.reviews = reviews
        self.moved = moved

    async def pull_request(self, _url):
        return {
            "state": "open",
            "head": {
                "sha": "0" * 40 if self.moved else self.head,
                "ref": "aq/epic/retire-the-publisher",
                "repo": {"id": 7},
            },
            "base": {"ref": "main", "repo": {"id": 7}},
        }

    async def paged_list(self, _path):
        return self.reviews


class _ReviewGit:
    def __init__(self, client):
        self.client = client

    async def bind_github_repository(self, _url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    def _github_client(self, _binding):
        return self.client


async def test_live_review_poller_arms_window_only_for_exact_human_approval(case):
    reviews = [
        {
            "id": 1,
            "state": "APPROVED",
            "user": {"type": "Bot", "login": "fixture[bot]"},
            "commit_id": case["first"],
        },
        {
            "id": 2,
            "state": "APPROVED",
            "user": {"type": "User", "login": "reviewer"},
            "commit_id": case["first"],
            "body": "Approved on GitHub",
        },
    ]
    poller = GitHubReviewPoller(
        case["db"], case["producer"], _ReviewGit(_ReviewClient(case["first"], reviews))
    )
    await poller.tick(1000.0)
    rows = await _rows(case["db"])
    assert len(rows) == 1
    assert rows[0]["reviewer_identity"] == "github:reviewer"
    assert rows[0]["evidence"]["github_review_id"] == 2
    async with case["db"]._engine.connect() as conn:
        schedule = (
            await conn.execute(select(project_integration_schedules))
        ).mappings().one()
    assert schedule["settling_first_approval_at"] == 1000.0
    await poller.tick(1031.0)
    assert len(await _rows(case["db"])) == 1


@pytest.mark.parametrize("moved", [True, False])
async def test_live_review_poller_refuses_moved_pr_or_stale_review(case, moved):
    client = _ReviewClient(
        case["first"],
        [{
            "id": 3,
            "state": "APPROVED",
            "user": {"type": "User", "login": "reviewer"},
            "commit_id": case["first"] if moved else case["second"],
        }],
        moved=moved,
    )
    await GitHubReviewPoller(case["db"], case["producer"], _ReviewGit(client)).tick(1000.0)
    assert await _rows(case["db"]) == []


async def test_legacy_leaf_root_materializes_exact_pr_source(case, tmp_path, monkeypatch):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
    git = GitManager()
    findings = await _unmaterialized_pr_findings(
        SimpleNamespace(db=case["db"]), {"p"}
    )
    assert [item["task_id"] for item in findings] == ["legacy"]

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    monkeypatch.setattr(git, "bind_github_repository", binding)
    monkeypatch.setattr(git, "_github_client", lambda _binding: _ReviewClient(case["first"], []))
    promotion = PromotionService(case["db"], data_dir=tmp_path / "legacy-data", git_manager=git)
    repair = RootMaterialization(case["db"], promotion)
    dry = await repair.run("legacy")
    assert dry["outcome"] == "would_materialize"
    assert (dry["base_sha"], dry["head_sha"]) == (case["base"], case["first"])
    assert (await repair.run("legacy", dry_run=False, expected_head_sha=case["second"],
                             reason="legacy PR", operator_id="operator"))["outcome"] == "changed"
    applied = await repair.run("legacy", dry_run=False, expected_head_sha=case["first"],
                               reason="legacy PR", operator_id="operator")
    assert applied["outcome"] == "materialized"
    assert (await repair.run("legacy"))["outcome"] == "not_eligible"
    async with case["db"]._engine.connect() as conn:
        checkpoint = (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "legacy"
        ))).mappings().one()
        origin = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == "legacy"
        ))).mappings().one()
        source = await case["producer"]._pull_request_source_on(conn, "legacy")
        eligible = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=20,
        )
    assert checkpoint["checkpoint_sha"] == case["first"]
    assert origin["base_sha"] == case["base"]
    assert origin["materialized"] is True
    assert source["head"] == case["first"]
    assert any(row["task_id"] == "legacy" for row in eligible)
    assert await _unmaterialized_pr_findings(SimpleNamespace(db=case["db"]), {"p"}) == []
    evidence = await case["producer"].snapshot_from_pull_request(
        "legacy", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    async with case["db"].immediate() as conn:
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["legacy"]
    assert members[0]["review"] == evidence


async def test_materialization_refuses_pr_head_that_differs_from_git(case, tmp_path, monkeypatch):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
    git = GitManager()

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    monkeypatch.setattr(git, "bind_github_repository", binding)
    monkeypatch.setattr(git, "_github_client", lambda _binding: _ReviewClient(case["first"], [], moved=True))
    repair = RootMaterialization(
        case["db"], PromotionService(case["db"], data_dir=tmp_path, git_manager=git)
    )
    result = await repair.run("legacy")
    assert result["outcome"] == "changed"
    async with case["db"]._engine.connect() as conn:
        assert (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "legacy"
        ))).first() is None


async def test_legacy_parent_root_cannot_forge_leaf_checkpoint(case, tmp_path):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy-parent", project_id="p", repo_id="repo", title="Legacy parent",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(tasks).values(
            id="legacy-child", project_id="p", repo_id="repo", title="Legacy child",
            description="", status="COMPLETED", parent_task_id="legacy-parent",
            created_at=1.0, updated_at=1.0,
        ))
    repair = RootMaterialization(
        case["db"], PromotionService(case["db"], data_dir=tmp_path, git_manager=GitManager())
    )
    result = await repair.run("legacy-parent")
    assert result["outcome"] == "not_eligible"
    assert "verification evidence" in result["reason"]


async def test_poller_warns_when_completed_pr_has_no_source(case, caplog):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
    poller = GitHubReviewPoller(
        case["db"], case["producer"], _ReviewGit(_ReviewClient(case["first"], []))
    )
    await poller.tick(1000.0)
    assert "Completed train root legacy has PR" in caplog.text


async def test_new_github_review_id_can_supersede_an_earlier_rejection(case):
    for review_id, verdict in ((4, "rejected"), (5, "approved")):
        await case["producer"].snapshot_from_pull_request(
            "e1",
            verdict=verdict,
            reviewer_login="reviewer",
            reviewed_sha=case["first"],
            github_review_id=review_id,
        )
    rows = await _rows(case["db"])
    assert len(rows) == 2
    assert max(rows, key=lambda row: row["created_at"])["verdict"] == "approved"


async def test_approval_writes_exact_trusted_evidence_and_is_eligible(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1",
        verdict="approved",
        reviewer_login="jkern",
        reviewed_sha=case["first"],
        summary="Looks good",
    )
    assert evidence == (await _rows(case["db"]))[0]
    assert {
        key: evidence[key]
        for key in (
            "source_task_id",
            "repository_id",
            "source_base",
            "reviewed_head_sha",
            "reviewed_tree_sha",
            "reviewer_task_id",
            "reviewer_session_attempt_id",
            "reviewer_identity",
            "review_kind",
            "generation",
            "verdict",
        )
    } == {
        "source_task_id": "e1",
        "repository_id": "repo",
        "source_base": case["base"],
        "reviewed_head_sha": case["first"],
        "reviewed_tree_sha": case["tree"],
        "reviewer_task_id": None,
        "reviewer_session_attempt_id": None,
        "reviewer_identity": "github:jkern",
        "review_kind": "parent",
        "generation": 1,
        "verdict": "approved",
    }
    assert evidence["evidence"] == {
        "decision_path": "github_pull_request",
        "summary": "Looks good",
        "feedback": "",
        "reviewed_sha": case["first"],
        "pr_url": "https://github.com/o/r/pull/7",
        "verification_id": "verification-e1",
    }
    async with case["db"].immediate() as conn:
        assert await settled(conn, project_id="p", now=1299.0) is False
        assert await settled(conn, project_id="p", now=1300.0) is True
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["e1"]
    assert members[0]["review"] == evidence


async def _childless_root(case, *, checkpoint_sha=None):
    """A childless train root on its own published branch (noble-harbor-74's shape)."""
    head = checkpoint_sha or case["first"]
    branch = "aq/epic/fix-one-thing"
    _git("push", "origin", f"{case['first']}:refs/heads/{branch}", cwd=case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="r1", project_id="p", repo_id="repo", title="Fix one thing",
                description="", status="COMPLETED", branch_name=branch,
                pr_url="https://github.com/o/r/pull/9", created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-r1", task_id="r1", repository_id="repo", branch_name=branch,
                parent_ref="main", base_sha=case["base"], creation_generation=0,
                reserved=True, materialized=True, created_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="r1", repository_id="repo", branch=branch, checkpoint_sha=head,
                generation=0, state="working", version=1, updated_at=1.0,
            )
        )


async def test_approval_of_a_childless_root_is_leaf_evidence_the_train_seats(case):
    """A leaf root's PR approval must reach the train, not stop at ingestion."""
    await _childless_root(case)

    evidence = await case["producer"].snapshot_from_pull_request(
        "r1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )

    assert evidence["review_kind"] == "leaf"
    assert evidence["reviewed_head_sha"] == case["first"]
    assert evidence["source_base"] == case["base"]
    assert evidence["generation"] == 0
    assert evidence["evidence"]["verification_id"] is None
    async with case["db"].immediate() as conn:
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["r1"]
    assert members[0]["source_kind"] == "leaf"
    assert members[0]["review"] == evidence


async def test_childless_root_still_at_its_base_takes_no_verdict(case):
    await _childless_root(case, checkpoint_sha=case["base"])

    assert await case["producer"].snapshot_from_pull_request(
        "r1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["base"]
    ) is None
    assert await _rows(case["db"]) == []


async def test_approval_of_moved_head_is_not_reused(case):
    first = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    _git("push", "origin", "HEAD", cwd=case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-e1-next",
                parent_task_id="e1",
                repository_id="repo",
                generation=2,
                pre_collection_checkpoint_sha=case["first"],
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-e1-next",
                target_kind="parent",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1},
                artifact_snapshot={"version": 1},
                required_check_version="checks-v1",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-e1-next",
                operation_id="operation-e1-next",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                generation=2,
                head_sha=case["second"],
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-e1-next",
                verification_id="verification-e1-next",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                completed_at=2.0,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "e1")
            .values(
                generation=2,
                checkpoint_sha=case["second"],
                verified_sha=case["second"],
                verified_generation=2,
                episode_id="episode-e1-next",
                current_verification_id="verification-e1-next",
                last_completed_operation_id="operation-e1-next",
                last_completed_verification_id="verification-e1-next",
            )
        )
    async with case["db"].immediate() as conn:
        candidates = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10
        )
        assert await case["db"].latest_exact_reviews_on(conn, candidates) == {}
    second = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["second"]
    )
    assert len(await _rows(case["db"])) == 2
    assert first["reviewed_head_sha"] == case["first"]
    assert second["reviewed_head_sha"] == case["second"]


async def test_rejection_records_feedback_without_arming_window(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1",
        verdict="rejected",
        reviewer_login="jkern",
        reviewed_sha=case["first"],
        feedback="Needs a test for the cap.",
    )
    assert evidence["verdict"] == "rejected"
    assert evidence["evidence"]["feedback"] == "Needs a test for the cap."
    async with case["db"].immediate() as conn:
        assert await settled(conn, project_id="p", now=1e12) is True
        schedule = (await conn.execute(select(project_integration_schedules))).mappings().one()
        assert schedule["settling_first_approval_at"] is None
        assert schedule["settling_fires_at"] is None


async def test_epic_without_pull_request_is_refused(case):
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(pr_url=None))
    result = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert result is None
    assert await _rows(case["db"]) == []


async def test_unverified_head_is_refused(case):
    with pytest.raises(HierarchyError, match="reviewed head"):
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["second"]
        )
    assert await _rows(case["db"]) == []


async def test_approval_is_refused_when_remote_head_moved(case):
    _git("push", "origin", "HEAD", cwd=case["work"])
    with pytest.raises(HierarchyError, match="reviewed remote ref"):
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
        )
    assert await _rows(case["db"]) == []


async def test_later_rejection_wins_when_clock_has_not_advanced(case):
    approved = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    rejected = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="rejected", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert rejected["created_at"] > approved["created_at"]
    async with case["db"].immediate() as conn:
        candidates = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10
        )
        latest = await case["db"].latest_exact_reviews_on(conn, candidates)
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert list(latest.values()) == [rejected]
    assert members == []


async def test_duplicate_approval_is_idempotent(case):
    first = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    second = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert first == second
    assert len(await _rows(case["db"])) == 1
