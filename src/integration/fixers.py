"""One durable, task-free fixer per git-first batch (fast integration revision 4).

The batch branch is the workspace and publication boundary. A red observation
changes the input of the same session, never files another task or consumes a
claim. Session launch is registered before process creation; an uncertain launch
is retained for inspection rather than retried as a second agent.
"""

from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import integration_batch_members, integration_batches, messages, sessions, tasks
from src.git.manager import is_valid_git_oid
from src.integration.batches import candidate_ref
from src.integration.lock import BranchLock
from src.integration.repair import OrdinaryRepairService, ordinary_repair_id
from src.waits.sources import execute_batch_mutation, insert_message_with_source_event


class BatchFixerService:
    """Train allocation port; all mutations run inside CommandHandler.

    ``runtime`` implements launch, liveness and publication using registered
    named sessions. Its launch identity is committed before invoking it. Neither
    deadlines nor repeated observations launch or wake an agent. Exact green
    always settles, even after an exhausted budget or a stopped session.
    """

    task_free = True

    def __init__(self, db, runtime, *, clock=time.time, locks=None):
        self.db, self.runtime, self.clock = db, runtime, clock
        self.locks = locks or BranchLock(db, clock=clock)
        self.legacy = OrdinaryRepairService(db, clock=clock, locks=self.locks)

    async def _write(self, conn, batch_id, state):
        await execute_batch_mutation(conn, update(integration_batches).where(
            integration_batches.c.id == batch_id,
            integration_batches.c.fixer_state.is_distinct_from(state),
        ).values(fixer_state=state, updated_at=self.clock()))

    async def _message(self, conn, batch, state, key, body, *, supervisor=False):
        identity = hashlib.sha256(f"{batch['id']}:{key}".encode()).hexdigest()
        await insert_message_with_source_event(conn, insert(messages).values(
            id="msg-fixer-" + identity, project_id=batch["project_id"],
            from_kind="system", from_id="integration-train", to_kind="session",
            to_id=(f"supervisor-{batch['project_id']}" if supervisor else state["session_name"]),
            subject=f"Batch fixer: {batch['id']}", body=body,
            thread_id="batch-fixer-" + batch["id"], priority=100 if supervisor else 50,
            created_at=self.clock(), archive_after_inject=1, body_kind="integration_fixer",
        ).on_conflict_do_nothing(index_elements=[messages.c.id]))

    async def _block(self, conn, batch, state, reason):
        from src.integration.fixer_batches import defer_on

        if state["status"] == "green":
            return {"success": True, "outcome": "green"}
        # One incident per allocation, including races between launch and observation.
        if not state.get("blocker"):
            state["blocker"] = reason
            await self._message(conn, batch, state, "blocker", (
                f"Batch {batch['id']} fixer needs attention: {reason}. "
                f"Session {state['session_id']}, branch {state['target_ref']}, "
                f"last head {state['head_sha']}. Its checkout and input are retained. "
                "The lane is released with a finite revisit; no human hold or successor was "
                "created. Exact green still advances this batch."
            ), supervisor=True)
        await self._write(conn, batch["id"], state)
        await defer_on(conn, batch, now=self.clock(), reason=state["blocker"],
                       head_sha=state["head_sha"],
                       revisit_seconds=state.get("revisit_seconds", 300))
        return {"success": True, "outcome": "deferred", "reason": state["blocker"],
                "session_id": state["session_id"]}

    async def allocate(
        self, batch_id, *, target_ref, head_sha, authorize=None, held=False,
        policy=None, brief="", existing_only=False, green_sha=None,
        sync_default_branch=None, completion_blocker=None, failure_evidence=None, **unused,
    ):
        if not target_ref.startswith("refs/heads/") or not is_valid_git_oid(head_sha):
            raise ValueError("fixer requires a full branch ref and exact observed head")
        if green_sha == head_sha:
            await self.settle_green(batch_id, head_sha)
            return {"success": True, "outcome": "green"}
        evidence = None
        if failure_evidence is not None:
            evidence = await self.runtime.failure_input(batch_id, failure_evidence)
        if evidence and not existing_only and hasattr(self.runtime, "split"):
            split = await self.runtime.split(batch_id, evidence, policy=policy, authorize=authorize)
            if split:
                return split
        evidence_key = hashlib.sha256(json.dumps(
            evidence or {"head": head_sha, "brief": brief}, sort_keys=True,
        ).encode()).hexdigest()
        launch = None
        legacy = False
        async with self.db.immediate() as conn:
            queue = await conn.scalar(select(integration_batches.c.fixer_queue).where(
                integration_batches.c.id == batch_id,
            )) or {}
            family = queue.get("family_id", batch_id)
            await conn.execute(select(func.pg_advisory_xact_lock(
                func.hashtextextended("batch-fixer-family:" + family, 0),
            )))
            # Publication and rebind lock companion/bound rows in this order.
            # Lock the family owner before selecting either individual row so
            # a successor allocation cannot invert that order.
            await conn.execute(select(integration_batches.c.id).where(
                (integration_batches.c.id == batch_id) |
                (integration_batches.c.fixer_state["family_id"].astext == family),
            ).order_by(integration_batches.c.id).with_for_update())
            batch = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch_id,
            ).with_for_update())).mappings().one_or_none()
            if batch is None:
                return {"success": False, "outcome": "missing_batch"}
            if target_ref not in {candidate_ref(batch_id), batch["target_ref"]}:
                raise ValueError("fixer target is not this batch's branch")
            if batch["intent"] == "aborted" and queue.get("status") in {"deferred", "companion"}:
                return {"success": True, "outcome": "deferred", "reason": queue["reason"],
                        "session_id": (batch["fixer_state"] or {}).get("session_id")}
            if held or batch["intent"] != "open" or batch["lifecycle"] in {
                "promoted", "aborted", "failed", "empty",
            }:
                return {"success": True, "outcome": "held"}
            state = dict(batch["fixer_state"] or {})
            if not state and family:
                owner = (await conn.execute(select(integration_batches).where(
                    integration_batches.c.id != batch_id,
                    integration_batches.c.fixer_state["family_id"].astext == family,
                    integration_batches.c.fixer_state["session_id"].astext.is_not(None),
                ).with_for_update())).mappings().one_or_none()
                if owner:
                    if existing_only:
                        return {"success": True, "outcome": "exists",
                                "session_id": owner["fixer_state"]["session_id"]}
                    if authorize is None or not await authorize():
                        return {"success": True, "outcome": "withdrawn"}
                    bound = dict(owner["fixer_state"])
                    if (bound.get("blocker") or self.clock() >= bound["deadline"]
                            or len(bound["red_keys"]) >= bound["max_rounds"]):
                        from src.integration.fixer_batches import defer_on

                        reason = bound.get("blocker") or "fixer family budget exhausted"
                        if not bound.get("blocker"):
                            bound["blocker"] = reason
                            await self._write(conn, owner["id"], bound)
                            await self._message(conn, owner, bound, "blocker", reason,
                                                supervisor=True)
                        await defer_on(conn, batch, now=self.clock(), reason=reason,
                                       head_sha=head_sha,
                                       revisit_seconds=bound.get("revisit_seconds", 300))
                        return {"success": True, "outcome": "deferred", "reason": reason}
                    pending = {"batch_id": batch_id, "head_sha": head_sha,
                               "evidence_key": evidence_key, "evidence": evidence, "brief": brief}
                    if bound.get("pending_binding") != pending:
                        bound["pending_binding"] = pending
                        await self._write(conn, owner["id"], bound)
                        await self._message(conn, owner, bound, "rebind:" + evidence_key, (
                            f"Batch {batch_id} needs the same fixer at {head_sha}. Save and "
                            "publish your current work, then run `aq integration batch-fix "
                            "--action rebind` to switch this checkout under its branch fence. "
                            "Do not switch branches manually.\n" + brief
                        ))
                    return {"success": True, "outcome": "exists", "rebind_required": True,
                            "session_id": bound["session_id"]}
            if not state and batch["repair_attempt_count"]:
                status = await conn.scalar(select(tasks.c.status).where(
                    tasks.c.id == ordinary_repair_id(batch_id, batch["repair_attempt_count"]),
                ))
                legacy = status is not None and status not in {"COMPLETED", "FAILED", "BLOCKED"}
            if legacy:
                # Finish an already held ordinary repair; never allocate its successor.
                pass
            elif not state:
                if existing_only:
                    return {"success": True, "outcome": "absent"}
                if authorize is None or not await authorize():
                    return {"success": True, "outcome": "withdrawn"}
                members = (await conn.execute(select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == batch_id,
                ).order_by(integration_batch_members.c.ordinal))).mappings().all()
                identity = uuid4().hex
                now = self.clock()
                state = {
                    "version": 1, "batch_id": batch_id, "project_id": batch["project_id"],
                    "family_id": family,
                    "repository_id": batch["repository_id"], "target_ref": target_ref,
                    "delivery_ref": batch["target_ref"], "session_id": identity,
                    "session_name": "n-batch-fixer-" + hashlib.sha256(batch_id.encode()).hexdigest(),
                    "instance_token": uuid4().hex[:12], "status": "launching",
                    "starting_sha": head_sha, "head_sha": head_sha, "red_heads": [head_sha],
                    "red_keys": [evidence_key], "failure_evidence": evidence,
                    "created_at": now,
                    "deadline": now + (policy.primary_seconds if policy else 1800),
                    "budget_seconds": policy.primary_seconds if policy else 1800,
                    "revisit_seconds": policy.deferred_revisit_seconds if policy else 300,
                    "max_rounds": min(3, policy.primary_attempts) if policy else 3,
                    "brief": brief, "sync_default_branch": sync_default_branch,
                    "members": [{"task_id": m["task_id"], "source_sha": m["source_sha"],
                                 "source_base_sha": m["source_base_sha"]} for m in members],
                }
                await self._write(conn, batch_id, state)
                # This is an observation generation, never a task filing counter.
                await conn.execute(update(integration_batches).where(
                    integration_batches.c.id == batch_id,
                ).values(repair_attempt_count=integration_batches.c.repair_attempt_count + 1))
                launch = state
            else:
                if state["status"] == "waiting_capacity":
                    state.update(status="launching", deadline=self.clock() + state["budget_seconds"],
                                 starting_sha=head_sha, head_sha=head_sha, red_heads=[head_sha],
                                 red_keys=[evidence_key], failure_evidence=evidence, brief=brief)
                    launch = state
                if state["target_ref"] != target_ref:
                    return await self._block(conn, batch, state, "repair branch changed")
                if state["status"] == "green":
                    # The same batch can turn red when its target advances. Preserve
                    # the original session and its accumulated fixes.
                    state["status"] = "active"
                if not existing_only and evidence_key not in state["red_keys"]:
                    state["red_heads"] = [*state["red_heads"], head_sha]
                    state["red_keys"] = [*state["red_keys"], evidence_key]
                    state["failure_evidence"] = evidence
                    state["head_sha"] = head_sha
                    state["brief"] = brief
                    if not state.get("blocker") and self.clock() < state["deadline"] \
                            and len(state["red_heads"]) <= state["max_rounds"]:
                        await self._message(conn, batch, state, "red:" + evidence_key, (
                            f"The batch is red at {head_sha}. Continue in the same checkout "
                            f"on {target_ref}; preserve all frozen members.\n{brief}\n"
                            f"Failure evidence: {json.dumps(evidence, sort_keys=True)}\n"
                            "Commit fixes, run aq integration batch-fix --action push, "
                            "then end your turn for the next CI observation."
                        ))
                if self.clock() >= state["deadline"]:
                    return await self._block(conn, batch, state, "fixer time budget exhausted")
                if len(state["red_heads"]) > state["max_rounds"]:
                    return await self._block(conn, batch, state, "fixer CI round budget exhausted")
                await self._write(conn, batch_id, state)
        if legacy:
            return await self.legacy.recover_reservation(
                batch_id, target_ref=target_ref, head_sha=head_sha, authorize=authorize, held=held,
            )
        if launch:
            try:
                details = await self.runtime.start(launch)
            except Exception as exc:
                from src.integration.fixer_runtime import FixerCapacityUnavailable
                from src.integration.fixer_batches import defer_on

                async with self.db.immediate() as conn:
                    row = (await conn.execute(select(integration_batches).where(
                        integration_batches.c.id == batch_id,
                    ).with_for_update())).mappings().one()
                    current = dict(row["fixer_state"])
                    if isinstance(exc, FixerCapacityUnavailable):
                        current["status"] = "waiting_capacity"
                        await self._write(conn, batch_id, current)
                        await defer_on(conn, row, now=self.clock(), reason="capacity",
                                       head_sha=head_sha,
                                       revisit_seconds=current["revisit_seconds"])
                        return {"success": True, "outcome": "deferred", "reason": str(exc),
                                "session_id": current["session_id"]}
                    return await self._block(conn, row, current, f"launch incomplete: {exc}")
            async with self.db.immediate() as conn:
                row = (await conn.execute(select(integration_batches).where(
                    integration_batches.c.id == batch_id,
                ).with_for_update())).mappings().one()
                state = dict(row["fixer_state"])
                state.update(details)
                if state["status"] == "launching":
                    state["status"] = "active"
                await self._write(conn, batch_id, state)
            return {"success": True, "outcome": "filed", "session_id": state["session_id"]}
        if state.get("blocker"):
            return {"success": True, "outcome": "deferred", "reason": state["blocker"],
                    "session_id": state["session_id"]}
        if state["status"] == "launching":
            return {"success": True, "outcome": "exists", "session_id": state["session_id"]}
        if not await self.runtime.alive(state):
            async with self.db.immediate() as conn:
                row = (await conn.execute(select(integration_batches).where(
                    integration_batches.c.id == batch_id,
                ).with_for_update())).mappings().one()
                return await self._block(conn, row, dict(row["fixer_state"]),
                                         "fixer session is stopped or launch is uncertain")
        return {"success": True, "outcome": "exists", "session_id": state["session_id"]}

    async def recover_reservation(self, batch_id, **kwargs):
        # In-place sessions reserve only during publication. Pending/unknown CI
        # cannot spend a round, emit a deadline notice or wake a fixer. Retained
        # ordinary repairs still need their original preparation lease restored.
        row = await self.db.get_integration_batch(batch_id)
        if row and row["fixer_state"]:
            return {"success": True, "outcome": "exists",
                    "session_id": row["fixer_state"]["session_id"]}
        return await self.allocate(batch_id, existing_only=True, **kwargs)

    async def settle_green(self, batch_id, head_sha):
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch_id,
            ).with_for_update())).mappings().one_or_none()
            if row is None:
                return
            state = dict(row["fixer_state"] or {})
            if state and state.get("green_notified_sha") != head_sha:
                state.update(status="green", green_sha=head_sha, green_notified_sha=head_sha)
                await self._write(conn, batch_id, state)
                await self._message(conn, row, state, "green:" + head_sha, (
                    f"Batch {batch_id} is green or delivered at {head_sha}. "
                    "Stop editing and pushing this branch. End your turn."
                ))
            await self.legacy._settle_green_on(conn, batch_id, head_sha)

    async def settle_delivered(self, batch_id, head_sha):
        await self.settle_green(batch_id, head_sha)
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch_id,
            ))).mappings().one()
            family = (row["fixer_queue"] or {}).get("family_id", batch_id)
            await conn.execute(select(func.pg_advisory_xact_lock(
                func.hashtextextended("batch-fixer-family:" + family, 0),
            )))
            owner = (await conn.execute(select(integration_batches).where(
                integration_batches.c.fixer_state["family_id"].astext == family,
            ).with_for_update())).mappings().one_or_none()
            state = dict(owner["fixer_state"]) if owner else None
            if state:
                outstanding = await conn.scalar(select(integration_batches.c.id).where(
                    integration_batches.c.id != batch_id,
                    integration_batches.c.fixer_queue["family_id"].astext
                    == state.get("family_id", batch_id),
                    integration_batches.c.lifecycle != "promoted",
                    func.coalesce(integration_batches.c.fixer_queue["status"].astext, "")
                    != "companion",
                ).limit(1))
                if outstanding:
                    return
                if owner["id"] != batch_id:
                    state.update(status="green", green_sha=head_sha, green_notified_sha=head_sha)
                    await self._write(conn, owner["id"], state)
                    await self._message(conn, owner, state, "green:" + head_sha,
                                        "The complete batch family is delivered. Stop editing.")
                await conn.execute(update(sessions).where(
                    sessions.c.id == state["session_id"],
                    sessions.c.instance_token == state["instance_token"],
                    sessions.c.task_id.is_(None),
                ).values(desired_state="stopped"))

    async def for_session(self, session_id):
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.fixer_state["session_id"].astext == session_id,
            ))).mappings().one_or_none()
        return dict(row) if row else None

    async def command(self, principal, *, action):
        """The session binding, never caller args, chooses the batch and branch."""
        from src.commands.principal import PrincipalKind

        if principal.kind is not PrincipalKind.SESSION or not principal.session_id:
            return {"success": False, "outcome": "unauthorized"}
        batch = await self.for_session(principal.session_id)
        session = await self.db.get_session(principal.session_id)
        if (batch is None or session is None or session.task_id is not None
                or session.instance_token != principal.session_instance_token
                or session.project_id != batch["project_id"]
                or batch["fixer_state"]["instance_token"] != session.instance_token):
            return {"success": False, "outcome": "unauthorized"}
        if action == "status":
            return {"success": True, "fixer": batch["fixer_state"]}
        if action == "rebind":
            return await self.runtime.rebind(batch, session)
        if action != "push":
            return {"success": False, "error": "action must be status, push or rebind"}
        state = batch["fixer_state"]
        deferred = (batch["fixer_queue"] or {}).get("status") in {"deferred", "companion"}
        if state["status"] != "active" or (batch["intent"] != "open" and not deferred):
            return {"success": False, "outcome": "held"}
        return await self.runtime.push(batch, session)


def split_members(members, attributed_members, dependencies):
    """A strict implicated subset and its dependent closure, or no safe split.

    An unattributed group is represented by None. Missing attribution never
    becomes permission to eject all inputs. Dependencies point from a member to
    the other frozen members it requires.
    """
    members = frozenset(members)
    groups = tuple(attributed_members)
    if not groups or any(not group or not set(group) <= members for group in groups):
        return None
    failing = set().union(*groups)
    while True:
        closure = failing | {member for member in members
                             if set(dependencies.get(member, ())) & failing}
        if closure == failing:
            break
        failing = closure
    if failing == members:
        return None
    return {"retained": sorted(members - failing), "deferred": sorted(failing)}
