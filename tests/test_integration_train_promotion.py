"""One-step request → pinned human review → exact FF and annotated release tag."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, update

from src.database.tables import integration_check_evidence, integration_outbox, projects
from src.integration.ci import TRUST_MANIFEST_PATH
from src.integration.promotion_steps import FlowSchema
from src.integration.train import IntegrationTrain, TrainTarget
from src.integration.train_sources import DaemonLanes, DatabaseBatches, LeasedPublish
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_promotion_policy import policy_engine
from tests.test_promotion_steps import promotion as promotion
from tests.test_promote_commands import (
    production_push as production_push, promote_env as promote_env, request,
)


async def test_one_step_release_pr_checks_approval_publish_and_annotated_tag(production_push):
    e = production_push
    opened = await request(e, source_sha=e.source)
    assert opened["outcome"] == "requested", opened
    meta = opened["promotion"]
    assert e.github.pull["head"]["sha"] == e.source
    assert e.github.pull["base"]["ref"] == "main"
    assert not e.github.reviews
    batch = await e.store.get(opened["batch_id"])
    members = await e.store.members(batch.id)
    e.github.runs[e.source] = "success"

    async def load_trust(state):
        assert state["candidate_sha"] == e.source
        return e.trust, e.github

    orchestrator = SimpleNamespace(
        db=e.db,
        git=e.ops.git,
        integration_attestation_service=SimpleNamespace(
            _load_trust=load_trust, _subject_manifest=AsyncMock(return_value=e.trust)
        ),
    )
    batches = DatabaseBatches(e.db)
    lanes = DaemonLanes(orchestrator, batches=batches)
    lanes.publish = LeasedPublish(e.db, e.ops.git)
    lane = await lanes._promotion_lane(
        TrainTarget("p", "r", "refs/heads/main", "promotion", step=meta["step"]),
        e.repo,
        e.repo.binding,
        e.ops,
        {
            "root": {
                "required_checks": {
                    "version": "checks-v1",
                    "names": ["unit"],
                    "producer_id": "15368",
                }
            }
        },
        e.snapshot,
    )
    held = await lane.service.visit(batch, members, await e.snapshot())
    assert held.state == "held" and held.detail["reason"] == "promotion_operator_approval_missing"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    approval = await e.handler._cmd_promote_approve(
        {"project_id": "p", "request_id": meta["request_id"]}
    )
    assert approval["outcome"] == "approved", approval
    assert approval["review"]["user"]["login"] == "operator"
    result = await lane.service.visit(batch, members, await e.snapshot())
    assert result.state == "delivered", result
    await batches.settle(batch, result)
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert ("main", e.base) in e.exact_pushes
    assert git(e.ops.git.remote_path, "cat-file", "-t", "refs/tags/v0.2.0") == "tag"
    assert git(e.ops.git.remote_path, "rev-parse", "refs/tags/v0.2.0^{}") == e.source
    pull = await e.github.request_json("GET", "/repositories/123/pulls/42")
    assert pull["merged"] is True and pull["state"] == "closed"
    status = await e.handler._cmd_promote_status({"project_id": "p"})
    [entry] = status["promotions"]
    assert entry["lifecycle"] == "promoted" and entry["result"]["source_sha"] == e.source
    async with e.db._engine.connect() as conn:
        evidence = (await conn.execute(select(integration_check_evidence))).mappings().all()
    assert evidence and all(row["sha"] == e.source for row in evidence)


async def test_continuous_train_reobserves_push_ci_and_promotes_without_new_settlement(
    production_push, monkeypatch,
):
    e = production_push
    step = FlowSchema.validate([{
        "id": "release", "source": "dev", "target": "main", "type": "continuous",
        "gate": {"approval": "none"}, "after": {"backmerge": False},
    }], default_branch="dev").flow[0]
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(
            promotion_flow=[step], hierarchical_integration_mode="train"))
    e.source = commit(e.repo.store, {
        TRUST_MANIFEST_PATH: e.trust.model_dump_json(by_alias=True),
    }, base=e.source)
    git(e.repo.store, "push", "origin", f"{e.source}:dev")
    monkeypatch.setattr(e.ops.git, "_github_client", lambda _: e.github)
    activations = [{
        "id": "continuous", "playbook_id": "promotion-continuous", "scope": "project",
        "scope_identifier": "p", "health": "ready", "active_artifact_sha256": "sha256:" + "a" * 64,
    }]
    e.db.list_playbook_activations = AsyncMock(return_value=[])
    batches = DatabaseBatches(e.db)
    orchestrator = SimpleNamespace(db=e.db, git=e.ops.git,
        integration_attestation_service=SimpleNamespace(
            _load_trust=AsyncMock(return_value=(e.trust, e.github))))
    target = TrainTarget("p", "r", "refs/heads/main", "promotion", step=step)
    # Promotion source trust belongs to the committed default manifest, even
    # when the ordinary integration lane has no hosted root-check policy.
    policy = {}

    def make_train():
        lanes = DaemonLanes(orchestrator, batches=batches)

        async def lane_for(target):
            return await lanes._promotion_lane(target, replace(e.repo, default_branch="dev"),
                e.repo.binding, e.ops, policy, e.snapshot)

        return IntegrationTrain(targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
            batches=batches, lane_for=lane_for,
            repair=SimpleNamespace(settle_green=AsyncMock()))

    train = make_train()

    async def tick():
        await train.tick()
        await train._lanes[target.key].task
        assert train.status()[0]["errors"] == 0, train.status()

    async def notifications():
        async with e.db._engine.connect() as conn:
            return (await conn.execute(select(integration_outbox).where(
                integration_outbox.c.event_type == "promotion.source_settled"))).mappings().all()

    # Activation after startup and CI finishing after settlement both need a
    # level-triggered observation; no new completion event is supplied here.
    e.github.runs[e.source] = "success"
    await tick()
    assert not await notifications()
    e.db.list_playbook_activations.return_value = activations
    for result in ("pending", "failure"):
        e.github.runs[e.source] = result
        await tick()
        assert not await notifications()
    e.github.runs[e.source] = "success"
    await tick()
    [event] = await notifications()
    assert event["payload"]["source_sha"] == e.source
    # Restart and repeated visits reuse the durable event identity.
    train = make_train()
    await tick()
    assert len(await notifications()) == 1
    e.handler.orchestrator.integration_train = train
    continuous = await policy_engine(e, "continuous")
    await continuous.run("advance-source", source_sha=event["payload"]["source_sha"])
    requested = [result for command, result in continuous.results if command == "promote_request"]
    assert len(requested) == 1 and requested[0]["outcome"] == "requested", continuous.results
    assert requested[0]["promotion"]["source_sha"] == e.source
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    await tick()
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert ("main", e.base) in e.exact_pushes
    await tick()
    assert len(await notifications()) == 1
    assert e.github.created == 1
