"""Actual resource producers, persisted registry bindings and fresh service proofs.

The callback is the admission-reader seam; listener/registry owners must run the
same producer cases with their concrete services before runtime activation.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import provider_availability, tasks, wait_records, wait_source_events, workspaces
from src.models import Project, RepoSourceType, Task, Workspace
from src.orchestrator.core import Orchestrator
from src.scheduler import PlacementCandidate, PoolKey, PoolSupply
from src.waits.resource_sources import (
    RESOURCE_BLOCKERS, reobserve_resource_candidates, resource_invalidation_candidates,
    resource_source_key,
)
from src.waits.sources import execute_workspace_release, record_pool_observations


@pytest.fixture
async def db(reuse_database):
    db = await reuse_database("resource-source-contract")
    for project in ("p", "q"):
        await db.create_project(Project(id=project, name=project))
        await db.create_task(Task(id=f"owner-{project}", project_id=project, title=project,
                                  description=""))
    return db


async def subscription(db, *, wid="wait", blocker="workspace", project="p", **overrides):
    """Persist the registry's canonical record format with an opaque service id."""
    identity = f"opaque-admission-{wid}"  # Never inferred from profile/workspace labels.
    context = dict(project_id=project, owner_kind="task", owner_id=f"owner-{project}",
                   session_id=None, claim_epoch=0, source_class=f"resource:{blocker}")
    condition = dict(kind="resource", blocker=blocker, identity=identity,
                     source_revision="cycle-A", generation=0, _verified=context)
    values = dict(id=wid, project_id=project, owner_kind="task", owner_id=f"owner-{project}",
                  origin="scheduler", session_id=None, claim_epoch=0,
                  condition_kind="resource", condition=condition,
                  source_key=resource_source_key(blocker, identity), source_generation=0,
                  source_watermark_seq=await db.wait_source_watermark(),
                  verification_state="verified", evidence_ref="admission:cycle-A", verified_at=1,
                  created_at=1, request_hash=wid)
    values.update(overrides)
    async with db._engine.begin() as conn:
        await conn.execute(insert(wait_records).values(**values))
    return values


async def latest(db, producer):
    async with db._engine.connect() as conn:
        return dict((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key.startswith(f"resource_invalidation:{producer}:"),
        ).order_by(wait_source_events.c.seq.desc()))).mappings().first())


def evidence(row, *, satisfied=True, revision="cycle-B", **overrides):
    value = SimpleNamespace(
        condition=SimpleNamespace(context=row["condition"]["_verified"]),
        source=SimpleNamespace(identity=row["source_key"], generation=0),
        observation=SimpleNamespace(state="satisfied" if satisfied else "unsatisfied",
                                    revision=revision, result_ref="admission:fresh-predicate"),
    )
    for name, replacement in overrides.items():
        setattr(value, name, replacement)
    return value


