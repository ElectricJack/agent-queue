"""Real Git recovery for parents whose children reached main by other routes."""

import time

import pytest
from sqlalchemy import insert, select, update

from src.database import tables as t
from src.integration.delivery_truth import DeliveryState
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.models import Task, TaskCompletion, TaskStatus
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


async def test_current_git_observer_preserves_stale_parent_and_writer(incident):
    db, service, _source, _remote, _repo = incident[0]
    children = incident[1]
    view = await service.delivery_observer.observe(children)
    async with db._engine.connect() as conn:
        evidence = await view.verified_on(conn, children)
        owner = (await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner"
        ))).mappings().one()
    assert set(evidence) == set(children)
    assert all(item.state is DeliveryState.CONTAINED for item in evidence.values())
    assert (await db.get_task(PARENT)).status is TaskStatus.PAUSED
    assert (await db.get_task(VERIFIER)).status is TaskStatus.READY
    assert owner["handoff_state"] == "reserved" and owner["fence_token"] == 4
    assert await db.get_task_completion(PARENT) is None


async def test_current_git_observer_rechecks_reopened_child_generation(incident):
    setup, children, _heads, _main = incident
    db, service, source, _remote, _repo = setup
    child = children[0]
    view = await service.delivery_observer.observe(children)
    git(source, "checkout", child)
    git(source, "commit", "--allow-empty", "-m", "undelivered generation")
    await complete_source(setup, child, "reopened-close", git(source, "rev-parse", "HEAD"))
    async with db._engine.connect() as conn:
        evidence = await view.verified_on(conn, children)
    assert child not in evidence  # The old view cannot answer the new generation.
    refreshed = await service.delivery_observer.observe(children, max_age=0)
    async with db._engine.connect() as conn:
        current = await refreshed.verified_on(conn, [child])
    assert current[child].state is DeliveryState.PENDING


@pytest.mark.parametrize("proof_kind", ["equivalent", "no_artifact"])
async def test_current_git_observer_accepts_exact_equivalence_and_no_artifact(incident, proof_kind):
    setup, children, _heads, main = incident
    db, service, source, _remote, repo = setup
    child, generation = children[0], "replacement-generation"
    git(source, "checkout", child)
    git(source, "commit", "--allow-empty", "-m", "delivered by equivalent route")
    head = git(source, "rev-parse", "HEAD")
    binding = CompletedSource(CompletionIdentity("p", repo.id, child, generation), head)
    provenance = GitProvenance(service.git, str(source), repository_url=repo.url)
    await db.save_task_completion(TaskCompletion(
        id=generation, task_id=child, outcome="pass", commits=[head], completed_at=time.time()
    ))
    await provenance.write_completion(binding, artifact=proof_kind != "no_artifact")
    if proof_kind == "equivalent":
        await provenance.write_replacement(
            source_oid=main, base_oid=git(source, "merge-base", head, main), replaces=[binding],
            authority="operator", reason="equivalent child already on main"
        )
    view = await service.delivery_observer.observe([child], max_age=0)
    async with db._engine.connect() as conn:
        evidence = await view.verified_on(conn, [child])
    assert evidence[child].satisfied
    assert evidence[child].source_oid == head
