"""Caller-transaction-owned persistence for integration rollout controls."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    integration_legacy_suppression,
    projects,
)


ROLLOUT_MODES = frozenset({"disabled", "observe", "hierarchy", "train", "development"})
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
        insert_fn = pg_insert
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
                (
                    await conn.execute(
                        select(integration_legacy_suppression).where(
                            integration_legacy_suppression.c.project_id == project_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None
