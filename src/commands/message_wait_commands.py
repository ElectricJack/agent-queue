"""A bounded attachment to the existing durable collaboration message wait."""

from __future__ import annotations

import time

from pydantic import ValidationError

from src.agent_waits import WaitError
from src.collaboration import CollaborationError, WAIT_REASONS, is_collaboration_thread
from src.commands.contracts.message_wait import MessageWaitArgs

_RECHECK_SECONDS = 2.0


class MessageWaitCommandsMixin:
    async def _message_wait_owner(self, args):
        caller = await self._collaboration_caller(args, mutation=True)
        if caller["kind"] != "member":
            raise CollaborationError("out_of_scope", "message wait requires a held member task")
        thread = await self._visible_collaboration(caller, args["thread_id"])
        member = next(m for m in thread["members"] if m["task_id"] == caller["task_id"])
        if member["state"] != "accepted" or member["accepted_claim_epoch"] != caller["claim_epoch"]:
            raise CollaborationError("not_accepted", "Accept collaboration for this claim first")
        return caller, thread

    async def _cmd_message_wait(self, args):
        disabled = self._messages_disabled_error()
        if disabled:
            return disabled
        try:
            values = MessageWaitArgs.model_validate(args)
            if not is_collaboration_thread(values.thread_id):
                raise CollaborationError(
                    "invalid", "For other threads use aq wait register --kind message"
                )
            caller, thread = await self._message_wait_owner(args)
            deadline = time.monotonic() + values.timeout
            # Subscribe before registration's producer snapshot. The bus is only
            # a wake hint; durable observation remains the source of truth.
            waiter = self.orchestrator.bus.waiter(
                ("message.sent",), filter={"thread_id": values.thread_id}
            )
            try:
                now = time.time()
                row = await self.db.register_agent_wait(
                    identity={
                        "session_id": caller["session_id"],
                        "instance_token": caller["instance_token"],
                        "project_id": caller["project_id"],
                        "claim_epoch": caller["claim_epoch"],
                        "elevated": False,
                    },
                    kind="message",
                    match={"thread_id": values.thread_id, "after_seq": values.after_seq},
                    deadline_at=min(now + 7200, thread["deadline_at"]),
                    idempotency_key=(
                        values.idempotency_key
                        or f"message-wait:{values.thread_id}:{values.after_seq}"
                    ),
                    now=now,
                )
                while True:
                    # A reconnect re-observes an active row returned by keyed
                    # registration. Recheck also covers missed events and peers
                    # that failed, disappeared or stopped running.
                    await self.db.reconcile_agent_waits(now=time.time(), wait_id=row["id"])
                    row = await self.db.get_agent_wait(row["id"])
                    live, _ = await self._message_wait_owner(args)
                    if (
                        live["session_id"] != caller["session_id"]
                        or live["task_id"] != caller["task_id"]
                        or live["claim_epoch"] != caller["claim_epoch"]
                    ):
                        raise CollaborationError("stale_claim", "Wait owner changed")
                    if row["state"] != "active":
                        page = await self.db.read_collaboration_messages(
                            values.thread_id,
                            project_id=caller["project_id"],
                            after_seq=values.after_seq,
                        )
                        reason = (row.get("digest") or {}).get("reason")
                        return {
                            "success": True,
                            "state": reason if reason in WAIT_REASONS else row["state"],
                            "wait": row,
                            **page,
                        }
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {
                            "success": True,
                            "state": "waiting",
                            "wait": row,
                            "cursor": values.after_seq,
                            "next_step": (
                                "End this turn. Resume with "
                                f"`aq wait show {row['id']} --consume --json`, or rerun "
                                f"`aq message wait --thread {values.thread_id} "
                                f"--after {values.after_seq}`."
                            ),
                        }
                    await waiter.wait(min(remaining, _RECHECK_SECONDS))
                    waiter.close()
                    # Each waiter is one-shot. Replace it before the next DB
                    # observation so a send in the check/subscribe gap is safe.
                    waiter = self.orchestrator.bus.waiter(
                        ("message.sent",), filter={"thread_id": values.thread_id}
                    )
            finally:
                waiter.close()
        except (CollaborationError, WaitError) as exc:
            return {"success": False, "error_code": exc.code, "error": str(exc)}
        except (ValidationError, TypeError, ValueError) as exc:
            return {"success": False, "error_code": "collaboration.invalid", "error": str(exc)}
