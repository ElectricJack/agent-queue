"""Real Git recovery for parents whose children reached main by other routes."""

import json
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import tables as t
from src.doctor.integration_checks import run_check
from src.doctor.models import Severity
from src.integration.delivery_truth import DeliveryState
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.models import SessionRecord, Task, TaskCompletion, TaskStatus
from tests.test_development_integration import (
    _merge_on_main,
    complete_source,
    feature,
    git,
    setup as setup,
)

PARENT = "agile-harbor-62"
VERIFIER = "verify-stale-aggregate"


@pytest.fixture
async def incident(setup):
    return await build_incident(setup, PARENT)


async def build_incident(setup, parent):
    db, service, source, remote, repo = setup
    base = git(source, "rev-parse", "main")
    children = [f"{parent}.{index}" for index in range(1, 5)]
    heads = {child: await feature(setup, child) for child in children}
    main = await _merge_on_main(source, *children)
    # The obsolete aggregate is deliberately not an ancestor of main.
    git(source, "checkout", "-B", "aq/epic/old-aggregate", base)
    (source / "obsolete.txt").write_text("old aggregate\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "obsolete aggregate")
    stale = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "HEAD:aq/epic/old-aggregate")
    await db.create_task(
        Task(
            id=parent,
            project_id="p",
            repo_id=repo.id,
            title="Train refactor phase 3",
            description="",
            status=TaskStatus.PAUSED,
            branch_name="aq/epic/old-aggregate",
        )
    )
    await db.create_task(
        Task(
            id=VERIFIER,
            project_id="p",
            repo_id=repo.id,
            title="Verify obsolete aggregate",
            description="",
            status=TaskStatus.READY,
            branch_name="aq/epic/old-aggregate",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(t.projects)
            .where(t.projects.c.id == "p")
            .values(
                hierarchical_integration_mode="train",
                hierarchical_integration_desired_mode="train",
            )
        )
        await conn.execute(
            update(t.tasks)
            .where(t.tasks.c.id.in_(children))
            .values(
                parent_task_id=parent,
            )
        )
        await conn.execute(
            insert(t.integration_parent_episodes).values(
                id="episode",
                parent_task_id=parent,
                repository_id=repo.id,
                generation=1,
                pre_collection_checkpoint_sha=base,
                created_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.integration_repair_operations).values(
                id="collection",
                target_kind="parent",
                parent_task_id=parent,
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                verifier_task_id=VERIFIER,
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.task_integration_checkpoints).values(
                task_id=parent,
                repository_id=repo.id,
                branch="aq/epic/old-aggregate",
                generation=6,
                checkpoint_sha=stale,
                state="integration_ready",
                version=9,
                episode_id="episode",
                branch_owner_id=VERIFIER,
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.task_branch_origins).values(
                id="stale-origin",
                task_id=VERIFIER,
                repository_id=repo.id,
                branch_name="aq/epic/old-aggregate",
                base_sha=stale,
                creation_generation=0,
                reserved=True,
                materialized=False,
                created_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.integration_branch_owners).values(
                id="owner",
                repository_id=repo.id,
                ref="aq/epic/old-aggregate",
                owner_id=VERIFIER,
                owner_role="verifier",
                fence_token=4,
                handoff_state="reserved",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    return setup, children, heads, main


async def adopt(incident, **overrides):
    setup, children, _heads, main = incident
    _db, service, *_ = setup
    args = dict(
        project_id="p",
        task_ids=[children[0].rsplit(".", 1)[0]],
        target_ref="refs/heads/main",
        head_sha=main,
        reason="all four children arrived through other routes",
        operator_id="supervisor",
        settle_delivered_children=True,
    )
    return await service.adopt(**(args | overrides))


async def reserve_children(incident):
    setup, children, _heads, _main = incident
    db, _service, source, _remote, repo = setup
    async with db.immediate() as conn:
        for child in children:
            branch = "aq/" + child
            git(source, "push", "origin", f"{child}:{branch}")
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == child).values(branch_name=branch)
            )
            await conn.execute(
                insert(t.integration_branch_owners).values(
                    id="owner-" + child,
                    repository_id=repo.id,
                    ref=branch,
                    owner_id=child,
                    owner_role="worker",
                    fence_token=7,
                    handoff_state="reserved",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
            )


@pytest.mark.parametrize("parent", [PARENT, "agile-impact-14", "vivid-quest-44"])
async def test_settlement_retires_proven_writerless_child_reservations(setup, parent):
    incident = await build_incident(setup, parent)
    await reserve_children(incident)
    db, _service, _source, remote, _repo = setup
    refs = git(remote, "show-ref")
    preview = await adopt(incident, dry_run=True)
    owners = preview["ownership"]
    assert {row["owner_id"] for row in owners} == {VERIFIER, *incident[1]}
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(t.integration_branch_owners))).mappings().all()
    assert all(row["handoff_state"] == "reserved" for row in rows)
    assert git(remote, "show-ref") == refs
    result = await adopt(incident)
    assert result["outcome"] == "adopted"
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(t.integration_branch_owners))).mappings().all()
        audit = json.loads(await conn.scalar(select(t.events.c.payload).where(
            t.events.c.event_type == "integration.parent_adopted"
        )))
    assert all(row["handoff_state"] == "released" for row in rows)
    assert {row["owner_id"]: row["fence_token"] for row in rows} == {
        VERIFIER: 5, **dict.fromkeys(incident[1], 8)
    }
    assert audit["ownership"] == owners
    assert (await db.get_task(parent)).status == TaskStatus.COMPLETED
    for child in incident[1]:
        assert (await db.get_task(child)).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("blocker", [
    "attached", "session", "workspace", "confirmed_workspace", "wrong_branch",
    "wrong_role", "wrong_repository", "other_owner", "default_branch", "undelivered",
    "pending_write",
])
async def test_child_reservations_cannot_waive_writer_or_delivery_proof(incident, blocker):
    await reserve_children(incident)
    setup, children, heads, _main = incident
    db, _service, source, remote, _repo = setup
    child = children[0]
    changes = {
        "attached": {"handoff_state": "attached"},
        "session": {"session_id": "retained"},
        "workspace": {"workspace_id": "retained"},
        "confirmed_workspace": {"confirmed_workspace_id": "retained"},
        "wrong_branch": {"ref": "aq/unrelated"},
        "wrong_role": {"owner_role": "repair"},
        "wrong_repository": {"repository_id": "another-repo"},
        "other_owner": {"owner_id": "another-task"},
        "default_branch": {"ref": "main"},
    }
    if blocker in changes:
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == "owner-" + child
            ).values(**changes[blocker]))
    elif blocker == "undelivered":
        git(source, "checkout", child)
        git(source, "commit", "--allow-empty", "-m", "undelivered generation")
        await complete_source(setup, child, "undelivered", git(source, "rev-parse", "HEAD"))
    else:
        async with db.immediate() as conn:
            await conn.execute(insert(t.integration_promotion_intents).values(
                id="pending-child-write", domain_key="child-write", operation_key="child-write",
                receipt_id="pending-child-receipt", project_id="p", repository_id="r",
                target_branch="refs/heads/aq/" + child, source_head=heads[child],
                source_base=heads[child], expected_target=heads[child],
                fence_owner_id=child, fence_token=7, state="prepared",
                created_at=time.time(), updated_at=time.time(),
            ))
    refs = git(remote, "show-ref")
    for dry_run in (True, False):
        with pytest.raises(ValueError):
            await adopt(incident, dry_run=dry_run)
    assert git(remote, "show-ref") == refs
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + child
        ))).mappings().one()
    assert owner["fence_token"] == 7
    assert owner["handoff_state"] != "released"
    assert await db.get_task_completion(PARENT) is None


