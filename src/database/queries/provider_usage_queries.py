"""Reads and writes for ``provider_usage_snapshots``.

The table is append-only, so the only interesting logic here is the pair of
rules that keep it honest.  :meth:`record_provider_usage` drops a reading
identical to the newest row already stored for its series — both producers
re-report the same number until it moves (the transcript watcher re-reads a
rollout file every tick, the probe runs on a ten-minute timer against a window
that changes in whole percent), so without that check an idle fleet would
write a row a second forever and the series would carry no more information
than it does now.  But a dropped reading is still *evidence the probe ran*, so
the duplicate pushes the stored row's ``last_seen_at`` forward instead.  That
is what keeps a healthy account parked at 81% for six hours from reading as a
dead probe downstream: ``observed_at`` says when the number appeared,
``last_seen_at`` says when we last confirmed it, and staleness is only ever
computed from the latter.

Nothing here interprets a snapshot.  Staleness budgets, bar colouring and
reset arithmetic belong to the API and dashboard layers; this module only
moves rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from sqlalchemy import and_, delete, exists, func, insert, or_, select, update

from src.database.tables import provider_usage_snapshots, system_config


def probe_health_key(provider: str) -> str:
    """The ``system_config`` key holding *provider*'s newest probe verdict.

    Spelled once, here, because the writer (the probe command) and the
    reader (``aq doctor --check providers.claude_usage``) live in different
    subsystems and a typo in either is a check that silently never fires.
    """
    return f"providers.{provider}_usage.last_probe"

#: Two readings within this of each other are the same reading.  ``==`` on a
#: float that has been through JSON, a division and a round-trip through two
#: DBAPIs is not a question worth asking.
_EPSILON = 1e-9

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
    """Normalise a :class:`~src.providers.snapshot.ProviderUsageSnapshot` or mapping.

    Mappings are accepted so a caller holding a decoded ``rate_limits`` block
    does not have to build a dataclass just to hand it over.  ``last_seen_at``
    is not an input: on an insert it equals ``observed_at``, and afterwards it
    only ever moves through the duplicate path below.

    ``dataclasses.asdict`` rather than ``vars``: the snapshot is declared
    with ``slots=True``, which leaves the instance with no ``__dict__`` at
    all, and ``vars`` on one raises ``TypeError``.
    """
    if isinstance(snapshot, Mapping):
        raw: Mapping[str, Any] = snapshot
    elif is_dataclass(snapshot) and not isinstance(snapshot, type):
        raw = asdict(snapshot)
    else:
        raw = vars(snapshot)
    observed_at = float(raw["observed_at"])
    return {
        "provider": str(raw["provider"]),
        "account_label": str(raw.get("account_label") or ""),
        "window": str(raw["window"]),
        "scope": str(raw.get("scope") or ""),
        "used_percent": float(raw["used_percent"]),
        "resets_at": None if raw.get("resets_at") is None else float(raw["resets_at"]),
        "observed_at": observed_at,
        "last_seen_at": observed_at,
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


def _same_reading(a: tuple[float, float | None], b: tuple[float, float | None]) -> bool:
    """Compare two readings the way floats have to be compared.

    A missing reset clock equals a missing reset clock and differs from any
    clock at all — regaining one is news even when the percentage held.
    """
    if abs(a[0] - b[0]) >= _EPSILON:
        return False
    if a[1] is None or b[1] is None:
        return a[1] is None and b[1] is None
    return abs(a[1] - b[1]) < _EPSILON


class _Newest:
    """The newest row known for one series while a batch is being folded.

    ``row_id`` is ``None`` while the newest row is still a pending insert in
    this same batch, in which case ``pending`` points at the dict that will be
    inserted and a later duplicate bumps its ``last_seen_at`` in place.
    """

    __slots__ = ("last_seen_at", "observed_at", "pending", "reading", "row_id")

    def __init__(
        self,
        *,
        row_id: int | None,
        pending: dict[str, Any] | None,
        observed_at: float,
        reading: tuple[float, float | None],
        last_seen_at: float,
    ) -> None:
        self.row_id = row_id
        self.pending = pending
        self.observed_at = observed_at
        self.reading = reading
        self.last_seen_at = last_seen_at

    def confirm(self, observed_at: float) -> None:
        """Record that this value was seen again at *observed_at*."""
        if observed_at <= self.last_seen_at:
            return
        self.last_seen_at = observed_at
        if self.pending is not None:
            self.pending["last_seen_at"] = observed_at


def _newest_stmt(key: tuple[str, str, str]):
    provider, window, scope = key
    return (
        select(
            provider_usage_snapshots.c.id,
            provider_usage_snapshots.c.used_percent,
            provider_usage_snapshots.c.resets_at,
            provider_usage_snapshots.c.observed_at,
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


def _superseded():
    """True for a row that is not the newest of its series.

    Written as a correlated ``EXISTS`` over a self-alias rather than a grouped
    max so it reads the same on SQLite and PostgreSQL, and so an
    ``observed_at`` tie inside one series still leaves exactly one survivor.
    """
    newer = provider_usage_snapshots.alias("newer")
    return exists(
        select(newer.c.id).where(
            and_(
                newer.c.provider == provider_usage_snapshots.c.provider,
                newer.c.window == provider_usage_snapshots.c.window,
                newer.c.scope == provider_usage_snapshots.c.scope,
                or_(
                    newer.c.observed_at > provider_usage_snapshots.c.observed_at,
                    and_(
                        newer.c.observed_at == provider_usage_snapshots.c.observed_at,
                        newer.c.id > provider_usage_snapshots.c.id,
                    ),
                ),
            )
        )
    )


class ProviderUsageQueryMixin:
    """Query mixin for provider quota snapshots.  Expects ``self._engine``."""

    async def record_provider_usage(self, snapshots: Sequence[Any]) -> int:
        """Append *snapshots*, dropping unchanged repeats; return rows written.

        Three rules, in the order they apply to each snapshot:

        1. A snapshot older than the newest row already stored for its
           ``(provider, window, scope)`` series is discarded outright — no
           row, no ``last_seen_at``.  A rewound or replayed transcript must
           not be able to walk a percentage backwards or vouch for a value
           that has since moved on.
        2. A snapshot whose ``(used_percent, resets_at)`` matches that newest
           row is not written, but does push its ``last_seen_at`` to
           ``max(last_seen_at, observed_at)``: the value is unchanged and
           freshly confirmed.
        3. Anything else is written, with ``last_seen_at = observed_at``.

        All three apply within a batch as well, so handing over a hundred
        identical readings writes at most one row and leaves its
        ``last_seen_at`` at the newest of the hundred.

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
            newest: dict[tuple[str, str, str], _Newest] = {}
            for key in {_series_key(row) for row in rows}:
                stored = (await conn.execute(_newest_stmt(key))).first()
                if stored is not None:
                    newest[key] = _Newest(
                        row_id=int(stored[0]),
                        pending=None,
                        observed_at=float(stored[3]),
                        reading=(
                            float(stored[1]),
                            None if stored[2] is None else float(stored[2]),
                        ),
                        last_seen_at=float(stored[4]),
                    )

            for row in rows:
                key = _series_key(row)
                current = newest.get(key)
                if current is not None:
                    if row["observed_at"] < current.observed_at:
                        continue  # rule 1: stale reading, nothing to say
                    if _same_reading(current.reading, _reading(row)):
                        current.confirm(row["observed_at"])  # rule 2
                        continue
                newest[key] = _Newest(  # rule 3
                    row_id=None,
                    pending=row,
                    observed_at=row["observed_at"],
                    reading=_reading(row),
                    last_seen_at=row["observed_at"],
                )
                fresh.append(row)

            if fresh:
                await conn.execute(insert(provider_usage_snapshots), fresh)
            for entry in newest.values():
                if entry.row_id is None:
                    continue
                await conn.execute(
                    update(provider_usage_snapshots)
                    .where(
                        and_(
                            provider_usage_snapshots.c.id == entry.row_id,
                            provider_usage_snapshots.c.last_seen_at < entry.last_seen_at,
                        )
                    )
                    .values(last_seen_at=entry.last_seen_at)
                )
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

        The newest row of every series is never deleted, however old it is:
        pruning a card's only value turns a known-but-idle account into "no
        data", which is a worse lie than a stale number with a timestamp on
        it.  Everything behind it is history and goes.

        Bounded per call for the same reason the metrics prune is: the sweep
        runs on a tick that is already doing other work, and an unbounded
        delete over a table nobody pruned for a month would hold a write lock
        long enough to be felt.  The caller re-runs until it returns 0.
        """
        doomed = (
            select(provider_usage_snapshots.c.id)
            .where(
                and_(
                    provider_usage_snapshots.c.observed_at < float(before),
                    _superseded(),
                )
            )
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

    # -- probe health ------------------------------------------------------
    #
    # A failed probe writes no snapshot, so the snapshot table cannot tell
    # "the CLI's wording moved" from "nobody has probed lately".  The probe
    # records its own verdict here on every run, success or failure, and
    # ``aq doctor --check providers.claude_usage`` reads exactly this key.

    async def record_probe_health(self, provider: str, health: Mapping[str, Any]) -> None:
        """Store *health* as the newest verdict for *provider*'s usage probe.

        One row per provider, overwritten in place: this is a status, not a
        series.  The snapshots carry the history.
        """
        key = probe_health_key(provider)
        payload = json.dumps(dict(health), sort_keys=True)
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(system_config.c.key).where(system_config.c.key == key)
                )
            ).scalar()
            if existing is None:
                await conn.execute(insert(system_config).values(key=key, value=payload))
            else:
                await conn.execute(
                    update(system_config).where(system_config.c.key == key).values(value=payload)
                )

    async def read_probe_health(self, provider: str) -> dict[str, Any] | None:
        """The newest recorded verdict for *provider*'s probe, or ``None``.

        A row this function cannot decode is reported as absent rather than
        raised: the doctor check that reads it must be able to say "no probe
        has run" without a traceback.
        """
        async with self._engine.connect() as conn:
            raw = (
                await conn.execute(
                    select(system_config.c.value).where(
                        system_config.c.key == probe_health_key(provider)
                    )
                )
            ).scalar()
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None