@pytest.mark.parametrize("producer", ["pool", "workspace", "provider", "config"])
async def test_cycle_a_subscription_reobserves_actual_cycle_b_or_external_mutation(db, producer):
    key = PoolKey(profile_id="worker")
    measurement = SimpleNamespace(supply={key: PoolSupply(running_busy=2)}, demand={key: 1},
                                  bounds={key: (0, 2)})
    orch = SimpleNamespace(db=db, _config_watcher=None, budget=SimpleNamespace(),
                           config=SimpleNamespace(global_token_budget_daily=0))
    await db.create_workspace(Workspace(id="instance", project_id="p", workspace_path="/tmp/rs",
                                        source_type=RepoSourceType.LINK,
                                        locked_by_task_id="owner-p"))
    await db.save_provider_availability(dict(provider="codex", state="disabled", since=1,
                                             updated_at=1))
    await record_pool_observations(db, measurement, 100)  # Unconditional, with no subscriptions.
    blocker = dict(pool="slot", workspace="workspace", provider="provider", config="budget")[producer]
    row = await subscription(db, blocker=blocker)

    async def verify(conn, candidate):
        # Concrete service-side scope/owner freshness, not event-payload truth.
        owner = (await conn.execute(select(tasks).where(
            tasks.c.id == candidate["owner_id"],
        ))).mappings().one()
        if (owner["project_id"] != candidate["project_id"]
                or owner["claim_epoch"] != candidate["claim_epoch"]):
            return None
        if producer == "pool":
            eligible = measurement.supply[key].running_busy < measurement.bounds[key][1]
        elif producer == "workspace":
            workspace = (await conn.execute(select(workspaces).where(
                workspaces.c.id == "instance", workspaces.c.project_id == candidate["project_id"],
            ))).mappings().first()
            eligible = workspace is not None and workspace["locked_by_task_id"] is None
        elif producer == "provider":
            eligible = await conn.scalar(select(provider_availability.c.state).where(
                provider_availability.c.provider == "codex",
            )) == "available"
        else:
            eligible = orch.config.global_token_budget_daily >= 100
        return evidence(candidate, satisfied=eligible)

    async with db._engine.connect() as conn:
        assert not await reobserve_resource_candidates(conn, [row], verify=verify)
    if producer == "pool":
        measurement.supply[key] = PoolSupply(running_busy=0)
        await record_pool_observations(db, measurement, 200)
        await record_pool_observations(db, measurement, 200)  # Same revision is a no-op.
    elif producer == "workspace":
        await db.release_workspaces_for_task("owner-p")
        await db.release_workspaces_for_task("owner-p")
    elif producer == "provider":
        value = dict(provider="codex", state="available", since=2, updated_at=2)
        transition = dict(provider="codex", from_state="disabled", to_state="available",
                          generation=1, at=2)
        await db.save_provider_availability(value, transition)
        await db.save_provider_availability(value, transition)
    else:
        reload = {"config": SimpleNamespace(global_token_budget_daily=100),
                  "changed_sections": ["scheduling"]}
        await Orchestrator._on_config_reloaded(orch, reload)
        await Orchestrator._on_config_reloaded(orch, reload)
    event = await latest(db, producer)
    async with db._engine.connect() as conn:
        candidates = await resource_invalidation_candidates(conn, event)
        observed = await reobserve_resource_candidates(conn, candidates, verify=verify)
        assert len(observed) == 1
        assert observed[0][0]["source_key"] == row["source_key"]
        assert observed[0][1].observation.revision != row["condition"]["source_revision"]
        assert observed[0][1].observation.result_ref != event["outcome_ref"]
        # The bridge produces evidence; only a guarded dispatcher can resolve.
        assert await conn.scalar(select(wait_records.c.outcome)) == "pending"
        assert len((await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == event["source_key"],
            wait_source_events.c.source_revision == event["source_revision"],
        ))).all()) == 1
        await conn.rollback()
    # A later owner incarnation cannot be satisfied by an old resource wait.
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner-p").values(claim_epoch=1))
    async with db._engine.connect() as conn:
        assert not await reobserve_resource_candidates(conn, [row], verify=verify)


async def test_bridge_scopes_canonical_bindings_and_pages_without_guessing_identity(db):
    valid = await subscription(db, wid="a")
    await subscription(db, wid="b")
    await subscription(db, wid="foreign", project="q")
    await subscription(db, wid="incarnation", source_generation=1)
    await subscription(db, wid="aggregate", source_key="resource:pool:worker")
    await subscription(db, wid="owner-binding", owner_id="different-owner")
    await subscription(db, wid="not-verified", verification_state="stale")
    await subscription(db, wid="unrelated-type", blocker="budget")
    await db.create_workspace(Workspace(id="w", project_id="p", workspace_path="/tmp/scoped",
                                        source_type=RepoSourceType.LINK,
                                        locked_by_task_id="owner-p"))
    await db.release_workspaces_for_task("owner-p")
    event = await latest(db, "workspace")
    await subscription(db, wid="registered-after-bound")
    async with db._engine.connect() as conn:
        first = await resource_invalidation_candidates(conn, event, limit=1)
        second = await resource_invalidation_candidates(conn, event, after_id=first[-1]["id"], limit=1)
        assert [r["id"] for r in first + second] == ["a", "b"]
        assert not await resource_invalidation_candidates(conn, event, after_id="b")
        # A fresh but unsatisfied unrelated predicate never yields evidence.
        async def unsatisfied(_conn, row):
            return evidence(row, satisfied=False)
        assert not await reobserve_resource_candidates(conn, [valid], verify=unsatisfied)