async def test_child_reservation_rechecks_the_fence_after_git_proof(incident, monkeypatch):
    from src.integration.delivered_parent_adoption import DeliveredParentAdoption

    await reserve_children(incident)
    db, *_ = incident[0]
    prove = DeliveredParentAdoption.prove

    async def moved_fence(self, facts, truth, **kwargs):
        result = await prove(self, facts, truth, **kwargs)
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == "owner-" + incident[1][0]
            ).values(fence_token=8))
        return result

    monkeypatch.setattr(DeliveredParentAdoption, "prove", moved_fence)
    with pytest.raises(ValueError, match="generation changed"):
        await adopt(incident)
    assert await db.get_task_completion(PARENT) is None
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY


async def test_incident_dry_run_and_apply_retire_stale_verifier_without_ci(incident):
    setup, children, heads, main = incident
    db, service, _source, remote, _repo = setup
    await db.set_task_meta(
        VERIFIER, "needs_attention", {"reason": "frontier_origin_not_materialized"}
    )
    with pytest.raises(ValueError, match="current verified parent completion"):
        await adopt(incident, settle_delivered_children=False, accept_equivalent=True)
    refs = git(remote, "show-ref")
    assert (await service.delivery_observer.observe([PARENT])).get(
        PARENT
    ).state == DeliveryState.UNKNOWN
    preview = await adopt(incident, dry_run=True)
    assert preview["outcome"] == "would_adopt_parent"
    assert preview["retire_delegates"] == [VERIFIER]
    assert {proof["task_id"]: proof["source_sha"] for proof in preview["children"]} == heads
    assert git(remote, "show-ref") == refs
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    result = await adopt(incident)
    assert result["outcome"] == "adopted"
    assert (await db.get_task(PARENT)).status == TaskStatus.COMPLETED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.FAILED
    completion = await db.get_task_completion(PARENT)
    assert completion.commits == [main]
    assert "not CI attested" in completion.verification
    proof = (await service.delivery_observer.observe([PARENT])).get(PARENT)
    assert proof.state == DeliveryState.CONTAINED
    assert proof.source_oid == main
    assert proof.request.parent_completion is None
    assert proof.request.parent_adoption.completion_id == completion.id
    from src.integration.epic_delivery import EpicDeliveryProjection

    assert (await EpicDeliveryProjection(db).for_tasks([PARENT]))[PARENT]["state"] == "delivered"
    async with db._engine.connect() as conn:
        operation = (await conn.execute(select(t.integration_repair_operations))).mappings().one()
        assert operation["state"] == "cancelled"
        assert operation["verifier_task_id"] == VERIFIER
        owner = (await conn.execute(select(t.integration_branch_owners))).mappings().one()
        assert (owner["handoff_state"], owner["fence_token"]) == ("released", 5)
        checkpoint = (await conn.execute(select(t.task_integration_checkpoints))).mappings().one()
        assert checkpoint["version"] == 10
        assert checkpoint["verified_sha"] is None
        for table in (
            t.integration_check_evidence,
            t.integration_parent_verifications,
            t.integration_parent_operation_completions,
        ):
            assert (await conn.execute(select(table))).all() == []
        releases = (await conn.execute(select(t.integration_delegate_releases))).mappings().all()
        assert [(row["task_id"], row["disposition"]) for row in releases] == [
            (VERIFIER, "cancelled")
        ]
        audit = json.loads(
            await conn.scalar(
                select(t.events.c.payload).where(
                    t.events.c.event_type == "integration.parent_adopted",
                )
            )
        )
        assert audit["head_sha"] == main
        assert len(audit["children"]) == len(children)
        assert audit["checkpoint_sha"] != main
    assert (await service.rows("p"))[-1]["evidence"]["conclusion"] == "not_ci_attested"


