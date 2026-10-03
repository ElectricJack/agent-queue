"""Exact-base proposal acceptance, decision replay and worker permission boundaries."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import knowledge_revisions
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    return KnowledgeService(db, knowledge_config())


async def proposal(service, *, body="correction"):
    record = await service.create(snapshot=snapshot(), idempotency_key="create", **LOCAL)
    proposed = await service.propose(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        snapshot=snapshot(body=body),
        idempotency_key="propose",
        **LOCAL,
    )
    return record, proposed


def decide_args(record, proposed, key="accept"):
    return dict(
        proposal_id=proposed["proposal_id"],
        proposal_sha256=proposed["proposal_sha256"],
        if_revision=record["revision_id"],
        decision="accept",
        reason="Reviewed evidence",
        idempotency_key=key,
        **LOCAL,
    )


async def test_exact_base_acceptance_two_connections_and_replay(service):
    record, proposed = await proposal(service)
    a, b = await asyncio.gather(
        service.proposal_decide(**decide_args(record, proposed)),
        service.proposal_decide(**decide_args(record, proposed, "second")),
    )
    assert a["revision_id"] == b["revision_id"]
    assert a["state"] == b["state"] == "accepted"
    replay = await service.proposal_decide(**decide_args(record, proposed))
    assert replay["outcome"] == "replayed"
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_revisions)) == 2


async def test_competing_proposals_have_one_winner_and_one_stale(service):
    record, a = await proposal(service)
    b = await service.propose(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        snapshot=snapshot(body="other"),
        idempotency_key="other",
        **LOCAL,
    )
    results = await asyncio.gather(
        service.proposal_decide(**decide_args(record, a)),
        service.proposal_decide(**decide_args(record, b, "other")),
    )
    assert sorted(r["state"] for r in results) == ["accepted", "stale"]


async def test_edit_before_accept_is_stale_not_merge(service):
    record, proposed = await proposal(service)
    edited = await service.update(
        identity=f"record:{record['record_id']}",
        patch={"body": "newer"},
        if_revision=record["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    result = await service.proposal_decide(**decide_args(record, proposed))
    assert result["state"] == "stale"
    current = await service.show(identity=f"record:{record['record_id']}", **LOCAL)
    assert current["revision_id"] == edited["revision_id"]
    assert current["snapshot"]["body"] == "newer"


async def test_decision_binds_proposal_hash_and_base(service):
    record, proposed = await proposal(service)
    for patch, code in [
        ({"proposal_sha256": "0" * 64}, "knowledge.proposal_conflict"),
        ({"if_revision": None}, "record.precondition_required"),
    ]:
        with pytest.raises(RecordError) as exc:
            await service.proposal_decide(**{**decide_args(record, proposed), **patch})
        assert exc.value.code == code
    accepted = await service.proposal_decide(**decide_args(record, proposed))
    assert accepted["state"] == "accepted"
    with pytest.raises(RecordError, match="knowledge.proposal_decided"):
        await service.proposal_decide(
            **{**decide_args(record, proposed, "reject"), "decision": "reject"}
        )


async def test_worker_proposes_other_record_but_cannot_decide_even_with_grant(service):
    worker = await worker_principal(
        service.db,
        grants=[
            "knowledge_propose",
            "knowledge_proposal_show",
            "knowledge_proposal_decide",
            "knowledge_update",
        ],
    )
    record = await service.create(snapshot=snapshot(), idempotency_key="create", **LOCAL)
    args = dict(principal=worker, project_id="p", claim_epoch=1)
    with pytest.raises(RecordError, match="Only the author"):
        await service.update(
            identity=f"record:{record['record_id']}",
            patch={"body": "overwrite"},
            if_revision=record["revision_id"],
            idempotency_key="edit",
            **args,
        )
    proposed = await service.propose(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        snapshot=snapshot(body="proposal"),
        idempotency_key="propose",
        **args,
    )
    with pytest.raises(RecordError, match="record.forbidden"):
        await service.proposal_decide(**{**decide_args(record, proposed), "principal": worker})
    accepted = await service.proposal_decide(**decide_args(record, proposed))
    assert accepted["state"] == "accepted"


async def test_new_record_proposal_and_rejection_preserve_evidence(service):
    proposed = await service.propose(snapshot=snapshot(), idempotency_key="new", **LOCAL)
    result = await service.proposal_decide(
        proposal_id=proposed["proposal_id"],
        proposal_sha256=proposed["proposal_sha256"],
        decision="reject",
        reason="Insufficient evidence",
        idempotency_key="reject",
        **LOCAL,
    )
    assert result["state"] == "rejected"
    shown = await service.proposal_show(proposal_id=proposed["proposal_id"], **LOCAL)
    assert shown["snapshot"] == snapshot()
    other = await service.propose(snapshot=snapshot(title="New"), idempotency_key="new2", **LOCAL)
    accepted = await service.proposal_decide(
        proposal_id=other["proposal_id"],
        proposal_sha256=other["proposal_sha256"],
        decision="accept",
        reason="Retain assertion",
        idempotency_key="accept",
        **LOCAL,
    )
    assert UUID(accepted["record_id"])
    assert accepted["sequence"] == 1


async def test_proposal_link_change_is_one_revision_and_exact_history(service):
    record, _ = await proposal(service)
    target = await service.create(
        snapshot=snapshot(title="Target"), idempotency_key="target", **LOCAL
    )
    p = await service.propose(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        snapshot=snapshot(),
        link_operations=[
            dict(action="add", target=f"record:{target['record_id']}", link_type="references")
        ],
        idempotency_key="links",
        **LOCAL,
    )
    accepted = await service.proposal_decide(**decide_args(record, p))
    now = await service.show(identity=f"record:{record['record_id']}", **LOCAL)
    old = await service.show(
        identity=f"record:{record['record_id']}", revision_id=record["revision_id"], **LOCAL
    )
    assert now["sequence"] == accepted["sequence"] == 2
    assert len(now["snapshot"]["outgoing_links"]) == 1
    assert old["snapshot"]["outgoing_links"] == []


async def test_service_dispatch_bypass_does_not_grant_proposal_authority(service):
    from dataclasses import replace
    from src.commands.principal import ExecutionPrincipal
    from src.profiles.capabilities import CapabilityPolicy

    principal = ExecutionPrincipal.service("knowledge-extraction")
    with pytest.raises(RecordError, match="record.forbidden"):
        await service.propose(
            snapshot=snapshot(), idempotency_key="denied", principal=principal, project_id="p"
        )
    narrowed = replace(
        principal,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(aq_commands=["knowledge_propose"]),
    )
    result = await service.propose(
        snapshot=snapshot(), idempotency_key="service", principal=narrowed, project_id="p"
    )
    assert result["state"] == "pending"
