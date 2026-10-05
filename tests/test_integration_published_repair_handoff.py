"""Public pool candidate submission uses exact qualified publication as detach proof."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_repair_stages,
    playbook_artifacts,
    project_integration_leases,
    sessions,
    tasks,
    workspaces,
)
from src.git.github_app import GitHubRepositoryBinding
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    TaskStatus,
    Workspace,
)
from src.profiles.capabilities import CapabilityPolicy
from tests.test_integration_candidates import (
    _AppClient,
    _artifact,
    _AuditForge,
    _git,
    _LocalPushGit,
    _make_conflicting_origin,
    _policy,
    _seed_batch,
)

pytestmark = pytest.mark.asyncio


class LocalForge(_AppClient, _AuditForge):
    def __init__(self, origin):
        _AppClient.__init__(self, origin)
        _AuditForge.__init__(self)
        self.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")


@pytest.fixture
async def repair(command_handler_factory, tmp_path, request, monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives

    authorize_root_primitives(monkeypatch)
    handler = await command_handler_factory()
    db, orchestrator = handler.db, handler.orchestrator
    await db.create_profile(AgentProfile(id="repairer", name="Repairer", lifecycle="pool"))
    await db.create_profile(AgentProfile(id="debugger", name="Debugger", lifecycle="pool"))
    await db.create_project(Project(id="p", name="project"))
    origin, base_checkout, base_sha, members = _make_conflicting_origin(tmp_path)
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(origin),
            default_branch="main",
        )
    )
    policy = _policy()
    policy["root"]["repair"]["primary_seconds"] = 600
    policy["root"]["repair"]["debug_seconds"] = 600
    aggregate = getattr(request, "param", "member") in {"batch_debug", "batch_continuous"}
    if aggregate:
        policy["root"]["repair"]["conflict_scope"] = "batch"
    if getattr(request, "param", "member") == "batch_continuous":
        policy["root"]["repair"]["on_exhausted"] = "continue"
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **_artifact().model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/published-repair-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    await _seed_batch(
        db, members=members, base_sha=base_sha, policy=policy, full_review_snapshot=True
    )
    async with db.immediate() as conn:
        await conn.execute(update(project_integration_leases).values(expires_at=time.time() + 3600))
    app = LocalForge(origin)
    orchestrator.git = _LocalPushGit(origin)
    orchestrator.github_repository_binding_resolver = lambda _repo: app.repository
    orchestrator.github_client_factory = lambda _binding: app
    # No CandidateService injection: build and submit through the same command factory.
    service = await handler._integration_candidate_service(await db.get_integration_batch("batch"))
    conflict = await service.build("batch")
    assert conflict.outcome == "conflict"
    task_id = "repair-repair-batch-batch-0"
    if aggregate:
        async with db._engine.connect() as conn:
            deadline = await conn.scalar(select(integration_repair_stages.c.deadline_at))
        # A never-claimed primary only extends its clock; one conclusive
        # attempt is what lets the deadline escalate to the debug stage.
        async with db.immediate() as conn:
            await conn.execute(update(integration_repair_stages).values(attempts=1))
        expired = await service.repair.expire("repair-batch-batch", 0, now=deadline)
        assert expired["action"] == "dispatch_debug"
        dispatched = await service.repair.dispatch("repair-batch-batch", 1)
        assert dispatched["outcome"] == "dispatched", dispatched
        task_id = dispatched["repair_task_id"]
    await db.create_agent(
        Agent(
            id="repair-agent",
            name="Repair Agent",
            profile_id="repairer",
            state=AgentState.BUSY,
            current_task_id=task_id,
        )
    )
    await db.transition_task(
        task_id, TaskStatus.IN_PROGRESS, force=True, assigned_agent_id="repair-agent"
    )
    task = await db.get_task(task_id)
    checkout = tmp_path / "slot"
    branch = conflict.branch.removeprefix("refs/heads/")
    _git(base_checkout, "fetch", "origin", conflict.branch)
    _git(base_checkout, "worktree", "add", "-b", branch, str(checkout), "FETCH_HEAD")
    await db.create_workspace(
        Workspace(
            id="repair-base",
            project_id="p",
            workspace_path=str(base_checkout),
            source_type=RepoSourceType.CLONE,
        )
    )
    await db.create_workspace(
        Workspace(
            id="repair-slot",
            project_id="p",
            workspace_path=str(checkout),
            source_type=RepoSourceType.WORKTREE,
            base_workspace_id="repair-base",
            slot_index=0,
            locked_by_task_id=task_id,
            locked_by_agent_id="repair-agent",
        )
    )
    await db.create_session(
        SessionRecord(
            id="repair-session",
            project_id="p",
            task_id=task_id,
            agent_id="repair-agent",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="repair-session",
            lifecycle="pool",
            state="running",
            epoch="test",
            instance_token="instance-1",
            work_dir=str(checkout),
            started_at=time.time(),
            claim_phase="active",
            claim_phase_at=time.time() - 1,
            last_claim_epoch=task.claim_epoch,
        )
    )
    orchestrator._worktree_base_paths[str(checkout)] = str(base_checkout)
    orchestrator.git.delegate.set_lock_provider(orchestrator._resolve_git_lock)
    ownership = BranchOwnership(db)
    fence = await ownership.acquire(
        BranchKey(repository_id="repo", branch=conflict.branch), task_id, "repair"
    )
    await ownership.attach(fence, "repair-session", "repair-slot", expected_role="repair")
    if aggregate:
        _git(
            checkout, "merge", "-s", "ours", "--no-ff", "-m", "cover frozen sources", members[1][1]
        )
    (checkout / "shared.txt").write_text("first and second\n")
    _git(checkout, "add", "shared.txt")
    _git(checkout, "commit", "-m", "resolve exact frozen conflict")
    head, tree = _git(checkout, "rev-parse", "HEAD"), _git(checkout, "rev-parse", "HEAD^{tree}")
    repair_commits = _git(
        checkout, "rev-list", "--first-parent", "--reverse", f"{conflict.head_sha}..{head}"
    ).split()
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_resolve_candidate_member"]
        ),
        session_id="repair-session",
        session_instance_token="instance-1",
        project_id="p",
        profile_id="repairer",
        task_id=None,
    )
    state = SimpleNamespace(
        handler=handler,
        db=db,
        orchestrator=orchestrator,
        app=app,
        origin=origin,
        checkout=checkout,
        base_checkout=base_checkout,
        conflict=conflict,
        task_id=task_id,
        head=head,
        tree=tree,
        principal=principal,
        fence=fence,
        ownership=ownership,
        args={
            "resolved_head_sha": head,
            "resolved_tree_sha": tree,
            "repair_commit_shas": repair_commits,
            "claim_epoch": task.claim_epoch,
        },
    )
    try:
        yield state
    finally:
        await db.close()


async def submit(repair, *, principal=None):
    with principal_context(principal or repair.principal):
        return await asyncio.wait_for(
            repair.handler.execute("integration_resolve_candidate_member", repair.args), timeout=30
        )


async def reservation(repair):
    async with repair.db._engine.connect() as conn:
        return dict(
            (await conn.execute(select(integration_candidate_resolutions))).mappings().one()
        )


async def retain_push(repair, monkeypatch):
    # Simulate the original deployed callback's refusal, then retry the very
    # same durable reservation through the fresh command factory.
    callback = repair.orchestrator.aconfirm_integration_pool_published_repair_handoff
    monkeypatch.setattr(
        repair.orchestrator,
        "aconfirm_integration_pool_published_repair_handoff",
        AsyncMock(return_value=False),
    )
    response = await submit(repair)
    assert response["outcome"] == "wait", response
    proof = await reservation(repair)
    assert proof["state"] == "pushed"
    assert (
        await repair.app.exact_head_ref(proof["target_branch"].removeprefix("refs/heads/"))
        == repair.head
    )
    monkeypatch.setattr(
        repair.orchestrator, "aconfirm_integration_pool_published_repair_handoff", callback
    )
    return proof




@pytest.mark.parametrize(
    "changed",
    [
        "private_ref",
        "missing_ref",
        "dirty",
        "different_head",
        "different_branch",
        "session_instance",
        "session_task",
        "fence",
        "claim",
        "workspace_lock",
        "stage_expired",
        "stage_subject",
        "manifest",
    ],
)
async def test_changed_publication_or_authority_retains_owner_and_claim(
    repair, monkeypatch, changed
):
    proof = await retain_push(repair, monkeypatch)
    if changed == "private_ref":
        _git(repair.origin, "update-ref", proof["target_branch"], repair.conflict.head_sha)
    elif changed == "missing_ref":
        _git(repair.origin, "update-ref", "-d", proof["target_branch"])
    elif changed == "dirty":
        (repair.checkout / "untracked.txt").write_text("retain work\n")
    elif changed == "different_head":
        _git(repair.checkout, "switch", "--detach", repair.conflict.head_sha)
    elif changed == "different_branch":
        _git(repair.checkout, "switch", "-c", "unrelated")
    else:
        async with repair.db.immediate() as conn:
            if changed == "session_instance":
                await conn.execute(update(sessions).values(instance_token="instance-2"))
            elif changed == "session_task":
                await conn.execute(update(sessions).values(task_id=None))
            elif changed == "fence":
                await conn.execute(
                    update(integration_branch_owners).values(fence_token=repair.fence.token + 1)
                )
            elif changed == "claim":
                await conn.execute(
                    update(tasks).where(tasks.c.id == repair.task_id).values(claim_epoch=99)
                )
            elif changed == "workspace_lock":
                await conn.execute(
                    update(workspaces)
                    .where(workspaces.c.id == "repair-slot")
                    .values(locked_by_task_id=None)
                )
            elif changed == "stage_expired":
                await conn.execute(
                    update(integration_repair_stages).values(deadline_at=time.time() - 1)
                )
            elif changed == "stage_subject":
                await conn.execute(
                    update(integration_repair_stages).values(
                        current_subject={
                            "kind": "batch",
                            "revision": proof["revision"],
                            "candidate_sha": "a" * 40,
                        }
                    )
                )
            elif changed == "manifest":
                stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
                dossier = dict(
                    stage["dossier"], manifest={"kind": "batch", "batch_id": "different"}
                )
                await conn.execute(update(integration_repair_stages).values(dossier=dossier))
    result = await submit(repair)
    assert result["success"] is False, result
    owner = await repair.ownership.get_owner(repair.fence.target)
    assert owner["handoff_state"] == "handoff_pending"
    assert owner["owner_id"] == repair.task_id
    assert owner["session_id"] == "repair-session"
    assert owner["workspace_id"] == "repair-slot"
    assert _git(repair.origin, "rev-parse", repair.conflict.branch) == repair.conflict.head_sha
    assert (await reservation(repair))["state"] == "pushed"


async def test_ordinary_pool_handoff_cannot_use_a_qualified_push(repair, monkeypatch):
    await retain_push(repair, monkeypatch)
    owner = await repair.ownership.get_owner(repair.fence.target)
    assert not await repair.orchestrator.aconfirm_integration_pool_owner_handoff(owner)
    assert (await repair.ownership.get_owner(repair.fence.target))[
        "handoff_state"
    ] == "handoff_pending"
    assert (await submit(repair))["outcome"] == "accepted"


@pytest.mark.parametrize("invalid", ["lineage", "tree"])
async def test_unverified_repair_objects_never_reach_detach(repair, invalid):
    if invalid == "lineage":
        repair.args["repair_commit_shas"] = [repair.conflict.head_sha]
    else:
        repair.args["resolved_tree_sha"] = _git(repair.origin, "rev-parse", "main^{tree}")
    result = await submit(repair)
    assert result["outcome"] == "stale", result
    assert (await reservation(repair))["state"] == "pushed"
    owner = await repair.ownership.get_owner(repair.fence.target)
    assert owner["handoff_state"] == "attached"
    assert _git(repair.checkout, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"


async def test_unpublished_reservation_never_authorizes_detach(repair, monkeypatch):
    factory = repair.handler._integration_candidate_service

    async def crash_factory(*args, **kwargs):
        service = await factory(*args, **kwargs)
        service.push_repair = AsyncMock(side_effect=RuntimeError("restart before push"))
        return service

    monkeypatch.setattr(repair.handler, "_integration_candidate_service", crash_factory)
    assert (await submit(repair))["outcome"] == "runtime_error"
    proof = await reservation(repair)
    assert proof["state"] == "reserved"
    owner = await repair.ownership.get_owner(repair.fence.target)
    assert not await repair.orchestrator.aconfirm_integration_pool_published_repair_handoff(
        owner, proof["id"], remote_head_reader=repair.app.exact_head_ref
    )
    assert (await repair.ownership.get_owner(repair.fence.target))["handoff_state"] == "attached"
    monkeypatch.setattr(repair.handler, "_integration_candidate_service", factory)
    assert (await submit(repair))["outcome"] == "accepted"


@pytest.mark.parametrize("race", ["private_ref", "session_instance", "dirty", "head"])
async def test_proof_rechecks_remote_checkout_and_authority_under_base_mutex(
    repair, monkeypatch, race
):
    proof = await retain_push(repair, monkeypatch)
    reader = repair.app.exact_head_ref
    qualified_reads = 0

    async def changing_reader(branch):
        nonlocal qualified_reads
        observed = await reader(branch)
        if branch == proof["target_branch"].removeprefix("refs/heads/"):
            qualified_reads += 1
            if qualified_reads == 1 and race == "private_ref":
                # The upstream read is correct; the fresh mutex-bound read
                # must observe this move, rather than trust cached evidence.
                _git(repair.origin, "update-ref", proof["target_branch"], repair.conflict.head_sha)
            if qualified_reads == 2:
                assert repair.orchestrator._git_mutex(str(repair.base_checkout)).locked()
                if race == "session_instance":
                    async with repair.db.immediate() as conn:
                        await conn.execute(update(sessions).values(instance_token="instance-2"))
                elif race == "dirty":
                    (repair.checkout / "new-work.txt").write_text("preserve concurrent work\n")
                elif race == "head":
                    _git(repair.checkout, "switch", "--detach", repair.conflict.head_sha)
        return observed

    monkeypatch.setattr(repair.app, "exact_head_ref", changing_reader)
    assert (await submit(repair))["outcome"] == "wait"
    assert qualified_reads == 2
    owner = await repair.ownership.get_owner(repair.fence.target)
    assert owner["handoff_state"] == "handoff_pending"
    assert owner["session_id"] == "repair-session"
    assert (await reservation(repair))["state"] == "pushed"
    assert (await repair.db.get_session("repair-session")).task_id == repair.task_id
    assert _git(repair.origin, "rev-parse", repair.conflict.branch) == repair.conflict.head_sha


@pytest.mark.parametrize("crash", ["after_detach", "after_release", "after_transfer", "after_push"])
async def test_restart_retries_same_reservation_and_never_republishes_handoff(
    repair, monkeypatch, crash
):
    proof = await retain_push(repair, monkeypatch)
    original_deadline = proof["stage_deadline_at"]
    if crash in {"after_detach", "after_release"}:
        from src.integration import published_repair_handoff as handoff

        original = handoff._snapshot_on
        calls = 0

        async def snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2 and crash == "after_detach":
                raise RuntimeError("crash after detach")
            return await original(*args, **kwargs)

        monkeypatch.setattr(handoff, "_snapshot_on", snapshot)
        callback = repair.orchestrator.aconfirm_integration_pool_published_repair_handoff

        async def confirm(*args, **kwargs):
            result = await callback(*args, **kwargs)
            if result and crash == "after_release":
                raise RuntimeError("crash after release")
            return result

        monkeypatch.setattr(
            repair.orchestrator, "aconfirm_integration_pool_published_repair_handoff", confirm
        )
        failed = await submit(repair)
        assert failed["outcome"] in {"wait", "runtime_error"}
        monkeypatch.setattr(handoff, "_snapshot_on", original)
        monkeypatch.setattr(
            repair.orchestrator, "aconfirm_integration_pool_published_repair_handoff", callback
        )
    else:
        factory = repair.handler._integration_candidate_service

        async def crash_factory(*args, **kwargs):
            service = await factory(*args, **kwargs)
            point = "after_handoff_transfer" if crash == "after_transfer" else "after_handoff_push"
            service.crash_hook = lambda value: (
                (_ for _ in ()).throw(RuntimeError("simulated restart")) if value == point else None
            )
            return service

        monkeypatch.setattr(repair.handler, "_integration_candidate_service", crash_factory)
        assert (await submit(repair))["outcome"] == "runtime_error"
        monkeypatch.setattr(repair.handler, "_integration_candidate_service", factory)
        if crash == "after_transfer":
            # A fresh process cannot steal the first process's live mutation
            # nonce. Retry stays fenced until the normal mutation lease expires.
            assert (await submit(repair))["outcome"] == "wait"
            async with repair.db._engine.connect() as conn:
                expires_at = await conn.scalar(
                    select(integration_candidate_ref_mutations.c.expires_at).where(
                        integration_candidate_ref_mutations.c.purpose == "repair_handoff"
                    )
                )

            async def later_factory(*args, **kwargs):
                service = await factory(*args, **kwargs)
                service.clock = lambda: expires_at + 1
                return service

            monkeypatch.setattr(repair.handler, "_integration_candidate_service", later_factory)
    assert _git(repair.checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert (await submit(repair))["outcome"] == "accepted"
    assert (await submit(repair))["outcome"] == "already_accepted"
    final = await reservation(repair)
    assert final["id"] == proof["id"]
    assert final["stage_deadline_at"] == original_deadline
    assert final["handoff_fence_token"] == repair.fence.token + 1
    pushes = repair.orchestrator.git.pushes
    assert (
        sum(
            p["tip_oid"] == repair.head
            and p["branch"] == repair.conflict.branch.removeprefix("refs/heads/")
            for p in pushes
        )
        == 1
    )
