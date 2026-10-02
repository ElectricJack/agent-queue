"""Transactional admission fences for the shipped object experiment formulas."""

from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import func, select, update

from src.commands.contracts.object_loop import ObjectLoopStartArgs
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import doc_review_revisions, doc_reviews, object_loops, task_metadata, tasks
from src.object_loop.contracts import Artifact, reject_nonfinite

PROPOSAL_ID = "rev-amber-zenith"
PROPOSAL_REVISION = 2


def start_from_vars(variables: dict, *, project_id: str, epic_task_id: str) -> dict:
    """Decode the whole typed packet; project and epic are server-derived."""
    payload = json.loads(variables["start"])
    if not isinstance(payload, dict):
        raise ValueError("start must be a JSON object")
    if {"project_id", "epic_task_id"} & payload.keys():
        raise ValueError("start must not supply project_id or epic_task_id")
    request = ObjectLoopStartArgs.model_validate(
        {**payload, "project_id": project_id, "epic_task_id": epic_task_id}
    )
    if request.object_id != variables["object_id"]:
        raise ValueError("formula object_id differs from start packet")
    return request.model_dump()


async def proposal_valid(conn, project_id: str, digest: str) -> bool:
    return bool((await conn.execute(
        select(doc_reviews.c.id).join(
            doc_review_revisions, doc_review_revisions.c.review_id == doc_reviews.c.id,
        ).where(
            doc_reviews.c.id == PROPOSAL_ID,
            doc_reviews.c.project_id == project_id,
            doc_reviews.c.kind == "spec",
            doc_reviews.c.state == "approved",
            doc_reviews.c.current_revision == PROPOSAL_REVISION,
            doc_reviews.c.decided_at.is_not(None),
            doc_review_revisions.c.revision == PROPOSAL_REVISION,
            doc_review_revisions.c.content_sha256 == digest,
        )
    )).first())


async def guard_formula(db, conn, plan, provenance, *, dry_run: bool = False) -> None:
    """Runs inside graph creation, before nodes can be routed or claimed.

    Reserved formula names cannot be shadowed into an unguarded recipe. This
    only narrows publication and admission; it grants no decision authority.
    """
    if provenance is None or provenance.name not in {"object", "variation"}:
        return
    from src.commands.object_loop_commands import _review_verdict

    variables = provenance.vars
    try:
        object_id = variables["object_id"]
        await conn.execute(select(func.pg_advisory_xact_lock(
            func.hashtext(f"object-formula:{plan.project_id}:{object_id}")
        )))
        loop = (await conn.execute(select(object_loops).where(
            object_loops.c.object_id == object_id,
            object_loops.c.project_id == plan.project_id,
        ).with_for_update())).mappings().first()
        parent = (await conn.execute(select(tasks).where(
            tasks.c.id == plan.parent_id,
        ))).mappings().first()
        if parent is None and dry_run and plan.parent_row:
            parent = SimpleNamespace(parent_task_id=plan.container_parent_id)
        if parent is None:
            raise ValueError("formula container does not exist")
        if provenance.name == "object":
            if parent.parent_task_id or loop:
                raise ValueError("object requires a new root without an existing loop")
            start_from_vars(variables, project_id=plan.project_id, epic_task_id=plan.parent_id)
            if not await proposal_valid(conn, plan.project_id, variables["proposal_sha256"]):
                raise ValueError("rev-amber-zenith revision 2 is not approved at this hash")
            duplicate = (await conn.execute(select(task_metadata.c.task_id).join(
                tasks, tasks.c.id == task_metadata.c.task_id,
            ).where(
                task_metadata.c.key == "object_formula_id",
                task_metadata.c.value == json.dumps(object_id),
                tasks.c.project_id == plan.project_id,
                tasks.c.id != plan.parent_id,
            ))).first()
            if duplicate:
                raise ValueError("object formula already cooked for this object_id")
            if dry_run:
                return
            await db._upsert_meta(plan.parent_id, "object_formula_id", object_id, conn=conn)
            gate_id, _ = await db.create_gate(
                plan.project_id, "event", f"Bootstrap object {object_id}",
                await_id=f"object:{object_id}:started", conn=conn,
            )
            purpose = "bootstrap"
        else:
            if (not loop or parent.parent_task_id != loop.epic_task_id
                    or loop.state["status"] != "active"):
                raise ValueError("variation requires a suite directly under an active object root")
            if (not loop.state.get("checkpoint") or not await _review_verdict(conn, loop.state)
                    or variables["candidate_sha256"] != loop.state["incumbent_sha256"]
                    or variables["render_profile_sha256"] != loop.state["render_profile_sha256"]):
                raise ValueError("variation requires the exact approved candidate and render profile")
            for name in ("seeds_a", "seeds_b"):
                seeds = json.loads(variables[name])
                if (not isinstance(seeds, list) or len(seeds) != 5
                        or any(type(seed) is not int for seed in seeds)):
                    raise ValueError("each seed bundle must contain five integer seeds")
            if len(set(json.loads(variables["seeds_a"]) + json.loads(variables["seeds_b"]))) != 10:
                raise ValueError("variation requires ten distinct fresh seeds")
            presets = json.loads(variables["presets"])
            if not isinstance(presets, dict) or not 1 <= len(presets) <= 16:
                raise ValueError("variation requires bounded parameter presets")
            reject_nonfinite(presets)
            if len(variables["presets"]) > 16384:
                raise ValueError("variation presets exceed the 16 KiB bound")
            Artifact(uri=variables["artifact_uri"], sha256=variables["candidate_sha256"])
            if (await conn.execute(select(task_metadata.c.value).where(
                task_metadata.c.task_id == plan.parent_id,
                task_metadata.c.key == "object_variation_cooked",
            ))).first():
                raise ValueError("variation suite is already cooked; inspect its existing children")
            if (await conn.execute(select(tasks.c.id).join(
                task_metadata, task_metadata.c.task_id == tasks.c.id,
            ).where(
                tasks.c.parent_task_id == loop.epic_task_id,
                task_metadata.c.key == "object_variation_cooked",
            ))).first():
                raise ValueError("object final-suite reserve already allocated to a variation suite")
            if dry_run:
                return
            await db._upsert_meta(plan.parent_id, "object_variation_cooked", True, conn=conn)
            gate_id = loop.state["checkpoint"]["gate_id"]
            purpose = "variation"
        for task_id in [plan.parent_id, *plan.task_ids]:
            await db._upsert_meta(task_id, "object_experiment", {
                "object_id": object_id, "purpose": purpose, "publish_source": False,
            }, conn=conn)
            if purpose == "variation":
                await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                    created_by_kind="object_loop", created_by_id=object_id,
                ))
        await db.attach_gate_waiters(gate_id, plan.task_ids, conn=conn)
    except (KeyError, TypeError, ValueError) as exc:
        raise HierarchyError("object_formula", str(exc)) from exc