@pytest.mark.parametrize("tamper", ["revision", "identity", "incarnation", "payload", "sequence"])
async def test_bridge_refuses_uncommitted_or_wrong_event_identity_revision_and_payload(db, tamper):
    await subscription(db)
    await db.create_workspace(Workspace(id="w", project_id="p", workspace_path="/tmp/tamper",
                                        source_type=RepoSourceType.LINK,
                                        locked_by_task_id="owner-p"))
    await db.release_workspaces_for_task("owner-p")
    event = deepcopy(await latest(db, "workspace"))
    if tamper == "revision":
        event["source_revision"] = "not-the-committed-revision"
    elif tamper == "identity":
        event["source_key"] = "resource_invalidation:workspace:other"
    elif tamper == "incarnation":
        event["source_generation"] = 1
    elif tamper == "payload":
        event["payload"]["project_id"] = "q"
    else:
        event["seq"] += 100
    async with db._engine.connect() as conn:
        assert not await resource_invalidation_candidates(conn, event)


@pytest.mark.parametrize("field", ["identity", "incarnation", "project", "owner", "session", "claim"])
async def test_bridge_requires_fresh_typed_evidence_to_keep_all_subscription_fences(db, field):
    row = await subscription(db)

    async def wrong(_conn, candidate):
        proof = evidence(candidate)
        proof.condition.context = deepcopy(proof.condition.context)
        if field == "identity":
            proof.source.identity = "resource:workspace:unrelated"
        elif field == "incarnation":
            proof.source.generation = 1
        else:
            key = dict(project="project_id", owner="owner_id", session="session_id",
                       claim="claim_epoch")[field]
            proof.condition.context[key] = "different"
        return proof
    async with db._engine.connect() as conn:
        assert not await reobserve_resource_candidates(conn, [row], verify=wrong)


async def test_workspace_mutation_and_invalidation_roll_back_together(db):
    await subscription(db)
    await db.create_workspace(Workspace(id="w", project_id="p", workspace_path="/tmp/rollback",
                                        source_type=RepoSourceType.LINK,
                                        locked_by_task_id="owner-p"))
    with pytest.raises(RuntimeError, match="abort"):
        async with db._engine.begin() as conn:
            await execute_workspace_release(conn, update(workspaces).where(
                workspaces.c.id == "w",
            ).values(locked_by_task_id=None))
            raise RuntimeError("abort")
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(wait_source_events))).all()
        assert await conn.scalar(select(workspaces.c.locked_by_task_id)) == "owner-p"


def test_only_admitted_resource_blockers_form_canonical_keys():
    for blocker in RESOURCE_BLOCKERS:
        assert resource_source_key(blocker, "opaque:owner/config/instance") == (
            f"resource:{blocker}:opaque:owner/config/instance"
        )
    for category in ("pool", "config"):
        with pytest.raises(ValueError):
            resource_source_key(category, "worker")


async def test_batched_pool_bounds_audit_preserves_every_profile_and_announces_once(db):
    emitted = []

    async def emit(kind, payload):
        emitted.append((kind, payload))

    profiles = {PoolKey(profile_id=f"worker-{n}"): SimpleNamespace(max_active=n + 1, min_active=n)
                for n in range(3)}
    candidates = [PlacementCandidate(
        project_id=project_id, ready=0, live=0, project_live_total=0, project_cap=None,
        workspace_capacity=1, quarantined=False, warm_floor=0, workspace_kind=kind,
    ) for project_id, kind in (("p", "project-repo"), ("p", "other"), ("q", "project-repo"))]
    measurement = SimpleNamespace(profiles=profiles, candidates={key: candidates for key in profiles})
    orch = SimpleNamespace(db=db, bus=SimpleNamespace(emit=emit))
    await Orchestrator._announce_bounds_rescoped(orch, measurement)
    await Orchestrator._announce_bounds_rescoped(orch, measurement)
    rows = await db.get_recent_events(event_type="pool.bounds_rescoped")
    assert len(rows) == len(emitted) == 3
    import json

    payloads = {json.loads(row["payload"])["profile_id"]: json.loads(row["payload"]) for row in rows}
    assert set(payloads) == {key.profile_id for key in profiles}
    for key, profile in profiles.items():
        assert payloads[key.profile_id]["effective_max_active"] == profile.max_active
        assert payloads[key.profile_id]["previous_effective_max_active"] == profile.max_active * 2
        assert payloads[key.profile_id]["previous_effective_min_active"] == profile.min_active * 2
    assert {payload["profile_id"] for _, payload in emitted} == set(payloads)
