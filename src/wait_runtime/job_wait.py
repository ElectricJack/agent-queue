"""Verified job registration on the producer's existing transaction."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from src.database.queries.wait_queries import verify_owner
from src.database.tables import wait_records
from src.waits.conditions import Scope, WaitError
from src.waits.graph import lock_graph

if TYPE_CHECKING:
    from src.wait_runtime.lifecycle import WaitLifecycle


@dataclass(frozen=True)
class AtomicJobWait:
    lifecycle: "WaitLifecycle"
    scope: Scope | None
    source_watermark: int

    async def prepare(self, conn, values):
        # This must precede every job, workspace and owner row lock. The source
        # watermark was fetched before entering the producer transaction.
        if self.scope is None:
            raise WaitError("wait.out_of_scope", "submit-with-wait requires a bound owner")
        from src.waits.loops import prepare_progress

        await prepare_progress(conn, [{"owner_kind": self.scope.owner_kind,
            "owner_id": self.scope.owner_id, "project_id": self.scope.project_id,
            "session_id": self.scope.session_id}])
        await lock_graph(conn)
        scope = await verify_owner(conn, self.scope)
        if (scope.owner_kind != "task" or scope.owner_id != values["task_id"]
                or scope.project_id != values["project_id"]):
            raise WaitError("wait.out_of_scope", "submit-with-wait requires the held task")

    async def register(self, conn, job):
        receipt = await self.lifecycle.register_in_transaction(
            conn, {"condition": {"kind": "job", "job_id": job["id"]},
                   "idempotency_key": f"job:{job['id']}"},
            scope=self.scope, source_watermark=self.source_watermark,
            now=self.lifecycle.clock(),
        )
        wait = (await conn.execute(select(wait_records).where(
            wait_records.c.id == receipt["wait_id"],
        ))).mappings().one()
        # A subscribe/observe race commits its resolved audit row and lets the
        # worker continue. Replays also use current outcome rather than sleeping
        # again on an already completed source.
        step = ("continue" if wait["cleared_at"] is not None else "acknowledge"
                if wait["outcome"] != "pending" else receipt["next_step"])
        return {**dict(wait), "registration_receipt": {**receipt, "next_step": step}}