@pytest.mark.parametrize(
    "blocker",
    [
        "open_child",
        "missing_provenance",
        "parent_hold",
        "verifier_hold",
        "attached_owner",
        "assigned_verifier",
        "moved_target",
        "wrong_target",
        "pending_child",
        "settled_child",
    ],
)
async def test_child_adoption_refuses_incomplete_or_unsafe_evidence(incident, blocker):
    setup, children, heads, main = incident
    db, service, source, remote, _repo = setup
    overrides = {"accept_equivalent": True}
    if blocker == "open_child":
        await db.update_task(children[0], status=TaskStatus.READY)
    elif blocker == "missing_provenance":
        await db.save_task_completion(
            TaskCompletion(
                id="unretained",
                task_id=children[0],
                outcome="pass",
                commits=[heads[children[0]]],
                completed_at=time.time(),
            )
        )
    elif blocker in {"parent_hold", "verifier_hold"}:
        await db.set_task_meta(
            PARENT if blocker == "parent_hold" else VERIFIER,
            "manual_pause",
            {"reason": "human decision"},
        )
    elif blocker == "attached_owner":
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).values(handoff_state="attached"))
    elif blocker == "assigned_verifier":
        await db.update_task(VERIFIER, status=TaskStatus.IN_PROGRESS)
    elif blocker == "moved_target":
        overrides["head_sha"] = heads[children[0]]
    elif blocker == "wrong_target":
        overrides["target_ref"] = "refs/heads/" + children[0]
        overrides["head_sha"] = heads[children[0]]
    else:
        git(source, "checkout", children[0])
        (source / "undelivered.txt").write_text("new required work\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "undelivered child generation")
        head = git(source, "rev-parse", "HEAD")
        await complete_source(setup, children[0], "new-child-generation", head)
        if blocker == "settled_child":
            await db.set_task_meta(
                children[0],
                "development_delivery_settlement",
                {
                    "repository_id": "r",
                    "target_ref": "refs/heads/main",
                    "completion_id": "new-child-generation",
                    "reason": "not owed",
                },
            )
    before = git(remote, "show-ref")
    operations = await service.rows("p")
    with pytest.raises(ValueError):
        await adopt(incident, **overrides)
    assert git(remote, "show-ref") == before
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert await db.get_task_completion(PARENT) is None
    assert (await service.rows("p")) == operations


@pytest.mark.parametrize("change", ["child_reclose", "new_child"])
async def test_adoption_rechecks_child_set_and_generations_after_git_observation(
    incident,
    monkeypatch,
    change,
):
    from src.integration.delivered_parent_adoption import DeliveredParentAdoption

    setup, children, heads, _main = incident
    db, _service, *_ = setup
    prove = DeliveredParentAdoption.prove

    async def change_after_proof(self, facts, truth, **kwargs):
        result = await prove(self, facts, truth, **kwargs)
        if change == "child_reclose":
            await complete_source(setup, children[0], "concurrent-close", heads[children[0]])
        else:
            await db.create_task(
                Task(
                    id="new-child",
                    project_id="p",
                    title="new",
                    description="",
                    parent_task_id=PARENT,
                )
            )
        return result

    monkeypatch.setattr(DeliveredParentAdoption, "prove", change_after_proof)
    with pytest.raises(ValueError):
        await adopt(incident)
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None


@pytest.mark.parametrize("change", ["checkpoint", "reopen"])
async def test_parent_adoption_cannot_be_borrowed_after_binding_changes(incident, change):
    setup, _children, _heads, _main = incident
    db, service, *_ = setup
    await adopt(incident)
    if change == "checkpoint":
        async with db.immediate() as conn:
            await conn.execute(update(t.task_integration_checkpoints).values(version=11))
    else:
        await db.transition_task(PARENT, TaskStatus.READY, context="reopen for new work")
        # Even a bookkeeping reclose cannot reuse the old operator generation.
        async with db.immediate() as conn:
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == PARENT).values(status="COMPLETED")
            )
    proof = (await service.delivery_observer.observe([PARENT])).get(PARENT)
    assert proof.state == DeliveryState.UNKNOWN
    assert proof.reason == "invalid_parent_completion"


