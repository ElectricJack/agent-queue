"""Task-proposal commands — staged spec ingestion (design §8, Phase 6).

Registers four CommandHandler commands:

- ``task_batch_propose`` — validate + persist a proposed batch, emit
  ``proposal.ready``.
- ``task_batch_update`` — replace the payload while status is draft/ready
  and no approval gate awaits the proposal yet.
- ``task_batch_discard`` — soft-drop the proposal.
- ``task_batch_commit`` — atomically materialize the batch into the live
  work graph, only under a human gate that approves this exact proposal.
  A replay of an already committed proposal returns the original receipt.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import and_, select, update

from src.database.queries import proposal_queries
from src.database.tables import TASK_DEP_TYPES, task_metadata, task_proposals, tasks
from src.models import DepType, Task, TaskStatus

logger = logging.getLogger(__name__)

#: The ``resolution`` values that approve a proposal's human gate.  The
#: dashboard's proposal pane and activity drawer write ``approved``; the task
#: pane writes ``approve``.  The shipped ``default-pipeline`` filters its
#: commit rule on exactly this set, so the event filter and the command agree.
APPROVAL_RESOLUTIONS = frozenset({"approve", "approved"})

#: Task metadata key linking each materialised task to its proposal, so a
#: replayed commit can report the original receipt.
PROPOSAL_ID_META = "proposal_id"


class TaskProposalCommandsMixin:
    """Mount on CommandHandler alongside the other command mixins."""

    # ----- helpers -------------------------------------------------------

    @staticmethod
    def _validate_shape(
        tasks_in: list[dict], edges_in: list[dict]
    ) -> str | None:
        if not isinstance(tasks_in, list) or not tasks_in:
            return "tasks must be a non-empty list"
        temp_ids: set[str] = set()
        for i, t in enumerate(tasks_in):
            if not isinstance(t, dict):
                return f"tasks[{i}] must be an object"
            tid = t.get("tempId")
            if not tid or not isinstance(tid, str):
                return f"tasks[{i}].tempId is required (string)"
            if tid in temp_ids:
                return f"duplicate tempId '{tid}'"
            temp_ids.add(tid)
            if not t.get("title"):
                return f"tasks[{i}].title is required"
            if "description" not in t:
                return f"tasks[{i}].description is required"
        if not isinstance(edges_in, list):
            return "edges must be a list"
        for i, e in enumerate(edges_in):
            if not isinstance(e, dict):
                return f"edges[{i}] must be an object"
            if not e.get("from") or not e.get("to"):
                return f"edges[{i}] requires 'from' and 'to'"
            dt = e.get("dep_type", "blocks")
            if dt not in TASK_DEP_TYPES:
                return (
                    f"edges[{i}].dep_type '{dt}' not in {list(TASK_DEP_TYPES)}"
                )
        return None

    async def _validate_existing_refs(
        self,
        project_id: str,
        tasks_in: list[dict],
        edges_in: list[dict],
    ) -> str | None:
        temp_ids = {t["tempId"] for t in tasks_in}
        referenced: set[str] = set()
        for e in edges_in:
            for endpoint in (e["from"], e["to"]):
                if endpoint not in temp_ids:
                    referenced.add(endpoint)
        if not referenced:
            return None
        async with self.db._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(tasks.c.id).where(
                        tasks.c.project_id == project_id,
                        tasks.c.id.in_(referenced),
                    )
                )
            ).all()
        found = {r[0] for r in rows}
        missing = referenced - found
        if missing:
            return f"unknown existing task ids: {sorted(missing)}"
        return None

    async def _emit_proposal_event(self, event_type: str, payload: dict) -> None:
        bus = getattr(self.orchestrator, "bus", None)
        if bus is None:
            return
        try:
            await bus.emit(event_type, payload)
        except Exception:  # pragma: no cover — defensive
            logger.debug("bus.emit %s failed", event_type, exc_info=True)

    # ----- commands ------------------------------------------------------

    async def _cmd_task_batch_propose(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._active_project_id
        source = args.get("source")
        tasks_in = args.get("tasks") or []
        edges_in = args.get("edges") or []

        if not project_id:
            return {"success": False, "error": "project_id is required"}
        if not source:
            return {"success": False, "error": "source is required"}

        shape_err = self._validate_shape(tasks_in, edges_in)
        if shape_err:
            return {"success": False, "error": shape_err}

        ref_err = await self._validate_existing_refs(project_id, tasks_in, edges_in)
        if ref_err:
            return {"success": False, "error": ref_err}

        existing = await proposal_queries.existing_graph_edges(self.db, project_id)
        cycles = proposal_queries.detect_cycles(existing, tasks_in, edges_in)
        if cycles:
            return {
                "success": False,
                "error": f"proposal introduces cycle(s): {cycles}",
            }

        payload = {"tasks": tasks_in, "edges": edges_in}
        proposal_id = await proposal_queries.insert_proposal(
            self.db, project_id=project_id, source=source, payload=payload
        )
        await proposal_queries.update_proposal(
            self.db, proposal_id, status="ready"
        )

        await self._emit_proposal_event(
            "proposal.ready",
            {"project_id": project_id, "proposal_id": proposal_id},
        )
        return {"success": True, "proposal_id": proposal_id}

    async def _cmd_task_batch_update(self, args: dict) -> dict:
        proposal_id = args.get("proposal_id")
        payload = args.get("payload") or {}
        if not proposal_id:
            return {"success": False, "error": "proposal_id is required"}
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {
                "success": False,
                "error": f"proposal '{proposal_id}' not found",
            }
        if row["status"] not in ("draft", "ready"):
            return {
                "success": False,
                "error": f"cannot update proposal in status '{row['status']}'",
            }
        tasks_in = payload.get("tasks") or []
        edges_in = payload.get("edges") or []
        shape_err = self._validate_shape(tasks_in, edges_in)
        if shape_err:
            return {"success": False, "error": shape_err}
        ref_err = await self._validate_existing_refs(
            row["project_id"], tasks_in, edges_in
        )
        if ref_err:
            return {"success": False, "error": ref_err}
        existing = await proposal_queries.existing_graph_edges(
            self.db, row["project_id"]
        )
        cycles = proposal_queries.detect_cycles(existing, tasks_in, edges_in)
        if cycles:
            return {"success": False, "error": f"cycle(s): {cycles}"}

        if not await proposal_queries.update_ungated_payload(
            self.db, proposal_id, project_id=row["project_id"], payload=payload
        ):
            current = await proposal_queries.get_proposal(self.db, proposal_id)
            status = current["status"] if current else "missing"
            if status not in ("draft", "ready"):
                return {
                    "success": False,
                    "error": f"cannot update proposal in status '{status}'",
                }
            return {
                "success": False,
                "error": (
                    f"proposal '{proposal_id}' already has an approval gate, so its "
                    "payload is frozen to the revision that gate asks about; discard "
                    "it and propose the revised batch instead"
                ),
            }
        return {"success": True}

    async def _cmd_task_batch_discard(self, args: dict) -> dict:
        proposal_id = args.get("proposal_id")
        if not proposal_id:
            return {"success": False, "error": "proposal_id is required"}
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {
                "success": False,
                "error": f"proposal '{proposal_id}' not found",
            }
        if row["status"] in ("committed", "discarded"):
            return {"success": False, "error": f"already {row['status']}"}
        await proposal_queries.update_proposal(
            self.db, proposal_id, status="discarded"
        )
        await self._emit_proposal_event(
            "proposal.status_changed",
            {
                "project_id": row["project_id"],
                "proposal_id": proposal_id,
                "status": "discarded",
            },
        )
        return {"success": True}

    async def _proposal_approval_error(
        self, row: dict, *, gate_id: str | None, project_id: str | None
    ) -> str | None:
        """Why no human decision approves this exact proposal, or ``None``.

        Materialisation is the authority boundary.  An event filter narrows
        which resolutions reach the pipeline's commit rule, but any caller can
        invoke ``task_batch_commit`` directly, so the approving gate is re-read
        here: a ``human`` gate in the proposal's own project, awaiting this
        proposal id, resolved with one of :data:`APPROVAL_RESOLUTIONS`.  With
        no ``gate_id`` the newest such gate is the decision of record.  The
        payload is frozen once that gate exists (``task_batch_update``), so the
        gate approves exactly the revision being committed.
        """
        proposal_id, owner = row["id"], row["project_id"]
        if project_id and project_id != owner:
            return f"proposal '{proposal_id}' belongs to project '{owner}', not '{project_id}'"
        if gate_id:
            gate = await self.db.get_gate(str(gate_id))
            if gate is None:
                return f"approval gate '{gate_id}' not found"
        else:
            candidates = await self.db.list_gates(
                project_id=owner, gate_type="human", await_id=proposal_id
            )
            if not candidates:
                return f"proposal '{proposal_id}' has no human approval gate"
            gate = candidates[0]
        label = f"gate '{gate['id']}'"
        if gate["gate_type"] != "human":
            return f"{label} is a '{gate['gate_type']}' gate, not a human approval"
        if gate["project_id"] != owner:
            return f"{label} belongs to project '{gate['project_id']}', not '{owner}'"
        if gate["await_id"] != proposal_id:
            return f"{label} awaits '{gate['await_id']}', not proposal '{proposal_id}'"
        if gate["status"] != "resolved":
            return f"{label} is {gate['status']}, not resolved"
        if gate["resolution"] not in APPROVAL_RESOLUTIONS:
            return f"{label} was resolved {gate['resolution']!r}, which is not an approval"
        return None

    async def _proposal_task_ids(self, proposal_id: str) -> list[str]:
        """The tasks a committed proposal materialised, oldest first."""
        async with self.db._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(tasks.c.id)
                    .join(task_metadata, task_metadata.c.task_id == tasks.c.id)
                    .where(
                        task_metadata.c.key == PROPOSAL_ID_META,
                        task_metadata.c.value == json.dumps(proposal_id),
                    )
                    .order_by(tasks.c.created_at, tasks.c.id)
                )
            ).all()
        return [r[0] for r in rows]

    async def _cmd_task_batch_commit(self, args: dict) -> dict:
        """Atomically materialise an approved proposal into the live work graph.

        Approval: ``_proposal_approval_error`` must find a human gate that
        approves this exact project and proposal (``gate_id``/``project_id``
        pin it when the caller knows them).  Anything else — no gate, an open,
        expired or rejected one, another project's or another proposal's —
        returns ``not_approved`` and creates nothing.

        Replay: a second approved call for an already committed proposal is
        not an error.  It returns ``already_committed`` with the original
        task ids, so a re-delivered ``gate.resolved`` yields one graph.

        Concurrency: the "claim" is a single conditional UPDATE that
        flips ``ready`` → ``committed`` and returns rowcount.  Only one
        caller can win; the loser sees rowcount==0 and aborts BEFORE
        creating any tasks — this closes the double-commit race the
        prior check-then-write pattern had.

        Failure atomicity: created task ids AND created dep edges are
        tracked and unwound in reverse order on any exception, so no
        orphan tasks or leaked edges survive a partial failure.  A
        single DB transaction across create_task / add_dependency
        would be nicer but those helpers open their own sessions.  On
        rollback the proposal row is flipped back to ``ready`` so the
        caller can retry after fixing the underlying issue.
        """
        proposal_id = args.get("proposal_id")
        if not proposal_id:
            return {"success": False, "error": "proposal_id is required"}
        row = await proposal_queries.get_proposal(self.db, proposal_id)
        if row is None:
            return {
                "success": False,
                "error": f"proposal '{proposal_id}' not found",
            }
        if row["status"] == "discarded":
            return {"success": False, "error": "proposal was discarded"}
        approval_error = await self._proposal_approval_error(
            row, gate_id=args.get("gate_id"), project_id=args.get("project_id")
        )
        if approval_error:
            return {"success": False, "not_approved": True, "error": approval_error}
        if row["status"] == "committed":
            return {
                "success": True,
                "already_committed": True,
                "task_ids": await self._proposal_task_ids(proposal_id),
            }

        project_id: str = row["project_id"]
        payload: dict[str, Any] = row["payload"]
        tasks_in = payload["tasks"]
        edges_in = payload["edges"]

        # Re-validate against the current DB state (source of truth is the
        # stored payload, but referenced task ids may have vanished).
        shape_err = self._validate_shape(tasks_in, edges_in)
        if shape_err:
            return {"success": False, "error": shape_err}
        ref_err = await self._validate_existing_refs(
            project_id, tasks_in, edges_in
        )
        if ref_err:
            return {"success": False, "error": ref_err}
        existing = await proposal_queries.existing_graph_edges(
            self.db, project_id
        )
        cycles = proposal_queries.detect_cycles(existing, tasks_in, edges_in)
        if cycles:
            return {"success": False, "error": f"cycle(s): {cycles}"}

        project = await self.db.get_project(project_id)
        if project is not None and project.default_profile_id:
            default_profile = await self.db.get_profile(project.default_profile_id)
            if default_profile is None:
                return {
                    "success": False,
                    "error": (
                        f"project default profile '{project.default_profile_id}' is not defined; "
                        "configure an eligible worker default before materializing the batch"
                    ),
                }
            if error := self._task_execution_profile_error(default_profile):
                return {"success": False, "error": f"project default is invalid: {error}"}
        # Batch tasks take the same route fields as direct and graph-created
        # work.  Validate explicit intent before claiming the proposal so an
        # invalid profile/class is an actionable admission refusal, not a
        # partially materialised graph.
        for spec in tasks_in:
            profile = None
            profile_id = spec.get("profile_id")
            if profile_id:
                profile = await self.db.get_profile(profile_id)
                if profile is None:
                    return {"success": False, "error": f"Profile '{profile_id}' not found"}
                if error := self._task_execution_profile_error(profile):
                    return {"success": False, "error": error}
            if error := self._validate_routing_class(spec.get("intelligence_class"), profile):
                return {"success": False, "error": error}
        if project is not None and project.hierarchical_integration_mode in {
            "hierarchy",
            "train",
        }:
            return await self._commit_hierarchical_proposal(
                proposal_id=proposal_id,
                project_id=project_id,
                source=row["source"],
                tasks_in=tasks_in,
                edges_in=edges_in,
            )

        # Claim: single conditional flip. Only one concurrent caller
        # wins this UPDATE; the loser sees rowcount==0 and aborts.
        now = time.time()
        async with self.db._engine.begin() as conn:
            claim = await conn.execute(
                update(task_proposals)
                .where(
                    and_(
                        task_proposals.c.id == proposal_id,
                        task_proposals.c.status == "ready",
                    )
                )
                .values(status="committed", updated_at=now)
            )
        if claim.rowcount == 0:
            return {
                "success": False,
                "error": (
                    "proposal not in 'ready' state or already claimed by "
                    "another commit"
                ),
            }

        created_ids: list[str] = []
        created_edges: list[tuple[str, str, str]] = []  # (from, to, dep_type)
        temp_to_real: dict[str, str] = {}
        try:
            for t in tasks_in:
                new_id = await _create_one_task(
                    self, project_id, t, source=row["source"]
                )
                created_ids.append(new_id)
                temp_to_real[t["tempId"]] = new_id
                await self.db.set_task_meta(new_id, PROPOSAL_ID_META, proposal_id)

            for e in edges_in:
                frm = temp_to_real.get(e["from"], e["from"])
                to = temp_to_real.get(e["to"], e["to"])
                dep_res = await self.execute(
                    "add_dependency",
                    {
                        "task_id": frm,
                        "depends_on_task_id": to,
                        "dep_type": e["dep_type"],
                    },
                )
                if not dep_res.get("success", True) and "error" in dep_res:
                    raise RuntimeError(dep_res["error"])
                created_edges.append((frm, to, e["dep_type"]))
        except Exception as exc:
            logger.exception("task_batch_commit failed, rolling back: %s", exc)
            # Roll back edges first (may reference tasks about to be deleted).
            for frm, to, dep_type in reversed(created_edges):
                try:
                    await self.db.remove_dependency(frm, to, dep_type)
                except Exception:
                    logger.exception(
                        "rollback: remove_dependency failed %s->%s (%s)",
                        frm, to, dep_type,
                    )
            for tid in reversed(created_ids):
                try:
                    await self.execute("delete_task", {"task_id": tid})
                except Exception:
                    logger.exception(
                        "rollback: delete_task failed for %s", tid
                    )
            # Release the claim so the caller can retry.
            try:
                await proposal_queries.update_proposal(
                    self.db, proposal_id, status="ready"
                )
            except Exception:
                logger.exception(
                    "rollback: could not release proposal %s claim",
                    proposal_id,
                )
            return {"success": False, "error": f"commit failed: {exc}"}

        await self._emit_proposal_event(
            "proposal.status_changed",
            {
                "project_id": project_id,
                "proposal_id": proposal_id,
                "status": "committed",
            },
        )
        return {"success": True, "task_ids": created_ids}

    async def _commit_hierarchical_proposal(
        self,
        *,
        proposal_id: str,
        project_id: str,
        source: str,
        tasks_in: list[dict],
        edges_in: list[dict],
    ) -> dict:
        """Claim and materialize an enabled-project proposal in one transaction."""
        parent_by_temp = {
            edge["from"]: edge["to"]
            for edge in edges_in
            if edge.get("dep_type", DepType.BLOCKS.value) == DepType.PARENT_CHILD.value
        }
        specs = {spec["tempId"]: spec for spec in tasks_in}
        unknown_parents = {
            parent for parent in parent_by_temp.values() if parent not in specs
        }
        if unknown_parents:
            return {
                "success": False,
                "error": "hierarchical proposal parents must be in the same batch: "
                + ", ".join(sorted(unknown_parents)),
            }
        children_by_parent: dict[str, list[str]] = {}
        for child, parent in parent_by_temp.items():
            children_by_parent.setdefault(parent, []).append(child)

        from src.playbooks.routing import requires_routing_gate

        service = self._hierarchy_integration_service()
        routing_manager = getattr(self.orchestrator, "playbook_manager", None)

        def routing_policy(task) -> bool:
            return requires_routing_gate(
                routing_manager, task, {"parent_task_id": task.parent_task_id}
            )

        temp_to_real: dict[str, str] = {}
        routing_task_ids: list[str] = []
        try:
            async with self.db.immediate() as conn:
                claim = await conn.execute(
                    update(task_proposals)
                    .where(
                        and_(
                            task_proposals.c.id == proposal_id,
                            task_proposals.c.status == "ready",
                        )
                    )
                    .values(status="committed", updated_at=time.time())
                )
                if claim.rowcount != 1:
                    return {
                        "success": False,
                        "error": "proposal not in 'ready' state or already claimed by another commit",
                    }
                await self.db.lock_hierarchy_project(conn, project_id)

                pending = set(specs)
                while pending:
                    roots = sorted(temp_id for temp_id in pending if temp_id not in parent_by_temp)
                    ready_children = sorted(
                        temp_id
                        for temp_id in pending
                        if parent_by_temp.get(temp_id) in temp_to_real
                    )
                    if not roots and not ready_children:
                        raise RuntimeError("hierarchical proposal has an unresolved parent cycle")
                    for temp_id in roots:
                        task = self._proposal_task(project_id, specs[temp_id])
                        created = await service.file_root_on(
                            conn, task, routing_policy=routing_policy
                        )
                        temp_to_real[temp_id] = created["task_id"]
                        if created.get("gate_id"):
                            routing_task_ids.append(created["task_id"])
                        await self.db._upsert_meta(
                            created["task_id"], "proposal_source", source, conn=conn
                        )
                        await self.db._upsert_meta(
                            created["task_id"], PROPOSAL_ID_META, proposal_id, conn=conn
                        )
                        pending.remove(temp_id)
                    grouped: dict[str, list[str]] = {}
                    for temp_id in ready_children:
                        grouped.setdefault(parent_by_temp[temp_id], []).append(temp_id)
                    for parent_temp, child_temps in grouped.items():
                        task_models = [
                            self._proposal_task(project_id, specs[temp_id])
                            for temp_id in child_temps
                        ]
                        created = await service.file_prepared_children_on(
                            conn,
                            temp_to_real[parent_temp],
                            task_models,
                            routing_policy=routing_policy,
                        )
                        for temp_id, item in zip(child_temps, created, strict=True):
                            temp_to_real[temp_id] = item["task_id"]
                            if item.get("gate_id"):
                                routing_task_ids.append(item["task_id"])
                            await self.db._upsert_meta(
                                item["task_id"], "proposal_source", source, conn=conn
                            )
                            await self.db._upsert_meta(
                                item["task_id"], PROPOSAL_ID_META, proposal_id, conn=conn
                            )
                            pending.remove(temp_id)

                for edge in edges_in:
                    dep_type = edge.get("dep_type", DepType.BLOCKS.value)
                    if dep_type == DepType.PARENT_CHILD.value:
                        continue
                    await self.db.add_dependency(
                        temp_to_real.get(edge["from"], edge["from"]),
                        temp_to_real.get(edge["to"], edge["to"]),
                        dep_type,
                        conn=conn,
                    )
        except Exception as exc:
            logger.exception("hierarchical task_batch_commit failed: %s", exc)
            return {"success": False, "error": f"commit failed: {exc}"}

        await self._emit_proposal_event(
            "proposal.status_changed",
            {"project_id": project_id, "proposal_id": proposal_id, "status": "committed"},
        )
        for task_id in routing_task_ids:
            await self._emit_admitted_routing_gates(task_id)
            task = await self.db.get_task(task_id)
            if task is not None:
                await self.orchestrator._emit_task_event(
                    "task.created",
                    task,
                    parent_task_id=task.parent_task_id,
                    profile_id=task.profile_id,
                    created_by_kind=None,
                    created_by_id=None,
                )
        return {
            "success": True,
            "task_ids": [temp_to_real[spec["tempId"]] for spec in tasks_in],
        }

    @staticmethod
    def _proposal_task(project_id: str, spec: dict) -> Task:
        return Task(
            id="",
            project_id=project_id,
            title=spec["title"],
            description=spec.get("description", ""),
            priority=spec.get("priority", 100),
            deliverables=spec.get("deliverables", []),
            profile_id=spec.get("profile_id"),
            intelligence_class=spec.get("intelligence_class"),
            status=TaskStatus.DEFINED,
        )


async def _create_one_task(
    handler, project_id: str, spec: dict, source: str
) -> str:
    """Wrapper around create_task that stamps proposal provenance.

    Extracted as a module-level function so tests can monkeypatch it.
    """
    r = await handler.execute(
        "create_task",
        {
            "project_id": project_id,
            "title": spec["title"],
            "description": spec.get("description", ""),
            "priority": spec.get("priority", 100),
            "deliverables": spec.get("deliverables", []),
            "profile_id": spec.get("profile_id"),
            "intelligence_class": spec.get("intelligence_class"),
            "metadata": {"proposal_source": source},
        },
    )
    # ``create_task`` returns ``{"created": <task_id>, ...}`` on success
    # (or ``{"error": ...}`` on failure).  Normalise here.
    if r.get("error") or not r.get("created"):
        raise RuntimeError(r.get("error", "create_task returned no task_id"))
    return r["created"]
