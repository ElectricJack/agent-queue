"""Connection-aware persistence primitives for triage decisions."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import and_, insert, select, update

from src.database.queries.session_queries import _row_to_session
from src.database.tables import (
    agent_profiles,
    agents,
    gates,
    playbook_runs,
    sessions,
    task_gates,
    task_routing_decisions,
    task_routing_deferrals,
    tasks,
)


def _locked(stmt, conn):
    return stmt.with_for_update() if conn.dialect.name == "postgresql" else stmt


class RoutingDecisionQueriesMixin:
    """Queries that participate in the caller's ``immediate`` transaction."""

    async def lock_triage_run_on(self, conn, run_id: str):
        result = await conn.execute(
            _locked(select(playbook_runs).where(playbook_runs.c.run_id == run_id), conn)
        )
        return result.mappings().one_or_none()

    async def lock_triage_session_on(self, conn, session_id: str):
        result = await conn.execute(
            _locked(select(sessions).where(sessions.c.id == session_id), conn)
        )
        return result.mappings().one_or_none()

    async def lock_routing_task_on(self, conn, task_id: str):
        result = await conn.execute(
            _locked(select(tasks).where(tasks.c.id == task_id), conn)
        )
        return result.mappings().one_or_none()

    async def routing_catalog_inputs_on(self, conn):
        """Return one coherent, definition-locked catalog input snapshot."""
        agent_rows = (
            await conn.execute(_locked(select(agents).order_by(agents.c.id), conn))
        ).mappings().all()
        profile_rows = (
            await conn.execute(
                _locked(select(agent_profiles).order_by(agent_profiles.c.id), conn)
            )
        ).mappings().all()
        session_rows = (
            await conn.execute(_locked(select(sessions).order_by(sessions.c.id), conn))
        ).mappings().all()
        return (
            [self._row_to_agent(row) for row in agent_rows],
            [self._row_to_profile(row) for row in profile_rows],
            [_row_to_session(row) for row in session_rows],
        )

    async def get_routing_decision_on(self, conn, task_id: str, routing_revision: int):
        row = (
            await conn.execute(
                select(task_routing_decisions).where(
                    task_routing_decisions.c.task_id == task_id,
                    task_routing_decisions.c.routing_revision == routing_revision,
                )
            )
        ).mappings().one_or_none()
        return self._decode_decision(row)

    async def get_routing_decision(self, decision_id: str):
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(task_routing_decisions).where(
                        task_routing_decisions.c.id == decision_id
                    )
                )
            ).mappings().one_or_none()
        return self._decode_decision(row)

    @staticmethod
    def _decode_decision(row):
        if row is None:
            return None
        value = dict(row)
        value["execution_snapshot"] = json.loads(value["execution_snapshot"])
        return value

    async def insert_routing_decision_on(
        self,
        conn,
        *,
        project_id: str,
        task_id: str,
        routing_revision: int,
        run,
        execution_type_key: str,
        execution_snapshot: dict,
        reason: str,
    ) -> str:
        decision_id = uuid.uuid4().hex
        await conn.execute(
            insert(task_routing_decisions).values(
                id=decision_id,
                project_id=project_id,
                task_id=task_id,
                routing_revision=routing_revision,
                playbook_run_id=run["run_id"],
                playbook_id=run["playbook_id"],
                playbook_version=run["playbook_version"],
                execution_type_key=execution_type_key,
                execution_snapshot=json.dumps(execution_snapshot, sort_keys=True),
                profile_id=execution_snapshot["profile_id"],
                intelligence_class=execution_snapshot["intelligence_class"],
                reason=reason,
                decided_at=time.time(),
            )
        )
        return decision_id

    async def apply_routing_decision_on(
        self, conn, *, task_id: str, decision_id: str, execution_snapshot: dict
    ) -> None:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(
                profile_id=execution_snapshot["profile_id"],
                intelligence_class=execution_snapshot["intelligence_class"],
                routing_decision_id=decision_id,
                updated_at=time.time(),
            )
        )

    async def resolve_routing_gates_on(
        self, conn, *, task_id: str, resolution: str
    ) -> list[str]:
        gate_ids = list(
            (
                await conn.execute(
                    select(gates.c.id)
                    .select_from(gates.join(task_gates, task_gates.c.gate_id == gates.c.id))
                    .where(
                        task_gates.c.task_id == task_id,
                        gates.c.gate_type == "routing",
                        gates.c.status == "open",
                    )
                    .order_by(gates.c.id)
                )
            ).scalars()
        )
        if gate_ids:
            await conn.execute(
                update(gates)
                .where(gates.c.id.in_(gate_ids), gates.c.status == "open")
                .values(status="resolved", resolved_by="task_route", resolution=resolution)
            )
        return gate_ids

    async def insert_or_get_routing_deferral_on(
        self,
        conn,
        *,
        project_id: str,
        task_id: str,
        routing_revision: int,
        playbook_run_id: str,
        catalog_generation: str,
        policy_generation: str,
        reason: str,
    ) -> dict:
        existing = (
            await conn.execute(
                select(task_routing_deferrals).where(
                    task_routing_deferrals.c.task_id == task_id,
                    task_routing_deferrals.c.routing_revision == routing_revision,
                    task_routing_deferrals.c.catalog_generation == catalog_generation,
                    task_routing_deferrals.c.policy_generation == policy_generation,
                )
            )
        ).mappings().one_or_none()
        if existing is not None:
            return dict(existing)
        value = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "task_id": task_id,
            "routing_revision": routing_revision,
            "playbook_run_id": playbook_run_id,
            "catalog_generation": catalog_generation,
            "policy_generation": policy_generation,
            "reason": reason,
            "deferred_at": time.time(),
        }
        await conn.execute(insert(task_routing_deferrals).values(**value))
        return value

    async def get_routing_deferral(self, deferral_id: str):
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(task_routing_deferrals).where(
                        task_routing_deferrals.c.id == deferral_id
                    )
                )
            ).mappings().one_or_none()
        return dict(row) if row else None

    async def has_open_routing_gate_on(self, conn, task_id: str) -> bool:
        return bool(
            await conn.scalar(
                select(gates.c.id)
                .select_from(gates.join(task_gates, task_gates.c.gate_id == gates.c.id))
                .where(
                    and_(
                        task_gates.c.task_id == task_id,
                        gates.c.gate_type == "routing",
                        gates.c.status == "open",
                    )
                )
                .limit(1)
            )
        )