async def test_doctor_names_delivered_children_and_stale_verifier(incident):
    setup, _children, _heads, main = incident
    db, _service, *_ = setup
    result = await run_check(db, "integration.delivered_children_unsettled_parent")
    assert result.severity == Severity.WARN
    assert result.data["count"] == 1
    parent = result.data["parents"][0]
    assert parent["state"] == "delivered_children_unsettled_parent"
    assert parent["verifier_task_id"] == VERIFIER
    assert parent["head_sha"] == main
    assert "--settle-delivered-children --dry-run" in parent["command"]
    await adopt(incident)
    assert (
        await run_check(db, "integration.delivered_children_unsettled_parent")
    ).severity == Severity.OK


@pytest.mark.parametrize("proof_kind", ["equivalent", "no_artifact"])
async def test_child_proof_accepts_retained_equivalence_and_explicit_no_artifact(
    incident, proof_kind
):
    setup, children, _heads, main = incident
    db, service, source, _remote, repo = setup
    child, generation = children[0], "replacement-generation"
    git(source, "checkout", child)
    git(source, "commit", "--allow-empty", "-m", "child close reached main by an equivalent route")
    head = git(source, "rev-parse", "HEAD")
    binding = CompletedSource(CompletionIdentity("p", repo.id, child, generation), head)
    provenance = GitProvenance(service.git, str(source), repository_url=repo.url)
    await db.save_task_completion(
        TaskCompletion(
            id=generation,
            task_id=child,
            outcome="pass",
            commits=[head],
            completed_at=time.time(),
        )
    )
    await provenance.write_completion(binding, artifact=proof_kind != "no_artifact")
    if proof_kind == "equivalent":
        await provenance.write_replacement(
            source_oid=main,
            base_oid=git(source, "merge-base", head, main),
            replaces=[binding],
            authority="operator",
            reason="equivalent child already on main",
        )
    if proof_kind == "equivalent":
        with pytest.raises(
            ValueError, match=f"{child}: equivalent child delivery requires --accept-equivalent"
        ):
            await adopt(incident, dry_run=True)
        with pytest.raises(ValueError, match="--accept-equivalent"):
            await adopt(incident)
        assert await db.get_task_completion(PARENT) is None
    result = await adopt(incident, accept_equivalent=proof_kind == "equivalent")
    proof = next(proof for proof in result["children"] if proof["task_id"] == child)
    assert proof["state"] == ("contained" if proof_kind == "equivalent" else "no_artifact")
    assert proof["source_sha"] == head


