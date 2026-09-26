"""Immutable test recommendations, appended evidence and promotion lifecycle.

Commands own authorization and input validation. This persistence API never
updates a selection or observation; retention deletes selections and cascades
their evidence. A promotion's only transition is its first revocation.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    test_selection_observations,
    test_selection_promotions,
    test_selections,
)


class PromotionActive(RuntimeError):
    """The project already has an active omission-policy promotion."""


class TestSelectionQueriesMixin:
    """Persistence for the explicit test-selection command boundary."""

    async def insert_test_selection(self, values: Mapping[str, Any]) -> dict:
        row_values = dict(values)
        row_values.setdefault("id", f"tsel-{uuid.uuid4().hex}")
        row_values.setdefault("created_at", time.time())
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        insert(test_selections).values(row_values).returning(test_selections)
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)

    async def get_test_selection(self, selection_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(test_selections).where(test_selections.c.id == selection_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    async def list_test_selections(
        self,
        *,
        project_id: str,
        task_id: str | None = None,
        limit: int = 50,
        before: float | None = None,
    ) -> list[dict]:
        statement = select(test_selections).where(test_selections.c.project_id == project_id)
        if task_id is not None:
            statement = statement.where(test_selections.c.task_id == task_id)
        if before is not None:
            statement = statement.where(test_selections.c.created_at < before)
        statement = statement.order_by(
            test_selections.c.created_at.desc(), test_selections.c.id.desc()
        ).limit(limit)
        async with self._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(statement)).mappings()]

    async def append_test_selection_observation(
        self,
        *,
        selection_id: str,
        kind: str,
        source: str,
        exit_code: int | None,
        duration_ms: int | None,
        executed_modules: list[str],
        failed_node_ids: list[str],
        payload: dict,
        observed_at: float,
    ) -> dict:
        async with self.immediate() as conn:
            # Keep retention from deleting the parent between lookup and append.
            exists = await conn.scalar(
                select(test_selections.c.id)
                .where(test_selections.c.id == selection_id)
                .with_for_update(read=True, key_share=True)
            )
            if exists is None:
                raise LookupError(f"unknown test selection: {selection_id}")
            row = (
                (
                    await conn.execute(
                        insert(test_selection_observations)
                        .values(
                            id=f"tsobs-{uuid.uuid4().hex}",
                            selection_id=selection_id,
                            kind=kind,
                            source=source,
                            exit_code=exit_code,
                            duration_ms=duration_ms,
                            executed_modules=executed_modules,
                            failed_node_ids=failed_node_ids,
                            payload=payload,
                            observed_at=observed_at,
                        )
                        .returning(test_selection_observations)
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)

    async def list_test_selection_observations(self, selection_id: str) -> list[dict]:
        statement = (
            select(test_selection_observations)
            .where(test_selection_observations.c.selection_id == selection_id)
            .order_by(test_selection_observations.c.observed_at, test_selection_observations.c.id)
        )
        async with self._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(statement)).mappings()]

    async def active_test_selection_promotion(self, *, project_id: str) -> dict | None:
        statement = select(test_selection_promotions).where(
            test_selection_promotions.c.project_id == project_id,
            test_selection_promotions.c.revoked_at.is_(None),
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
            return dict(row) if row is not None else None

    async def insert_test_selection_promotion(self, values: Mapping[str, Any]) -> dict:
        row_values = dict(values)
        row_values.setdefault("id", f"tsprom-{uuid.uuid4().hex}")
        # Only suppress a conflict on the active-project index. Invalid foreign
        # keys, required fields and duplicate ids remain ordinary IntegrityErrors.
        statement = (
            pg_insert(test_selection_promotions)
            .values(row_values)
            .on_conflict_do_nothing(
                index_elements=[test_selection_promotions.c.project_id],
                index_where=test_selection_promotions.c.revoked_at.is_(None),
            )
            .returning(test_selection_promotions)
        )
        async with self.immediate() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
            if row is None:
                raise PromotionActive(
                    f"active test selection promotion for {row_values['project_id']}"
                )
            return dict(row)

    async def revoke_test_selection_promotion(
        self, promotion_id: str, *, now: float, reason: str
    ) -> bool:
        statement = (
            update(test_selection_promotions)
            .where(
                test_selection_promotions.c.id == promotion_id,
                test_selection_promotions.c.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoke_reason=reason)
        )
        async with self.immediate() as conn:
            return (await conn.execute(statement)).rowcount == 1

    async def delete_test_selections_older_than(self, *, older_than: float) -> int:
        async with self.immediate() as conn:
            result = await conn.execute(
                delete(test_selections).where(test_selections.c.created_at < older_than)
            )
            return result.rowcount
