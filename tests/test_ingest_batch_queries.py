"""Held ingestion batch queries (implementation spec 2026-10-14 P1 L1.1).

Batch rows, membership and holds, the revision fence over a digest computed
from live rows, receipts, release, and design lineage resolved through the
implementation document's author-task ``spec_ingest`` stamp.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from src.database.queries import ingest_batch_queries as q
from src.database.queries.proposal_queries import insert_proposal
from src.database.tables import task_metadata, task_proposals, tasks
from src.models import Project, Task, TaskStatus
from src.reviews.service import ReviewHooks, ReviewService
from src.task_graph.batch_digest import compute_structure_digest

PROJECT = "p"
OPERATOR = "human:local-operator"
DOC = "# Doc\n\nIntro paragraph.\n\n## Scope\n\nBody.\n"


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("ingest_batch_queries.db")
    await database.create_project(Project(id=PROJECT, name="p"))
    yield database


@pytest.fixture
def svc(db, tmp_path):
    async def emit(event_type, payload):
        pass

    async def resolve_gate(gate_id, resolved_by, resolution):
        return await db.resolve_gate(gate_id, resolved_by=resolved_by, resolution=resolution)

    async def ignore(*args):
        pass

    return ReviewService(db, tmp_path, ReviewHooks(
        emit=emit, resolve_gate=resolve_gate, changes_requested=ignore, file_feedback=ignore,
    ))


async def mktask(db, tid, *, parent=None, title=None):
    await db.create_task(Task(
        id=tid, project_id=PROJECT, title=title or f"title {tid}", description=f"do {tid}",
        status=TaskStatus.READY, parent_task_id=parent,
    ))
    return tid


async def submit(svc, *, content, author, title="Spec"):
    return await svc.submit(
        project_id=PROJECT, author_task_id=author, kind="spec", title=title, content=content,
        submitted_by="session:worker",
    )


async def approve(svc, review_id):
    args = {"review_id": review_id, "revision": 1, "decision": "approve", "note": "ok",
            "decided_by": OPERATOR}
    result = await svc.decide(**args)
    if result.get("error_code") == "ask.preview_required":
        result = await svc.decide(**args, preview_id=result["preview"]["id"],
                                  expected=result["expected"])
    return result


async def stamp(db, task_id, decision_id):
    async with db.immediate() as conn:
        await conn.execute(task_metadata.insert().values(
            task_id=task_id, key=q.SPEC_INGEST_META,
            value=json.dumps({"decision_id": decision_id, "spec_kind": "design"}),
        ))


async def resolve(db, review_id):
    async with db.immediate() as conn:
        return await q.resolve_design_lineage(conn, review_id)


async def design_then_implementation(db, svc, *, design_kind="design"):
    """An approved design whose ingestion task authors an implementation spec."""
    design = await submit(svc, content=f"---\nspec_kind: {design_kind}\n---\n{DOC}",
                          author="design-author", title="Design")
    decision = await approve(svc, design["review_id"])
    author = await mktask(db, "impl-author")
    await stamp(db, author, decision["decision_id"])
    implementation = await submit(
        svc, content=f"---\nspec_kind: implementation\n---\n{DOC}\nPlan.\n",
        author=author, title="Implementation",
    )
    return design, decision, implementation


async def test_lineage_resolved(db, svc):
    design, decision, implementation = await design_then_implementation(db, svc)
    lineage = await resolve(db, implementation["review_id"])
    revision = await db.get_review_revision(design["review_id"], 1)
    assert lineage == {
        "lineage_kind": "resolved",
        "decision_id": decision["decision_id"],
        "design_review_id": design["review_id"],
        "design_revision": 1,
        "design_content_sha256": revision["content_sha256"],
    }


async def test_lineage_none(db, svc):
    plain = await submit(svc, content=DOC, author=await mktask(db, "unstamped"))
    assert (await resolve(db, plain["review_id"]))["lineage_kind"] == "none"
    assert (await resolve(db, plain["review_id"]))["reason"] == "no_spec_ingest"
    assert (await resolve(db, "rev-missing"))["reason"] == "no_author_task"

    _, _, implementation = await design_then_implementation(db, svc, design_kind="implementation")
    lineage = await resolve(db, implementation["review_id"])
    assert lineage["lineage_kind"] == "none"
    assert lineage["reason"] == "not_design"
    assert lineage["design_review_id"] is lineage["design_revision"] is None


# --- Batches over live rows ---------------------------------------------------


NO_LINEAGE = q._no_lineage("no_spec_ingest")


async def new_batch(db, *, review_id="rev-1", revision=1, sha="sha-1"):
    proposal = await insert_proposal(db, project_id=PROJECT, source="spec", payload={},
                                     status="held")
    async with db.immediate() as conn:
        return await q.create_batch(
            conn, batch_id=proposal, project_id=PROJECT, source_review_id=review_id,
            source_revision=revision, source_content_sha256=sha, decision_id="dec-1",
            lineage=NO_LINEAGE, idempotency_key=f"ingest:{review_id}:{revision}",
        )


async def join(db, batch_id, task_id, key, *, role="leaf", revision=1, **fidelity):
    async with db.immediate() as conn:
        return await q.add_member(
            conn, batch_id, task_id=task_id, member_key=key, role=role,
            section_anchor=f"#{key}", primary_outcome=f"outcome {key}",
            created_revision=revision, **fidelity,
        )


async def held_by(db, task_id):
    async with db.immediate() as conn:
        return (await conn.execute(
            select(tasks.c.held_batch_id).where(tasks.c.id == task_id)
        )).scalar_one()


async def two_member_batch(db):
    batch_id = (await new_batch(db))["batch"]["batch_id"]
    await mktask(db, "root")
    await mktask(db, "leaf-a", parent="root")
    await join(db, batch_id, "root", "root", role="container")
    await join(db, batch_id, "leaf-a", "a", acceptance=["passes"], size="S")
    async with db.immediate() as conn:
        digest = await q.store_live_digest(conn, batch_id)
    return batch_id, digest


async def test_create_replays_the_same_source(db):
    first = await new_batch(db)
    assert first["success"] and not first["replayed"]
    assert first["batch"]["state"] == "held"
    assert first["batch"]["structure_revision"] == 1
    assert first["batch"]["structure_digest"] == compute_structure_digest([], [])
    again = await new_batch(db)
    assert again["replayed"]
    assert again["batch"]["batch_id"] == first["batch"]["batch_id"]


@pytest.mark.parametrize(("older_state", "newer_state", "retire_newer", "preferred"), [
    ("held", "released", False, "older"),
    ("released", "held", False, "newer"),
    ("released", "released", False, "newer"),
    ("released", "held", True, "older"),
])
async def test_joined_batch_summary_preserves_membership_preference(
    db, older_state, newer_state, retire_newer, preferred,
):
    for tid in ("member", "ordinary", "outside"):
        await mktask(db, tid)
    batch_ids = {}
    for name, state, timestamp in (("older", older_state, 1), ("newer", newer_state, 2)):
        bid = (await new_batch(db, review_id=name))["batch"]["batch_id"]
        batch_ids[name] = bid
        await join(db, bid, "member", "leaf")
        await join(db, bid, "outside", "outside")
        async with db.immediate() as conn:
            await conn.execute(q.ingest_batches.update().where(
                q.ingest_batches.c.batch_id == bid,
            ).values(created_at=timestamp))
            if state == "released":
                digest = await q.store_live_digest(conn, bid)
                assert (await q.release(
                    conn, bid, expected_revision=1, expected_digest=digest,
                    actor="supervisor", release_receipt={"task_ids": ["member", "outside"]},
                ))["success"]
    async with db.immediate() as conn:
        if retire_newer:
            assert (await q.retire_member(
                conn, batch_ids["newer"], "member", reason="superseded", retired_revision=2,
            ))["success"]
        visible_ids = select(tasks.c.id).where(tasks.c.id.in_(["member", "ordinary"]))
        joined = q.task_batch_summary_rows(visible_ids)
        rows = (await conn.execute(select(joined))).mappings().all()
        assert [row["task_id"] for row in rows] == ["member"]
        actual = q.batch_summary(rows[0]["ingest_batch_summary"])
        assert actual == await q.task_batch_summary(conn, "member")
        assert actual["batch_id"] == batch_ids[preferred]


async def test_members_hold_tasks_and_digest_reads_live_rows(db):
    batch_id, digest = await two_member_batch(db)
    assert await held_by(db, "root") == await held_by(db, "leaf-a") == batch_id
    async with db.immediate() as conn:
        assert await q.member_map(conn, batch_id) == {"a": "leaf-a", "root": "root"}
        members, edges = await q.hydrate_structure(conn, batch_id)
        assert [m["acceptance"] for m in members] == [["passes"], None]
        assert edges == []
        assert (await q.get_batch(conn, batch_id))["structure_digest"] == digest
        await conn.execute(tasks.update().where(tasks.c.id == "leaf-a").values(title="renamed"))
        assert await q.live_digest(conn, batch_id) == digest  # retitles are nonstructural
        await conn.execute(tasks.update().where(tasks.c.id == "leaf-a").values(description="new"))
        assert await q.live_digest(conn, batch_id) != digest


async def test_bump_revision_fences_stale_callers(db):
    batch_id, digest = await two_member_batch(db)
    async with db.immediate() as conn:
        await conn.execute(tasks.update().where(tasks.c.id == "leaf-a").values(description="new"))
        bumped = await q.bump_revision(conn, batch_id, 1)
    assert bumped["success"] and bumped["structure_revision"] == 2
    assert bumped["structure_digest"] != digest
    async with db.immediate() as conn:
        stale = await q.bump_revision(conn, batch_id, 1)
    assert stale["error_code"] == "stale_revision"
    assert stale["structure_revision"] == 2


async def test_retired_member_leaves_the_digest_and_keeps_its_hold(db):
    batch_id, digest = await two_member_batch(db)
    async with db.immediate() as conn:
        assert (await q.retire_member(conn, batch_id, "leaf-a", reason="obsolete",
                                      retired_revision=2))["success"]
        assert (await q.retire_member(conn, batch_id, "leaf-a", reason="again",
                                      retired_revision=2))["error_code"] == "membership"
        assert await q.member_map(conn, batch_id) == {"root": "root"}
        assert await q.live_digest(conn, batch_id) != digest
        retired = await q.members(conn, batch_id, state="retired")
    assert [(m["task_id"], m["retired_revision"]) for m in retired] == [("leaf-a", 2)]
    assert await held_by(db, "leaf-a") == batch_id


async def receipt(conn, batch_id, revision, digest, *, passing=True, findings=()):
    return await q.record_receipt(
        conn, batch_id, structure_revision=revision, structure_digest=digest,
        reviewer_task_id="reviewer", reviewer_session_id="sess-1", passing=passing,
        section_verdicts={"#a": "covered"}, reverse_coverage_verdict="pass",
        walkthrough_verdict="pass", structural_findings=list(findings),
    )


async def test_receipts_bind_to_the_current_revision(db):
    batch_id, digest = await two_member_batch(db)
    async with db.immediate() as conn:
        assert (await receipt(conn, batch_id, 1, "other"))["error_code"] == "stale_revision"
        first = await receipt(conn, batch_id, 1, digest)
        assert first["success"] and not first["replayed"]
        assert first["receipt"]["section_verdicts"] == {"#a": "covered"}
        assert (await receipt(conn, batch_id, 1, digest))["replayed"]
        assert (await receipt(conn, batch_id, 1, digest, findings=["x"]))["error_code"] == (
            "receipt_exists")
        assert (await receipt(conn, batch_id, 1, digest, passing=False))["success"]


async def test_set_state_is_fenced_and_cannot_release(db):
    batch_id, _ = await two_member_batch(db)
    async with db.immediate() as conn:
        moved = await q.set_state(conn, batch_id, "reviewing", from_states=["held"])
        assert moved["previous_state"] == "held"
        conflict = await q.set_state(conn, batch_id, "needs_scope", from_states=["held"])
        assert conflict["error_code"] == "batch_state_conflict"
        with pytest.raises(ValueError):
            await q.set_state(conn, batch_id, "released")
        bumped = await q.bump_revision(conn, batch_id, 1)
    assert bumped["state"] == "held"


async def test_release_clears_holds_once_and_refuses_stale_digest(db):
    batch_id, digest = await two_member_batch(db)
    async with db.immediate() as conn:
        stale = await q.release(conn, batch_id, expected_revision=1, expected_digest="old",
                                actor=OPERATOR, release_receipt={"ok": True})
    assert stale["error_code"] == "stale_revision"
    assert await held_by(db, "leaf-a") == batch_id
    async with db.immediate() as conn:
        released = await q.release(conn, batch_id, expected_revision=1, expected_digest=digest,
                                   actor=OPERATOR, release_receipt={"ok": True})
    assert released["success"] and released["released_task_ids"] == ["leaf-a", "root"]
    assert await held_by(db, "root") is None
    async with db.immediate() as conn:
        batch = await q.get_batch(conn, batch_id)
        status = (await conn.execute(select(task_proposals.c.status).where(
            task_proposals.c.id == batch_id))).scalar_one()
        replay = await q.release(conn, batch_id, expected_revision=1, expected_digest=digest,
                                 actor=OPERATOR, release_receipt={"ok": True})
        bump = await q.bump_revision(conn, batch_id, 1)
    assert (batch["state"], batch["release_revision"], batch["release_digest"]) == (
        "released", 1, digest)
    assert status == "committed"
    assert replay["already_released"] and replay["release_receipt"] == {"ok": True}
    assert bump["error_code"] == "batch_not_held"
