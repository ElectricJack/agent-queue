"""Caller-transaction-owned persistence for integration rollout controls."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    gates,
    integration_history_waiver_consumptions,
    integration_history_waivers,
    integration_legacy_gate_applicability,
    integration_legacy_suppression,
    integration_rollout_transitions,
    projects,
)


ROLLOUT_MODES = frozenset({"disabled", "observe", "hierarchy", "train"})
_BLOCKER_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _mode(value: str) -> str:
    if value not in ROLLOUT_MODES:
        raise ValueError("integration rollout mode must be disabled, observe, hierarchy, or train")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or _BLOCKER_DIGEST.fullmatch(value) is None:
        raise ValueError("blocker digest must be an exact lowercase sha256 digest")
    return value


class IntegrationControlQueriesMixin:
    """Durable primitives composed by the later hierarchy-locked cutover."""

    async def cas_project_integration_control_on(
        self,
        conn: AsyncConnection,
        *,
        project_id: str,
        expected_generation: int,
        effective_mode: str,
        desired_mode: str,
        draining: bool,
    ) -> bool:
        """CAS the project projection; callers append audit in the same transaction."""
        if expected_generation < 0:
            raise ValueError("expected integration generation must be non-negative")
        result = await conn.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .where(projects.c.hierarchical_integration_generation == expected_generation)
            .values(
                hierarchical_integration_mode=_mode(effective_mode),
                hierarchical_integration_desired_mode=_mode(desired_mode),
                hierarchical_integration_draining=bool(draining),
                hierarchical_integration_generation=expected_generation + 1,
            )
        )
        return result.rowcount == 1

    async def append_integration_rollout_transition_on(
        self,
        conn: AsyncConnection,
        *,
        transition_id: str,
        project_id: str,
        generation: int,
        old_effective_mode: str,
        new_effective_mode: str,
        old_desired_mode: str,
        new_desired_mode: str,
        draining: bool,
        operator_id: str,
        reason: str,
        blocker_digest: str,
        old_legacy_policy: dict[str, Any],
        new_legacy_policy: dict[str, Any],
        waiver_id: str | None,
        now: float,
    ) -> None:
        """Append one exact post-CAS transition record without rewriting history."""
        if not transition_id or not operator_id or not reason:
            raise ValueError("transition identity, operator, and reason are required")
        _mode(old_effective_mode)
        _mode(new_effective_mode)
        _mode(old_desired_mode)
        _mode(new_desired_mode)
        _digest(blocker_digest)
        current = (
            await conn.execute(
                select(
                    projects.c.hierarchical_integration_mode,
                    projects.c.hierarchical_integration_desired_mode,
                    projects.c.hierarchical_integration_draining,
                ).where(
                    projects.c.id == project_id,
                    projects.c.hierarchical_integration_generation == generation,
                )
            )
        ).one_or_none()
        if current is None or tuple(current) != (
            new_effective_mode,
            new_desired_mode,
            bool(draining),
        ):
            raise ValueError("transition does not match the current project generation")
        if waiver_id is not None:
            waiver = (
                await conn.execute(
                    select(integration_history_waivers.c.id).where(
                        integration_history_waivers.c.id == waiver_id,
                        integration_history_waivers.c.project_id == project_id,
                        integration_history_waivers.c.blocker_digest == blocker_digest,
                    )
                )
            ).scalar_one_or_none()
            if waiver is None:
                raise ValueError("history waiver does not match project and blocker digest")
        await conn.execute(
            insert(integration_rollout_transitions).values(
                id=transition_id,
                project_id=project_id,
                generation=generation,
                old_effective_mode=old_effective_mode,
                new_effective_mode=new_effective_mode,
                old_desired_mode=old_desired_mode,
                new_desired_mode=new_desired_mode,
                draining=bool(draining),
                operator_id=operator_id,
                reason=reason,
                blocker_digest=blocker_digest,
                old_legacy_policy=old_legacy_policy,
                new_legacy_policy=new_legacy_policy,
                waiver_id=waiver_id,
                created_at=now,
            )
        )

    async def append_integration_history_waiver_on(
        self,
        conn: AsyncConnection,
        *,
        waiver_id: str,
        project_id: str,
        operator_id: str,
        reason: str,
        blocker_digest: str,
        now: float,
    ) -> None:
        if not waiver_id or not operator_id or not reason:
            raise ValueError("waiver identity, operator, and reason are required")
        _digest(blocker_digest)
        await conn.execute(
            insert(integration_history_waivers).values(
                id=waiver_id,
                project_id=project_id,
                operator_id=operator_id,
                reason=reason,
                blocker_digest=blocker_digest,
                created_at=now,
            )
        )

    async def consume_integration_history_waiver_on(
        self,
        conn: AsyncConnection,
        *,
        waiver_id: str,
        transition_id: str,
        project_id: str,
        blocker_digest: str,
        consumed_by: str,
        now: float,
    ) -> bool:
        """Consume a matching waiver once without mutating its original record."""
        _digest(blocker_digest)
        insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        matching = (
            select(
                integration_history_waivers.c.id,
                integration_rollout_transitions.c.id,
                integration_history_waivers.c.project_id,
                literal(blocker_digest),
                literal(consumed_by),
                literal(now),
            )
            .select_from(
                integration_history_waivers.join(
                    integration_rollout_transitions,
                    integration_rollout_transitions.c.waiver_id
                    == integration_history_waivers.c.id,
                )
            )
            .where(
                integration_history_waivers.c.id == waiver_id,
                integration_history_waivers.c.project_id == project_id,
                integration_history_waivers.c.blocker_digest == blocker_digest,
                integration_rollout_transitions.c.id == transition_id,
                integration_rollout_transitions.c.project_id == project_id,
                integration_rollout_transitions.c.blocker_digest == blocker_digest,
            )
        )
        result = await conn.execute(
            insert_fn(integration_history_waiver_consumptions)
            .from_select(
                [
                    "waiver_id",
                    "transition_id",
                    "project_id",
                    "blocker_digest",
                    "consumed_by",
                    "consumed_at",
                ],
                matching,
            )
            .on_conflict_do_nothing(index_elements=["waiver_id"])
        )
        return result.rowcount == 1

    async def consume_integration_history_waiver(self, **values: Any) -> bool:
        async with self.immediate() as conn:
            return await self.consume_integration_history_waiver_on(conn, **values)

    async def append_integration_legacy_gate_applicability_on(
        self,
        conn: AsyncConnection,
        *,
        project_id: str,
        gate_id: str,
        waiver_id: str,
        transition_id: str,
        blocker_digest: str,
        applicable: bool,
        now: float,
    ) -> None:
        """Append waiver applicability without resolving or deleting the gate."""
        _digest(blocker_digest)
        match = (
            await conn.execute(
                select(integration_history_waiver_consumptions.c.waiver_id)
                .select_from(
                    integration_history_waiver_consumptions.join(
                        gates, gates.c.id == gate_id
                    )
                )
                .where(
                    integration_history_waiver_consumptions.c.waiver_id == waiver_id,
                    integration_history_waiver_consumptions.c.transition_id == transition_id,
                    integration_history_waiver_consumptions.c.project_id == project_id,
                    integration_history_waiver_consumptions.c.blocker_digest == blocker_digest,
                    gates.c.project_id == project_id,
                    gates.c.gate_type == "pr-merged",
                )
            )
        ).scalar_one_or_none()
        if match is None:
            raise ValueError("gate applicability requires a consumed matching history waiver")
        await conn.execute(
            insert(integration_legacy_gate_applicability).values(
                project_id=project_id,
                gate_id=gate_id,
                waiver_id=waiver_id,
                transition_id=transition_id,
                blocker_digest=blocker_digest,
                applicable=bool(applicable),
                created_at=now,
            )
        )

    async def set_integration_legacy_suppression_on(
        self,
        conn: AsyncConnection,
        *,
        project_id: str,
        generation: int,
        merge_sweep_suppressed: bool,
        final_review_route_suppressed: bool,
        legacy_gate_creation_suppressed: bool,
        policy_snapshot: dict[str, Any],
        now: float,
    ) -> None:
        """Set the reversible per-project legacy-routing projection."""
        project_generation = (
            await conn.execute(
                select(projects.c.hierarchical_integration_generation).where(
                    projects.c.id == project_id
                )
            )
        ).scalar_one_or_none()
        if project_generation != generation:
            raise ValueError("legacy suppression must match the current integration generation")
        insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        values = {
            "generation": generation,
            "merge_sweep_suppressed": bool(merge_sweep_suppressed),
            "final_review_route_suppressed": bool(final_review_route_suppressed),
            "legacy_gate_creation_suppressed": bool(legacy_gate_creation_suppressed),
            "policy_snapshot": policy_snapshot,
            "updated_at": now,
        }
        await conn.execute(
            insert_fn(integration_legacy_suppression)
            .values(project_id=project_id, **values)
            .on_conflict_do_update(
                index_elements=["project_id"],
                set_=values,
                where=integration_legacy_suppression.c.generation <= generation,
            )
        )

    async def get_integration_legacy_suppression(self, project_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(integration_legacy_suppression).where(
                        integration_legacy_suppression.c.project_id == project_id
                    )
                )
            ).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def list_integration_legacy_suppressions(self) -> list[dict]:
        """Return the small per-project routing predicate snapshot."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(integration_legacy_suppression).order_by(
                        integration_legacy_suppression.c.project_id
                    )
                )
            ).mappings().all()
        return [dict(row) for row in rows]
