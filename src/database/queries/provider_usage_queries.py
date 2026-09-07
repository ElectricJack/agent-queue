"""Reads and writes for ``provider_usage_snapshots``.

The table is append-only, so the only interesting logic here is the one
thing that keeps it from growing without bound: :meth:`record_provider_usage`
drops a reading identical to the newest row already stored for its series and
advances that row's ``last_seen_at`` instead.
Both producers re-report the same number until it moves — the transcript
watcher re-reads a rollout file every tick, the probe runs on a ten-minute
timer against a window that changes in whole percent — so without that check
an idle fleet would write a row a second forever and the series would carry
no more information than it does now.

That second half matters as much as the first: a dropped reading is still a
*successful confirmation*, and a reader that could only see ``observed_at``
would have no way to tell a window steady at 81% for six hours from a producer
that died at 13:00.  ``observed_at`` is when a value first appeared;
``last_seen_at`` is when it was last confirmed, and freshness is measured from
the latter (spec amendment A3).

Nothing here interprets a snapshot.  Staleness budgets, bar colouring and
reset arithmetic belong to the API and dashboard layers; this module only
moves rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, delete, func, insert, select, update

from src.database.tables import provider_usage_snapshots

#: Columns returned by every reader, in table order.  Kept as one list so a
#: series row and a latest row are the same shape on the client.
_COLUMNS = (
    provider_usage_snapshots.c.id,
    provider_usage_snapshots.c.provider,
    provider_usage_snapshots.c.account_label,
    provider_usage_snapshots.c.window,
    provider_usage_snapshots.c.scope,
    provider_usage_snapshots.c.used_percent,
    provider_usage_snapshots.c.resets_at,
    provider_usage_snapshots.c.observed_at,
    provider_usage_snapshots.c.last_seen_at,
    provider_usage_snapshots.c.source,
)


def _as_dict(snapshot: Any) -> dict[str, Any]:
    """Normalise a :class:`~src.models.ProviderUsageSnapshot` or mapping.

    Mappings are accepted so a caller holding a decoded ``rate_limits`` block
    does not have to build a dataclass just to hand it over.
    """
    if isinstance(snapshot, Mapping):
        raw: Mapping[str, Any] = snapshot
    else:
        raw = vars(snapshot)
    return {
        "provider": str(raw["provider"]),
        "account_label": str(raw.get("account_label") or ""),
        "window": str(raw["window"]),
        "scope": str(raw.get("scope") or ""),
        "used_percent": float(raw["used_percent"]),
        "resets_at": None if raw.get("resets_at") is None else float(raw["resets_at"]),
        "observed_at": float(raw["observed_at"]),
        # A brand new value has been confirmed exactly once, at the moment it
        # was observed.  A caller may pass an explicit last_seen_at (a replayed
        # row), but never one earlier than observed_at.
        "last_seen_at": max(
            float(raw["observed_at"]),
            float(raw.get("last_seen_at") or raw["observed_at"]),
        ),
        "source": str(raw["source"]),
    }


def _series_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (row["provider"], row["window"], row["scope"])


def _reading(row: Mapping[str, Any]) -> tuple[float, float | None]:
    """The part of a row that has to *change* for it to be worth storing.

    ``observed_at`` is excluded on purpose — it moves on every read, and
    including it would make every snapshot novel and the dedup a no-op.
    ``account_label`` and ``source`` are excluded too: a plan rename or the
    same window arriving from the other producer is not new information about
    the quota.
    """
    return (float(row["used_percent"]), row["resets_at"])


def _newest_stmt(key: tuple[str, str, str]):
    provider, window, scope = key
    return (
        select(
            provider_usage_snapshots.c.id,
            provider_usage_snapshots.c.used_percent,
            provider_usage_snapshots.c.resets_at,
            provider_usage_snapshots.c.last_seen_at,
        )
        .where(
            and_(
                provider_usage_snapshots.c.provider == provider,
                provider_usage_snapshots.c.window == window,
                provider_usage_snapshots.c.scope == scope,
            )
        )
        .order_by(
            provider_usage_snapshots.c.observed_at.desc(),
            provider_usage_snapshots.c.id.desc(),
        )
        .limit(1)
    )


class ProviderUsageQueryMixin:
    """Query mixin for provider quota snapshots.  Expects ``self._engine``."""

    async def record_provider_usage(self, snapshots: Sequence[Any]) -> int:
        """Append *snapshots*, dropping unchanged repeats; return rows written.

        A snapshot whose ``(used_percent, resets_at)`` equals the newest row
        already stored for its ``(provider, window, scope)`` series is not
        written again.  It is not discarded either: it advances that row's
        ``last_seen_at`` to ``max(last_seen_at, observed_at)``, which is what
        lets a reader tell "steady at 81% and re-confirmed a minute ago" from
        "nothing has reported since 13:00".  The return value counts *rows
        written*, so a pure confirmation still returns 0.

        The rule applies within the batch as well, so handing over a hundred
        identical readings writes at most one row and leaves its
        ``last_seen_at`` at the newest observation in the batch.

        The whole batch runs in one transaction: on SQLite a commit is an
        fsync, and the transcript watcher can offer several series at once
        off a single rollout line.
        """
        rows = [_as_dict(snapshot) for snapshot in snapshots]
        if not rows:
            return 0
        # Oldest first, so "newest so far" is coherent while folding a batch
        # that arrived out of order.  Stable, so equal timestamps keep the
        # caller's ordering.
        rows.sort(key=lambda row: row["observed_at"])

        fresh: list[dict[str, Any]] = []
        async with self._engine.begin() as conn:
            # Per series: the newest reading, when it was last confirmed, and
            # where it lives -- ``row`` while it is still a pending insert in
            # this batch, ``row_id`` once it is a row in the table.
            newest: dict[tuple[str, str, str], dict[str, Any]] = {}
            for key in {_series_key(row) for row in rows}:
                stored = (await conn.execute(_newest_stmt(key))).first()
                if stored is not None:
                    newest[key] = {
                        "row_id": int(stored[0]),
                        "reading": (
                            float(stored[1]),
                            None if stored[2] is None else float(stored[2]),
                        ),
                        "last_seen": float(stored[3]),
                        "row": None,
                    }

            confirmed: dict[int, float] = {}
            for row in rows:
                key = _series_key(row)
                reading = _reading(row)
                current = newest.get(key)
                if current is not None and current["reading"] == reading:
                    seen = max(current["last_seen"], row["last_seen_at"])
                    if seen > current["last_seen"]:
                        current["last_seen"] = seen
                        if current["row"] is not None:
                            current["row"]["last_seen_at"] = seen
                        else:
                            confirmed[current["row_id"]] = seen
                    continue
                newest[key] = {
                    "row_id": None,
                    "reading": reading,
                    "last_seen": row["last_seen_at"],
                    "row": row,
                }
                fresh.append(row)

            for row_id, seen in confirmed.items():
                await conn.execute(
                    update(provider_usage_snapshots)
                    .where(provider_usage_snapshots.c.id == row_id)
                    .values(last_seen_at=seen)
                )
            if fresh:
                await conn.execute(insert(provider_usage_snapshots), fresh)
        return len(fresh)

    async def latest_provider_usage(self, provider: str | None = None) -> list[dict]:
        """Newest row per ``(provider, window, scope)``, newest series first.

        Written as a grouped max joined back to the table rather than a
        window function so it reads identically on SQLite and PostgreSQL.
        Two rows in one series can share an ``observed_at`` (a batch written
        from one transcript line), so the join can return both and the fold
        below keeps the larger ``id`` — the later insert.
        """
        newest = select(
            provider_usage_snapshots.c.provider,
            provider_usage_snapshots.c.window,
            provider_usage_snapshots.c.scope,
            func.max(provider_usage_snapshots.c.observed_at).label("observed_at"),
        ).group_by(
            provider_usage_snapshots.c.provider,
            provider_usage_snapshots.c.window,
            provider_usage_snapshots.c.scope,
        )
        if provider is not None:
            newest = newest.where(provider_usage_snapshots.c.provider == provider)
        sub = newest.subquery()
        stmt = select(*_COLUMNS).join(
            sub,
            and_(
                provider_usage_snapshots.c.provider == sub.c.provider,
                provider_usage_snapshots.c.window == sub.c.window,
                provider_usage_snapshots.c.scope == sub.c.scope,
                provider_usage_snapshots.c.observed_at == sub.c.observed_at,
            ),
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()

        winners: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            row = dict(row)
            key = _series_key(row)
            current = winners.get(key)
            if current is None or int(row["id"]) > int(current["id"]):
                winners[key] = row
        return sorted(
            winners.values(),
            key=lambda row: (-float(row["observed_at"]), row["provider"], row["window"]),
        )

    async def provider_usage_series(
        self,
        provider: str,
        window: str,
        scope: str = "",
        *,
        since: float = 0.0,
        limit: int = 10_000,
    ) -> list[dict]:
        """One series, oldest first, from *since* (inclusive) onward."""
        stmt = (
            select(*_COLUMNS)
            .where(
                and_(
                    provider_usage_snapshots.c.provider == provider,
                    provider_usage_snapshots.c.window == window,
                    provider_usage_snapshots.c.scope == scope,
                    provider_usage_snapshots.c.observed_at >= float(since),
                )
            )
            .order_by(
                provider_usage_snapshots.c.observed_at,
                provider_usage_snapshots.c.id,
            )
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def purge_provider_usage(self, before: float, *, limit: int = 1000) -> int:
        """Delete up to *limit* snapshots observed before *before*; return the count.

        Bounded per call for the same reason the metrics prune is: the sweep
        runs on a tick that is already doing other work, and an unbounded
        delete over a table nobody pruned for a month would hold a write lock
        long enough to be felt.  The caller re-runs until it returns 0.
        """
        doomed = (
            select(provider_usage_snapshots.c.id)
            .where(provider_usage_snapshots.c.observed_at < float(before))
            .order_by(provider_usage_snapshots.c.observed_at)
            .limit(max(1, int(limit)))
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(provider_usage_snapshots).where(
                    provider_usage_snapshots.c.id.in_(doomed.scalar_subquery())
                )
            )
        return int(result.rowcount or 0)