@pytest.mark.parametrize(
    "holder", ["session", "retained_claim", "workspace", "gate", "human_operation", "hold_label"]
)
async def test_recovery_preserves_live_holders_and_human_decisions(incident, holder):
    setup, *_ = incident
    db, _service, *_ = setup
    if holder in {"session", "retained_claim"}:
        await db.create_session(
            SessionRecord(
                id="holder",
                project_id="p",
                task_id=VERIFIER,
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="holder",
                lifecycle="pool",
                work_dir="/tmp/holder",
                epoch="epoch",
                instance_token="token",
                started_at=time.time(),
                state="running" if holder == "session" else "stopped",
                desired_state="running" if holder == "session" else "stopped",
            )
        )
        if holder == "retained_claim":
            async with db.immediate() as conn:
                await conn.execute(update(t.sessions).values(claim_phase="claimed"))
    else:
        async with db.immediate() as conn:
            if holder == "workspace":
                await conn.execute(
                    insert(t.workspaces).values(
                        id="held-workspace",
                        project_id="p",
                        workspace_path="/tmp/held",
                        locked_by_task_id=VERIFIER,
                        created_at=time.time(),
                    )
                )
            elif holder == "gate":
                await conn.execute(
                    insert(t.gates).values(
                        id="decision",
                        project_id="p",
                        gate_type="human",
                        title="Keep verifier",
                        status="open",
                        created_at=time.time(),
                    )
                )
                await conn.execute(
                    insert(t.task_gates).values(task_id=VERIFIER, gate_id="decision")
                )
            elif holder == "hold_label":
                await conn.execute(
                    insert(t.task_labels).values(task_id=PARENT, label="hold:decision")
                )
            else:
                await conn.execute(
                    update(t.integration_repair_operations).values(state="human_required")
                )
    with pytest.raises(ValueError):
        await adopt(incident)
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None


async def test_recovery_cannot_bypass_reconciler_ownership(incident):
    from src.integration.parent_subjects import ParentSubjectAdapter
    from src.integration.subjects import PolicyArtifactPin
    from tests.test_integration_parent_completion import _artifact

    setup, *_ = incident
    db, _service, *_ = setup
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
        subject, _created = await ParentSubjectAdapter(db).ensure_on(
            conn,
            PARENT,
            policy=PolicyArtifactPin(
                playbook_id=artifact.playbook_id,
                artifact_sha256=artifact.artifact_sha256,
            ),
            max_wait_seconds=300,
        )
        await conn.execute(
            update(t.integration_subjects)
            .where(
                t.integration_subjects.c.id == subject.id,
            )
            .values(engine="reconciler")
        )
    result = await adopt(incident)
    assert result["outcome"] == "blocked"
    assert "reconciler" in result["reason"]
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert await db.get_task_completion(PARENT) is None


async def test_recovery_refuses_unresolved_remote_write(incident):
    setup, _children, _heads, main = incident
    db, _service, *_ = setup
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_promotion_intents).values(
                id="uncertain",
                domain_key="uncertain",
                operation_key="collection",
                receipt_id="unwritten",
                project_id="p",
                repository_id="r",
                target_branch="aq/epic/old-aggregate",
                source_head=main,
                source_base=main,
                expected_target=main,
                fence_owner_id="collection",
                fence_token=4,
                state="pushed",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    with pytest.raises(ValueError, match="unresolved external write"):
        await adopt(incident)
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None


async def test_live_incident_refuses_and_names_undelivered_fourth_child(incident):
    setup, children, _heads, _main = incident
    db, _service, source, remote, _repo = setup
    child = children[3]
    git(source, "checkout", child)
    (source / "phase-four.txt").write_text("fourth child is still owed\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "undelivered fourth child")
    await complete_source(setup, child, "live-fourth-generation", git(source, "rev-parse", "HEAD"))
    refs = git(remote, "show-ref")
    for dry_run in (True, False):
        with pytest.raises(ValueError, match=f"{child}: child delivery is pending"):
            await adopt(incident, dry_run=dry_run, accept_equivalent=True)
    assert git(remote, "show-ref") == refs
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None
