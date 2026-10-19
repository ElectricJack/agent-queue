"""Completed-container recovery with disposable PostgreSQL and real remote Git."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.contracts.integration import IntegrationRecoverCompletedContainerArgs as Request
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    TRUSTED_LOCAL,
    principal_context,
)
from src.profiles.capabilities import CapabilityPolicy
from src.database.tables import (
    events,
    integration_branch_owners,
    integration_parent_episodes,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.completed_container_recovery import CompletedContainerRecovery, RECOVERY_EVENT
from src.integration.delivery_truth import load_delivery_requests
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.integration.repository_git import RepositoryGit
from src.integration.train import TrainTarget
from src.models import SessionRecord
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import (  # noqa: F401 - pytest fixture
    MAIN,
    completed,
    hosted_train,
    review_epic,
    world,
)

EPIC = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
REQUIRED = {"version": "v1", "names": ["unit"], "producer_id": "15368"}


@pytest.fixture
async def case(world, tmp_path):  # noqa: F811
    db, origin = world.db, world.origin
    base = git(origin.url, "rev-parse", "main")
    head = await completed(world, "epic", pr=False)
    # Child closes at a source actually contained in the accepted epic head.
    await completed(world, "child", parent="epic", head=head, source_base=base)
    async with db.immediate() as conn:
        # The generic train fixture permits absent historical branch labels;
        # this recovery intentionally requires the exact accepted close branch.
        for tid in ("epic", "child"):
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == tid,
                )
                .values(branch=f"aq/{tid}")
            )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="epic",
                repository_id="r",
                branch="aq/epic",
                checkpoint_sha=base,
                generation=3,
                version=4,
                state="working",
                updated_at=1,
            )
        )
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="r", branch="aq/epic"), "epic", "worker"
    )
    promotion = RepositoryGit(db, data_dir=tmp_path, git_manager=GitManager())
    train, github, _ = await hosted_train(world)
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_policy={
                    "parent": {"required_checks": REQUIRED, "admission": "authorized"},
                    "root": {"required_checks": REQUIRED, "admission": "reviewed"},
                },
            )
        )
    return SimpleNamespace(
        world=world,
        db=db,
        origin=origin,
        base=base,
        head=head,
        promotion=promotion,
        service=CompletedContainerRecovery(db, promotion, git_first="active"),
        train=train,
        github=github,
        target=EPIC,
    )


def request(**changes):
    return Request(task_id="epic", project_id="p", repository_id="r", branch="aq/epic", **changes)


async def preview(case):
    result = await case.service.run(request(), operator_id="human:local-operator")
    assert result["outcome"] == "preview", result
    return result


def apply_request(preview, **changes):
    values = dict(
        dry_run=False,
        expected_head_sha=preview["head_sha"],
        expected_completion_id=preview["completion_id"],
        expected_generation=preview["generation"],
        expected_version=preview["version"],
        expected_origin_sha256=preview["origin_sha256"],
        expected_snapshot_sha256=preview["snapshot_sha256"],
        reason="repair accepted final source",
    )
    return request(**{**values, **changes})


async def rows(case):
    async with case.db._engine.connect() as conn:
        result = {}
        for table in (
            task_integration_checkpoints,
            tasks,
            task_branch_origins,
            task_completion_records,
            integration_branch_owners,
            events,
        ):
            result[table.name] = [
                dict(row) for row in (await conn.execute(select(table))).mappings()
            ]
        return result


async def test_preview_apply_audit_repeat_and_normal_admission(case):
    before = await rows(case)
    plan = await preview(case)
    assert await rows(case) == before
    result = await case.service.run(apply_request(plan), operator_id="human:local-operator")
    assert result["outcome"] == "recovered", result
    after = await rows(case)
    for table in (
        "tasks",
        "task_branch_origins",
        "task_completion_records",
        "integration_branch_owners",
    ):
        assert after[table] == before[table]
    checkpoint = after["task_integration_checkpoints"][0]
    assert checkpoint["checkpoint_sha"] == case.head and checkpoint["generation"] == 3
    assert checkpoint["version"] == 5 and checkpoint["episode_id"] is None
    audit = [row for row in after["events"] if row["event_type"] == RECOVERY_EVENT]
    assert len(audit) == 1
    assert json.loads(audit[0]["payload"]) == result["receipt"]
    assert result["receipt"]["operator_id"] == "human:local-operator"
    assert result["receipt"]["before"]["checkpoint_sha"] == case.base
    assert result["receipt"]["children"][0]["head_sha"] == case.head
    assert result["receipt"]["delivery"] is False
    from src.event_schemas import validate_event

    assert validate_event(RECOVERY_EVENT, result["receipt"], strict_extras=True) == []
    repeated = await case.service.run(apply_request(plan), operator_id="human:local-operator")
    assert repeated["outcome"] == "already_recovered" and repeated["receipt"] == result["receipt"]
    assert await rows(case) == after
    delivery = (
        await load_delivery_requests(
            case.db, ["epic"], repository_id="r", target_ref=MAIN.target_ref, reduced=True
        )
    )["epic"]
    assert delivery.reported_source == checkpoint["checkpoint_sha"] == case.head
    # Real normal admission: each exact-source gate binds independently after repair.
    waiting = await case.train.visit(EPIC)
    assert waiting.state == "blocked", waiting
    case.github.runs[case.head] = "success"
    assert (await case.train.visit(EPIC)).state == "blocked"
    del case.github.runs[case.head]
    await review_epic(case)
    assert (await case.train.visit(EPIC)).state == "blocked"
    assert (await case.train.visit(MAIN)).batch_id is None
    assert git(case.origin.url, "rev-parse", "main") == case.base
    case.github.runs[case.head] = "success"
    assert (await case.train.visit(EPIC)).state == "idle"
    testing = await case.train.visit(MAIN)
    assert testing.state == "testing", testing
    case.github.runs[testing.candidate_sha] = "success"
    assert (await case.train.visit(MAIN)).state == "delivered"
    git(case.origin.url, "merge-base", "--is-ancestor", case.head, "main")


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_generation", 2),
        ("expected_version", 3),
        ("expected_head_sha", "a" * 40),
        ("expected_completion_id", "wrong"),
        ("expected_origin_sha256", "a" * 64),
        ("expected_snapshot_sha256", "a" * 64),
    ],
)
async def test_stale_preview_refuses_without_writes(case, field, value):
    plan = await preview(case)
    before = await rows(case)
    result = await case.service.run(
        apply_request(plan, **{field: value}), operator_id="human:local-operator"
    )
    assert result["outcome"] == "refused", result
    assert await rows(case) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "task",
        "completion",
        "child",
        "child_completion",
        "owner",
        "checkpoint",
        "repository",
        "origin",
        "child_added",
    ],
)
async def test_race_across_git_verification_refuses_without_recovery(case, mutation, monkeypatch):
    plan = await preview(case)
    original = case.promotion._tree_oid
    after_race = None

    async def race(*args):
        nonlocal after_race
        tree = await original(*args)
        async with case.db.immediate() as conn:
            if mutation == "task":
                await conn.execute(
                    update(tasks).where(tasks.c.id == "epic").values(status="PAUSED")
                )
            elif mutation in {"completion", "child_completion"}:
                tid = "epic" if mutation == "completion" else "child"
                row = dict(
                    (
                        await conn.execute(
                            select(task_completion_records).where(
                                task_completion_records.c.task_id == tid,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                await conn.execute(
                    insert(task_completion_records).values(
                        **{**row, "id": "new-close", "completed_at": time.time() + 10},
                    )
                )
            elif mutation == "child":
                await conn.execute(
                    update(tasks).where(tasks.c.id == "child").values(status="READY")
                )
            elif mutation == "child_added":
                row = dict(
                    (await conn.execute(select(tasks).where(tasks.c.id == "child")))
                    .mappings()
                    .one()
                )
                await conn.execute(insert(tasks).values(**{**row, "id": "new-child"}))
            elif mutation == "owner":
                await conn.execute(update(integration_branch_owners).values(fence_token=99))
            elif mutation == "checkpoint":
                await conn.execute(update(task_integration_checkpoints).values(version=5))
            elif mutation == "repository":
                await conn.execute(update(repos).values(default_branch="changed"))
            elif mutation == "origin":
                await conn.execute(
                    update(task_branch_origins)
                    .where(
                        task_branch_origins.c.task_id == "epic",
                    )
                    .values(base_refresh={"changed": True})
                )
        after_race = await rows(case)
        return tree

    monkeypatch.setattr(case.promotion, "_tree_oid", race)
    result = await case.service.run(apply_request(plan), operator_id="human:local-operator")
    assert result["outcome"] == "refused", result
    assert after_race is not None and await rows(case) == after_race


@pytest.mark.parametrize(
    "mutation",
    [
        "unresolved",
        "missing_completion",
        "nonpass",
        "wrong_branch",
        "wrong_repository",
        "foreign_owner",
        "handoff",
        "missing_owner",
        "leased_owner",
        "live_claim",
        "uncontained_child",
        "unpublished",
        "remote_changed",
        "inactive",
        "active_state",
        "episode_history",
        "episode_checkpoint",
        "empty_source",
        "abandoned",
    ],
)
async def test_ineligible_or_unproven_refuses_without_writes(case, mutation):
    async with case.db.immediate() as conn:
        if mutation in {"episode_history", "episode_checkpoint"}:
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id="episode",
                    parent_task_id="epic",
                    repository_id="r",
                    generation=3,
                    pre_collection_checkpoint_sha=case.base,
                    created_at=1,
                )
            )
            if mutation == "episode_checkpoint":
                await conn.execute(
                    update(task_integration_checkpoints).values(episode_id="episode")
                )
        elif mutation == "empty_source":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == "epic",
                )
                .values(commits="[]")
            )
        elif mutation == "abandoned":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == "epic",
                )
                .values(work_outcome="abandoned")
            )
        elif mutation == "unresolved":
            await conn.execute(update(tasks).where(tasks.c.id == "child").values(status="READY"))
        elif mutation == "missing_completion":
            await conn.execute(
                delete(task_completion_records).where(task_completion_records.c.task_id == "child")
            )
        elif mutation == "nonpass":
            await conn.execute(
                update(task_completion_records)
                .where(task_completion_records.c.task_id == "epic")
                .values(outcome="fail")
            )
        elif mutation == "wrong_branch":
            await conn.execute(
                update(task_completion_records)
                .where(task_completion_records.c.task_id == "epic")
                .values(branch="foreign")
            )
        elif mutation == "wrong_repository":
            await conn.execute(update(task_integration_checkpoints).values(repository_id="foreign"))
        elif mutation == "foreign_owner":
            await conn.execute(update(integration_branch_owners).values(owner_id="foreign"))
        elif mutation == "handoff":
            await conn.execute(
                update(integration_branch_owners).values(handoff_state="handoff_pending")
            )
        elif mutation == "missing_owner":
            await conn.execute(delete(integration_branch_owners))
        elif mutation == "leased_owner":
            await conn.execute(
                update(integration_branch_owners).values(
                    holder="train:live", fence=2, expires_at=time.time() + 60
                )
            )
        elif mutation == "active_state":
            await conn.execute(update(task_integration_checkpoints).values(state="verifying"))
    if mutation == "live_claim":
        await case.db.create_session(
            SessionRecord(
                id="live",
                task_id="epic",
                project_id="p",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="live",
                lifecycle="pool",
                work_dir="/tmp",
                epoch="test",
                instance_token="test",
                started_at=1,
                state="running",
            )
        )
    elif mutation == "uncontained_child":
        other = case.origin.work("other")
        async with case.db.immediate() as conn:
            await conn.execute(
                update(task_completion_records)
                .where(task_completion_records.c.task_id == "child")
                .values(commits=json.dumps([other]))
            )
    elif mutation == "unpublished":
        git(case.origin.clone, "push", "-q", "origin", ":refs/heads/aq/epic")
    elif mutation == "remote_changed":
        git(case.origin.clone, "push", "-q", "--force", "origin", f"{case.base}:refs/heads/aq/epic")
    elif mutation == "inactive":
        case.service.git_first = "shadow"
    before = await rows(case)
    result = await case.service.run(request(), operator_id="human:local-operator")
    assert result["outcome"] == "refused", result
    assert await rows(case) == before


async def test_remote_changes_after_first_observation(case, monkeypatch):
    plan = await preview(case)
    original = case.promotion._tree_oid

    async def move(*args):
        tree = await original(*args)
        git(case.origin.clone, "push", "-q", "--force", "origin", f"{case.base}:refs/heads/aq/epic")
        return tree

    monkeypatch.setattr(case.promotion, "_tree_oid", move)
    before = await rows(case)
    assert (await case.service.run(apply_request(plan), operator_id="human:local-operator"))[
        "outcome"
    ] == "refused"
    assert await rows(case) == before


async def test_command_authority_and_apply_contract(case):
    handler = IntegrationCommandsMixin()
    handler.db = case.db
    handler.config = SimpleNamespace(integration=SimpleNamespace(git_first="active"))
    handler._integration_repository_git = lambda: case.promotion
    args = request().model_dump()
    for principal in (
        ExecutionPrincipal(
            kind=PrincipalKind.SERVICE, service_name="train", policy=CapabilityPolicy()
        ),
        ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            session_id="worker",
            project_id="p",
            policy=CapabilityPolicy(),
        ),
        ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            session_id="stopped",
            project_id="p",
            elevated=True,
            policy=CapabilityPolicy(),
        ),
    ):
        with principal_context(principal):
            result = await handler._cmd_integration_recover_completed_container(args)
        assert result["outcome"] == "unauthorized", result
    result = await handler._cmd_integration_recover_completed_container({**args, "dry_run": False})
    assert result["outcome"] == "invalid"
    with principal_context(TRUSTED_LOCAL):
        assert (await handler._cmd_integration_recover_completed_container(args))[
            "outcome"
        ] == "preview"


async def test_released_fence_is_eligible(case):
    async with case.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id="past-train",
                owner_role="collector",
                handoff_state="released",
            )
        )
    assert (await preview(case))["head_sha"] == case.head


async def test_authority_revoked_during_verification(case):
    plan = await preview(case)

    async def no_longer_live():
        return None, "supervisor stopped"

    case.service.authorize = no_longer_live
    before = await rows(case)
    result = await case.service.run(apply_request(plan), operator_id="supervisor session:live")
    assert result["outcome"] == "refused" and "authority changed" in result["reason"]
    assert await rows(case) == before


async def test_mode_changes_during_verification(case, monkeypatch):
    plan = await preview(case)
    original = case.promotion._tree_oid

    async def switch_mode(*args):
        result = await original(*args)
        case.service.git_first = "shadow"
        return result

    monkeypatch.setattr(case.promotion, "_tree_oid", switch_mode)
    before = await rows(case)
    assert (await case.service.run(apply_request(plan), operator_id="human:local-operator"))[
        "outcome"
    ] == "refused"
    assert await rows(case) == before


async def test_handler_registry_and_cli_preview_apply(case, monkeypatch):
    from click.testing import CliRunner

    from src.cli.app import cli
    from src.commands import CommandHandler
    from src.commands.contracts.registry import CONTRACTS
    from src.config import AppConfig

    handler = CommandHandler(
        SimpleNamespace(db=case.db, repository_git=case.promotion), AppConfig()
    )
    handler.config.integration.git_first = "active"
    with principal_context(TRUSTED_LOCAL):
        plan = await handler.execute(
            "integration_recover_completed_container", request().model_dump()
        )
        assert plan["success"] and plan["outcome"] == "preview", plan
        applied = await handler.execute(
            "integration_recover_completed_container", apply_request(plan).model_dump()
        )
        assert (
            applied["success"] and applied["receipt"]["completion_id"] == plan["completion_id"]
        ), applied
    registration = CONTRACTS.get("integration_recover_completed_container")
    assert registration is not None and registration.contract.execution.supports_preview
    captured = []
    monkeypatch.setattr(
        "src.cli.integration._execute", lambda _ctx, name, args: captured.append((name, args))
    )
    runner = CliRunner()
    preview_cli = runner.invoke(
        cli,
        [
            "integration",
            "recover-completed-container",
            "epic",
            "--project",
            "p",
            "--repository",
            "r",
            "--branch",
            "aq/epic",
        ],
    )
    assert preview_cli.exit_code == 0, preview_cli.output
    assert captured[-1] == (
        "integration_recover_completed_container",
        {
            "task_id": "epic",
            "project_id": "p",
            "repository_id": "r",
            "branch": "aq/epic",
            "dry_run": True,
        },
    )
    missing = runner.invoke(
        cli,
        [
            "integration",
            "recover-completed-container",
            "epic",
            "--project",
            "p",
            "--repository",
            "r",
            "--branch",
            "aq/epic",
            "--apply",
        ],
    )
    assert missing.exit_code != 0 and "requires" in missing.output
    apply_cli = runner.invoke(
        cli,
        [
            "integration",
            "recover-completed-container",
            "epic",
            "--project",
            "p",
            "--repository",
            "r",
            "--branch",
            "aq/epic",
            "--apply",
            "--head",
            plan["head_sha"],
            "--completion",
            plan["completion_id"],
            "--generation",
            str(plan["generation"]),
            "--version",
            str(plan["version"]),
            "--origin-sha256",
            plan["origin_sha256"],
            "--snapshot-sha256",
            plan["snapshot_sha256"],
            "--reason",
            "recovery",
        ],
    )
    assert apply_cli.exit_code == 0, apply_cli.output
    assert captured[-1][1]["dry_run"] is False
    assert captured[-1][1]["expected_version"] == plan["version"]


async def test_audit_failure_rolls_back_checkpoint(case, monkeypatch):
    import src.integration.completed_container_recovery as recovery

    plan = await preview(case)
    before = await rows(case)
    original = recovery.json.dumps

    def fail_audit(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("delivery") is False:
            raise ValueError("audit serialization unavailable")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(recovery.json, "dumps", fail_audit)
    result = await case.service.run(apply_request(plan), operator_id="human:local-operator")
    assert result["outcome"] == "refused" and "audit serialization" in result["reason"]
    assert await rows(case) == before


async def test_canonical_origin_and_retained_origin_refuse(case, tmp_path):
    # The database repository remains the authority, including the actual origin
    # of its retained store. A local cache pointed elsewhere cannot supply proof.
    plan = await preview(case)
    resolved = await case.promotion._resolve_repository("r")
    other = tmp_path / "other.git"
    git(tmp_path, "init", "--bare", "-q", str(other))
    git(resolved.retained_git_dir, "remote", "set-url", "origin", str(other))
    before = await rows(case)
    refused = await case.service.run(apply_request(plan), operator_id="human:local-operator")
    assert refused["outcome"] == "refused" and "identity changed" in refused["reason"]
    assert await rows(case) == before


@pytest.mark.parametrize("identity", ["project", "repository", "branch"])
async def test_request_identity_refuses(case, identity):
    args = request().model_dump()
    args[{"project": "project_id", "repository": "repository_id", "branch": "branch"}[identity]] = (
        "foreign"
    )
    before = await rows(case)
    refused = await case.service.run(Request(**args), operator_id="human:local-operator")
    assert refused["outcome"] == "refused", refused
    assert await rows(case) == before


async def test_live_supervisor_authority_and_revocation(case, monkeypatch):
    from src.commands import CommandHandler
    from src.config import AppConfig
    from src.models import AgentProfile

    await case.db.create_profile(AgentProfile(id="supervisor", name="Supervisor"))
    await case.db.create_session(
        SessionRecord(
            id="live-supervisor",
            project_id="p",
            profile_id="supervisor",
            harness="codex",
            provider="openai",
            name="live-supervisor",
            lifecycle="named",
            work_dir="/tmp",
            epoch="test",
            instance_token="test",
            started_at=1,
            state="running",
        )
    )
    handler = CommandHandler(
        SimpleNamespace(db=case.db, repository_git=case.promotion), AppConfig()
    )
    handler.config.integration.git_first = "active"
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        session_id="live-supervisor",
        project_id="p",
        profile_id="supervisor",
        elevated=True,
        policy=CapabilityPolicy(aq_commands=frozenset({"integration_recover_completed_container"})),
    )
    with principal_context(principal):
        plan = await handler.execute(
            "integration_recover_completed_container", request().model_dump()
        )
        assert plan["success"] and plan["outcome"] == "preview", plan
        foreign = await handler.execute(
            "integration_recover_completed_container",
            {
                **request().model_dump(),
                "project_id": "foreign",
            },
        )
        assert not foreign["success"], foreign
        original = case.promotion._tree_oid

        async def stop_supervisor(*args):
            tree = await original(*args)
            await case.db.update_session("live-supervisor", state="stopped")
            return tree

        monkeypatch.setattr(case.promotion, "_tree_oid", stop_supervisor)
        before = await rows(case)
        result = await handler.execute(
            "integration_recover_completed_container", apply_request(plan).model_dump()
        )
        assert not result["success"] and "authority changed" in result["error"], result
        assert await rows(case) == before


@pytest.mark.parametrize("missing_branch", [False, True])
async def test_child_completion_branch_binding_required(case, missing_branch):
    async with case.db.immediate() as conn:
        if missing_branch:
            await conn.execute(update(tasks).where(tasks.c.id == "child").values(branch_name=None))
        await conn.execute(
            update(task_completion_records)
            .where(
                task_completion_records.c.task_id == "child",
            )
            .values(branch=None if missing_branch else "foreign")
        )
    before = await rows(case)
    result = await case.service.run(request(), operator_id="human:local-operator")
    assert result["outcome"] == "refused" and "branch identity" in result["reason"]
    assert await rows(case) == before
