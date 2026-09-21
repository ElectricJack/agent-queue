"""Reads and writes for ``provider_availability`` and its transition log.

The daemon's availability service is the only writer (provider-failover
D7).  It serialises writes per provider in memory, so an upsert here does
not need a compare-and-swap: the row and the transition it produced land in
one transaction, and a restart reloads exactly what was committed.

Nothing here interprets a row.  The state machine is
:mod:`src.providers.availability`; this module only moves rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import provider_availability, provider_availability_transitions

_ROW_COLUMNS = tuple(c.name for c in provider_availability.columns)


def _row_values(row: Mapping[str, Any]) -> dict[str, Any]:
    values = {name: row[name] for name in _ROW_COLUMNS if name in row}
    if "evidence" in values:
        values["evidence"] = [dict(entry) for entry in values["evidence"] or ()]
    return values


class ProviderAvailabilityQueryMixin:
    """Query mixin for provider availability.  Expects ``self._engine``."""

    async def list_provider_availability(self) -> list[dict]:
        """Every provider row, ordered by key."""
        stmt = select(provider_availability).order_by(provider_availability.c.provider)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def get_provider_availability(self, provider: str) -> dict | None:
        stmt = select(provider_availability).where(provider_availability.c.provider == provider)
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        return dict(row) if row is not None else None

    async def save_provider_availability(
        self,
        row: Mapping[str, Any],
        transition: Mapping[str, Any] | None = None,
    ) -> None:
        """Upsert *row* and append *transition*, atomically.

        The transition is the audit record of the same change the row
        describes, so the two never land apart: a crash between them would
        leave a state with no history, or history for a state never stored.
        """
        values = _row_values(row)
        stmt = pg_insert(provider_availability).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[provider_availability.c.provider],
            # ``notified_generation`` is written only by
            # :meth:`claim_provider_notification`; an upsert from a row read
            # before a claim must not roll it back.
            set_={
                k: v for k, v in values.items() if k not in ("provider", "notified_generation")
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
            if transition is not None:
                await conn.execute(
                    insert(provider_availability_transitions).values(
                        provider=transition["provider"],
                        from_state=transition["from_state"],
                        to_state=transition["to_state"],
                        reason_code=transition.get("reason_code") or "",
                        reason=transition.get("reason") or "",
                        until=transition.get("until"),
                        generation=int(transition["generation"]),
                        actor=transition.get("actor") or "system",
                        detail=dict(transition.get("detail") or {}),
                        at=float(transition["at"]),
                    )
                )

    async def list_provider_transitions(
        self,
        provider: str | None = None,
        *,
        since: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Transitions newest first, optionally for one provider and after *since*."""
        stmt = select(provider_availability_transitions)
        if provider is not None:
            stmt = stmt.where(provider_availability_transitions.c.provider == provider)
        if since is not None:
            stmt = stmt.where(provider_availability_transitions.c.at >= float(since))
        stmt = stmt.order_by(
            provider_availability_transitions.c.at.desc(),
            provider_availability_transitions.c.id.desc(),
        ).limit(max(1, int(limit)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def claim_provider_notification(self, provider: str, generation: int) -> bool:
        """Take the right to notify about *provider*'s *generation*, once.

        A compare-and-swap on ``notified_generation``: an event, its replay
        and the periodic timer may all ask, and exactly one gets ``True``.
        """
        stmt = (
            update(provider_availability)
            .where(
                and_(
                    provider_availability.c.provider == provider,
                    provider_availability.c.notified_generation < int(generation),
                    provider_availability.c.generation >= int(generation),
                )
            )
            .values(notified_generation=int(generation))
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
        return bool(result.rowcount)


__all__ = ["ProviderAvailabilityQueryMixin"]
