"""Connected promotion policy, exact FF, release tags and continuous lag diagnostics."""

from dataclasses import replace
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.config import AppConfig
from src.database.tables import integration_check_evidence, integration_outbox, projects
from src.doctor.integration_checks import _BY_ID, continuous_promotion_lag
from src.doctor.models import DoctorContext, Severity
from src.integration.ci import TRUST_MANIFEST_PATH
from src.integration.outbox import IntegrationOutbox
from src.integration.promotion_steps import FlowSchema
from src.integration.service import IntegrationService
from src.integration.train import IntegrationTrain, TrainTarget
from src.integration.train_sources import DaemonLanes, DatabaseBatches, LeasedPublish
from src.playbooks.run_state import RunLifecycle
from src.playbooks.services import resolve_integration_route
from tests.test_integration_gitops import git_history_template as git_history_template
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
    continuous = await policy_engine(e, "continuous")
    activations[0]["active_artifact_sha256"] = continuous.ref.artifact_sha256
    e.handler._cmd_integration_promotion_publish = (
        IntegrationCommandsMixin._cmd_integration_promotion_publish.__get__(e.handler))

    async def accept(event_type, payload, event_id):
        route = await resolve_integration_route(e.db, event_type, payload)
        assert route is not None
        outcome = await continuous.engine.dispatch_event(
            {**payload, "_event_type": event_type}, continuous.principal,
            playbook_ids=[route.playbook_id], dispatch_id=event_id,
        )
        assert outcome.run_ids or outcome.deduplicated, outcome
        for run_id in outcome.run_ids:
            assert (await continuous.runs.load_run(run_id)).lifecycle is RunLifecycle.COMPLETED
        return True

    def make_train():
        lanes = DaemonLanes(orchestrator, batches=batches)

        async def lane_for(target):
            return await lanes._promotion_lane(target, replace(e.repo, default_branch="dev"),
                e.repo.binding, e.ops, policy, e.snapshot)

        train = IntegrationTrain(targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
            batches=batches, lane_for=lane_for,
            repair=SimpleNamespace(settle_green=AsyncMock()))
        e.handler.orchestrator.integration_train = train
        return train, IntegrationService(e.db, IntegrationOutbox(e.db, accept), train=train)

    train, service = make_train()

    async def tick():
        await service.tick(service._clock())
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
        assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    e.github.runs[e.source] = "success"
    await tick()
    [event] = await notifications()
    assert event["payload"]["source_sha"] == e.source
    # Restart and repeated visits reuse the durable event identity.
    await service.stop()
    train, service = make_train()
    await tick()
    [event] = await notifications()
    assert event["delivered_at"] is not None and event["attempts"] == 1
    requested = [result for command, result in continuous.results if command == "promote_request"]
    assert len(requested) == 1 and requested[0]["outcome"] == "requested", continuous.results
    assert requested[0]["promotion"]["source_sha"] == e.source
    await tick()
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert ("main", e.base) in e.exact_pushes
    await tick()
    assert len(await notifications()) == 1
    assert e.github.created == 1
    assert all(run.lifecycle is RunLifecycle.COMPLETED
               for run in continuous.runs.snapshots.values()), continuous.runs.snapshots
    await service.stop()


@pytest.mark.parametrize("case", ["stale", "fresh", "caught_up", "request", "disabled", "paused",
                                  "invalid", "missing_target", "diverged"])
async def test_continuous_lag_uses_oldest_unpromoted_work_and_exact_git_heads(
    promote_env, monkeypatch, case,
):
    e = promote_env
    now = 1_800_000_000
    base = e.source
    step = FlowSchema.validate([{
        "id": "staging", "source": "dev", "target": "staging",
        "type": "request" if case == "request" else "continuous",
        "gate": {"approval": "operator" if case == "request" else "none"},
        "after": {"backmerge": False},
    }], default_branch="dev").flow[0]
    if case == "invalid":
        step["source"] = "other"
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step],
            status="PAUSED" if case == "paused" else "ACTIVE",
            hierarchical_integration_mode="disabled" if case == "disabled" else "train"))
    oldest = now - (3600 if case == "fresh" else 7 * 3600)
    with monkeypatch.context() as dated:
        dated.setenv("GIT_COMMITTER_DATE", f"{oldest} +0000")
        source = commit(e.repo.store, {"first.txt": "unpromoted\n"}, base=base)
        dated.setenv("GIT_COMMITTER_DATE", f"{now - 300} +0000")
        source = commit(e.repo.store, {"latest.txt": "new source advancement\n"}, base=source)
    target = source if case == "caught_up" else base
    if case == "diverged":
        target = commit(e.repo.store, {"target-only.txt": "target advanced\n"}, base=base)
    git(e.repo.store, "push", "origin", f"{source}:dev")
    if case != "missing_target":
        git(e.repo.store, "push", "origin", f"{target}:refs/heads/staging")
    snapshot = await e.snapshot()
    observer = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
    checks = importlib.import_module("src.doctor.integration_checks")
    monkeypatch.setattr(checks, "_doctor_observer", lambda *a, **kw: observer)
    ctx = DoctorContext(db=e.db, config=AppConfig())
    report = await continuous_promotion_lag(ctx, now=now)
    if case in {"stale", "diverged"}:
        assert len(report["findings"]) == 1, report
        [finding] = report["findings"]
        assert finding["project_id"] == "p" and finding["step_id"] == "staging"
        assert finding["source_sha"] == source and finding["target_sha"] == target
        assert finding["ahead"] == 2
        assert finding["behind"] == (1 if case == "diverged" else 0)
        assert finding["oldest_unpromoted_at"] == oldest
        assert finding["age_seconds"] == 7 * 3600
        assert not report["unavailable"]
        # The threshold is part of the diagnostic input and reported evidence.
        assert not (await continuous_promotion_lag(ctx, now=now, max_age_seconds=8 * 3600))["findings"]
        assert not (await continuous_promotion_lag(ctx, project_ids={"other"}, now=now))["findings"]
    elif case == "missing_target":
        assert not report["findings"]
        assert report["unavailable"][0]["step_id"] == "staging"
    else:
        assert not report["findings"] and not report["unavailable"]
    monkeypatch.setattr(checks.time, "time", lambda: now)
    check = _BY_ID["integration.continuous_promotion_lag"]
    result = await check.run(ctx)
    assert result.severity == (Severity.WARN if case in {"stale", "diverged", "missing_target"}
                               else Severity.OK)
    assert not result.fixable
    assert git(e.ops.git.remote_path, "rev-parse", "dev") == source
