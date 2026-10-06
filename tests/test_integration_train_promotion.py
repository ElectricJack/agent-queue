"""One-step request → pinned human review → exact FF and annotated release tag."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from src.database.tables import integration_check_evidence
from src.integration.train import TrainTarget
from src.integration.train_sources import DaemonLanes, DatabaseBatches, LeasedPublish
from tests.test_integration_gitops import git, setup as setup
from tests.test_promotion_steps import promotion as promotion
from tests.test_promote_commands import promote_env as promote_env, request


async def test_one_step_release_pr_checks_approval_publish_and_annotated_tag(promote_env):
    e = promote_env
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
