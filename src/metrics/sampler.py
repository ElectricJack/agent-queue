"""One sample per second of what the fleet is actually doing.

The sampler is the only writer of ``metrics_samples``.  Each tick it folds
the live state into one JSON sample, stores it at ``1s`` resolution and
publishes it on the EventBus as ``metrics.tick`` so the dashboard never has
to poll.  Two slower jobs ride the same loop: a roll-up that collapses
finished seconds into minutes and minutes into hours, and a retention sweep
that drops each tier past its own horizon.

**Cost is the design constraint.**  A metrics tab that makes the box it is
measuring slower is worse than no metrics tab, so the work is split by how
expensive it is:

* every tick — two grouped counts over small indexed tables (sessions by
  state, tasks by status), ``os.getloadavg()`` and one ``/proc/meminfo``
  read;
* every ``slow_interval_seconds`` — the reads that touch append-only,
  unbounded tables: the sub-agent event fold, the delegation fold, the
  token-ledger window and the worktree slot count.  Their results are
  cached and carried into the intervening per-second samples, so a sample
  is always complete even though most of it was not recomputed;
* every ``rollup_interval_seconds`` — completions and PRs per hour, then
  the roll-up and the prune.

Three series have no durable source at all.  The reconciler emits
``task.nudged`` and ``session.killed``, and the merge sweep emits
``merge.succeeded``, on the bus only — none is written to the ``events``
table.  The sampler therefore subscribes to the bus and keeps a one-hour
deque of timestamps for each.  Those three rates restart at zero when the
daemon does, which is why ``daemon.uptime_seconds`` is graphed beside them:
the reset is meant to be visible rather than silently smoothed over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The bus event carrying one live sample.  Never persisted through
#: ``db.log_event`` — at one per second that would add ~86k rows a day to
#: ``events`` and flood every WebSocket replay.
METRIC_TICK_EVENT = "metrics.tick"

#: Resolution tier → step in seconds.  Ordered coarsest-last; the roll-up
#: walks the pairs in order.
RESOLUTIONS: dict[str, int] = {"1s": 1, "1m": 60, "1h": 3600}

#: Bus events the sampler counts into rolling one-hour windows.
_COUNTED_EVENTS = ("task.nudged", "session.killed", "merge.succeeded")

_HOUR = 3600.0

#: Buckets one roll-up pass will fill.  Four hours of minutes, or ten days of
#: hours — enough that an ordinary restart catches up in a single pass, small
#: enough that a first run against a long history does not block a tick.
_MAX_BUCKETS_PER_PASS = 240

#: Row ceiling on the single range read each pass makes.
_ROLLUP_ROW_LIMIT = 100_000

#: Half-open range guard: ``[start, end + step)`` expressed with an inclusive
#: upper bound.
_EPSILON = 1e-6

#: Most sessions the sub-agent drill-down will name in one sample.  The fleet
#: totals are always exact; this bounds only the per-session breakdown, which
#: is stored 86,400 times a day.  Sessions that started children inside the
#: window outlive their own rows, so on a busy pool fleet the honest set is
#: unbounded — the busiest few are what a drill-down is for.
_MAX_SESSION_ROWS = 25


def floor_bucket(ts: float, step: int) -> float:
    """Floor *ts* to a whole multiple of *step* seconds."""
    return float(int(ts // step) * step)


def read_machine() -> dict[str, float | None]:
    """Load average and memory, or ``None`` where the platform has neither.

    Everything here is a cheap read of a virtual file or a libc call.  Values
    the platform cannot supply come back as ``None`` rather than 0 — a zero
    load average is a claim, an absent one is an admission.
    """
    out: dict[str, float | None] = {
        "load1": None,
        "load5": None,
        "load15": None,
        "cpu_count": float(os.cpu_count() or 0) or None,
        "mem_total_mb": None,
        "mem_free_mb": None,
        "mem_available_mb": None,
    }
    try:
        one, five, fifteen = os.getloadavg()
        out["load1"], out["load5"], out["load15"] = one, five, fifteen
    except (OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            wanted = {
                "MemTotal:": "mem_total_mb",
                "MemFree:": "mem_free_mb",
                "MemAvailable:": "mem_available_mb",
            }
            for line in handle:
                parts = line.split()
                if len(parts) < 2:
                    continue
                field = wanted.get(parts[0])
                if field is not None:
                    # /proc/meminfo reports kB.
                    out[field] = round(int(parts[1]) / 1024.0, 1)
    except (OSError, ValueError):
        pass
    return out


def aggregate_samples(samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Collapse several samples into one, recursively.

    Numeric leaves are averaged, because every series here is a gauge — a
    count of things that are true right now, or a rate already expressed per
    minute or per hour.  Summing them would turn "12 agents for 60 seconds"
    into 720 agents.  Non-numeric leaves (booleans like
    ``subagents.complete``, strings) take the **last** value: for a
    completeness flag the newest reading is the honest one.

    Keys absent from some samples are still carried: the average is over the
    samples that actually had the key, so a series that started mid-minute is
    not diluted by zeros it never reported.
    """
    materialised = [s for s in samples if isinstance(s, Mapping)]
    if not materialised:
        return {}
    out: dict[str, Any] = {}
    keys: list[str] = []
    seen: set[str] = set()
    for sample in materialised:
        for key in sample:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    for key in keys:
        values = [s[key] for s in materialised if key in s and s[key] is not None]
        if not values:
            out[key] = None
            continue
        if all(isinstance(v, Mapping) for v in values):
            out[key] = aggregate_samples(values)
        elif all(isinstance(v, bool) for v in values):
            out[key] = values[-1]
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            mean = sum(values) / len(values)
            out[key] = round(mean, 4)
        else:
            out[key] = values[-1]
    return out


class MetricsSampler:
    """Collects, stores, rolls up and prunes the fleet metrics series.

    Args:
        db: the database adapter (needs :class:`MetricsQueryMixin`).
        config: the daemon's ``AppConfig``.
        bus: the ``EventBus``.  Optional — without it the sampler still
            writes rows, it just cannot publish ticks or count bus-only
            events.
        clock: injected ``time.time`` replacement for deterministic tests.
    """

    def __init__(self, db, config, bus=None, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.config = config
        self.bus = bus
        self._clock = clock
        self._started_at = clock()
        self._daemon_starts = 0
        self._task: asyncio.Task | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        # Rolling one-hour windows of bus-only events, newest last.
        self._counters: dict[str, deque[float]] = {
            name: deque() for name in _COUNTED_EVENTS
        }
        # Values recomputed on the slow tier and carried between ticks.
        self._slow: dict[str, Any] = {}
        self._slow_at: float = 0.0
        self._hourly: dict[str, Any] = {}
        self._hourly_at: float = 0.0
        # Both start at the sampler's own start time, not at 0: an epoch-zero
        # baseline makes "has the interval elapsed?" true on the very first
        # tick, so the buffer would commit a single row and the roll-up would
        # run against an empty tier before anything had been sampled.
        self._rollup_at: float = self._started_at
        # Per-second samples awaiting one batched commit, keyed by bucket.
        self._pending: dict[float, dict[str, Any]] = {}
        self._flushed_at: float = self._started_at
        # In-memory resume point per target tier, seeded from the DB on the
        # first pass and advanced past every bucket examined thereafter.
        self._rollup_from: dict[str, float] = {}
        # Per-tick wall-clock cost of ``collect`` — surfaced in the sample so
        # the sampler's own overhead is one of the things you can graph.
        self._last_collect_ms: float = 0.0

    @property
    def _settings(self):
        """The live ``metrics:`` config section.

        Read through on every access rather than captured at construction:
        the section is hot-reloadable, and ``reload_config`` swaps in a whole
        new ``MetricsConfig`` — a reference taken in ``__init__`` would keep
        serving the values the daemon booted with.
        """
        return getattr(self.config, "metrics", None)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the bus, record the restart, and run the loop."""
        if self._settings is not None and not self._settings.enabled:
            logger.info("Metrics sampler disabled by config")
            return
        self.subscribe()
        try:
            self._daemon_starts = await self.db.bump_daemon_start_count()
        except Exception:
            logger.debug("metrics: daemon start counter unavailable", exc_info=True)
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Stop the loop, drop bus subscriptions, and commit what is buffered."""
        for unsub in self._unsubscribe:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubscribe.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        # After the loop is down, so the final flush cannot race a tick that
        # is still appending to the buffer.
        with contextlib.suppress(Exception):
            await self.flush()

    def subscribe(self) -> None:
        """Start counting the bus-only events (nudges, kills, merges)."""
        if self.bus is None:
            return
        for name in _COUNTED_EVENTS:
            handler = self._make_counter(name)
            self._unsubscribe.append(self.bus.subscribe(name, handler))

    def _make_counter(self, name: str) -> Callable[[dict], None]:
        window = self._counters[name]

        def _record(_data: dict) -> None:
            window.append(self._clock())
            self._trim(window)

        return _record

    def _trim(self, window: deque[float]) -> None:
        cutoff = self._clock() - _HOUR
        while window and window[0] < cutoff:
            window.popleft()

    async def run(self) -> None:
        """Tick forever at ``interval_seconds``, absorbing per-tick failures.

        One bad tick must not end the series: a transient DB error is logged
        and the loop sleeps to the next interval.  The sleep is the interval
        minus however long the tick actually took, so a slow tick shortens the
        following sleep instead of letting the cadence drift.
        """
        while True:
            # Read per iteration: the section is hot-reloadable, so changing
            # the cadence should not need a daemon restart.
            interval = float(getattr(self._settings, "interval_seconds", 1.0) or 1.0)
            # Monotonic, not the injected clock: that one exists so bucket
            # arithmetic is deterministic in tests, and pacing a real sleep
            # off a frozen clock would busy-loop.
            started = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("metrics sampler tick failed")
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    # -- one tick ----------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        """Collect a sample, publish it, buffer it, and run the due chores.

        The publish comes **before** every write: the dashboard's live series
        is fed by this event, and there is no reason for a chart to wait on a
        flush, a roll-up or a retention sweep.  The bus is handed a copy —
        ``emit`` stamps ``_event_type`` and ``event_id`` onto the payload, and
        those belong on the frame, not in the stored sample.
        """
        sample = await self.collect()
        if self.bus is not None:
            with contextlib.suppress(Exception):
                await self.bus.emit(METRIC_TICK_EVENT, dict(sample))

        bucket = floor_bucket(sample["ts"], RESOLUTIONS["1s"])
        self._pending[bucket] = sample
        flush_every = float(getattr(self._settings, "flush_interval_seconds", 5.0) or 5.0)
        if sample["ts"] - self._flushed_at >= flush_every:
            self._flushed_at = sample["ts"]
            await self.flush()
        rollup_every = float(getattr(self._settings, "rollup_interval_seconds", 60.0) or 60.0)
        if sample["ts"] - self._rollup_at >= rollup_every:
            self._rollup_at = sample["ts"]
            # The roll-up reads the 1s tier, so anything still buffered has
            # to be on disk first or the closing minute averages a hole.
            await self.flush()
            await self.roll_up(sample["ts"])
            await self.prune(sample["ts"])
        return sample

    async def collect(self) -> dict[str, Any]:
        """Build one sample of the current fleet state."""
        started = time.perf_counter()
        now = self._clock()
        live = await self.db.metrics_live_counts()

        slow_every = float(getattr(self._settings, "slow_interval_seconds", 5.0) or 5.0)
        if now - self._slow_at >= slow_every or not self._slow:
            self._slow_at = now
            self._slow = await self._collect_slow(now)
        if now - self._hourly_at >= 60.0 or not self._hourly:
            self._hourly = await self._collect_hourly(now)

        sample: dict[str, Any] = {
            "ts": now,
            "agents": _fold_sessions(live["sessions"]),
            "tasks": _fold_tasks(live["tasks"]),
            "machine": read_machine(),
            "daemon": {
                "uptime_seconds": round(max(0.0, now - self._started_at), 3),
                "restarts": max(0, self._daemon_starts - 1),
            },
            "stall": {
                "nudges_per_hour": self._rate("task.nudged"),
                "kills_per_hour": self._rate("session.killed"),
            },
            "merges_per_hour": self._rate("merge.succeeded"),
            **self._slow,
            **self._hourly,
        }
        self._last_collect_ms = round((time.perf_counter() - started) * 1000, 3)
        sample["sampler"] = {"collect_ms": self._last_collect_ms}
        return sample

    async def flush(self) -> int:
        """Commit the buffered per-second samples.  Returns rows written.

        Buffering is what keeps the sampler cheap: a commit is one fsync, and
        one fsync per ``flush_interval_seconds`` costs the same as one per
        second.  The window this trades away is durability of the newest few
        seconds of *history* — the live chart is fed by the WebSocket tick,
        which is emitted before any of this, so nothing on screen waits for
        the disk.
        """
        if not self._pending:
            return 0
        rows = sorted(self._pending.items())
        self._pending = {}
        try:
            await self.db.write_metrics_samples("1s", rows)
        except Exception:
            logger.exception("metrics: flushing %d sample(s) failed", len(rows))
            return 0
        return len(rows)

    def _rate(self, name: str) -> float:
        """Events of *name* in the trailing hour."""
        window = self._counters.get(name)
        if window is None:
            return 0.0
        self._trim(window)
        return float(len(window))

    def _window(self, key: str, default: float) -> float:
        """A configured measurement window, floored at a minute."""
        value = getattr(self._settings, key, None)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = default
        return max(60.0, seconds or default)

    async def _collect_slow(self, now: float) -> dict[str, Any]:
        """The tier that range-scans append-only tables.  Cached between ticks."""
        token_window = self._window("token_window_seconds", 300.0)
        spawn_window = self._window("subagent_window_seconds", _HOUR)
        snapshot = await self.db.metrics_slow_snapshot(
            now - token_window,
            recent_ts=now - 60.0,
            spawn_since_ts=now - spawn_window,
        )
        live = snapshot["live"]
        ids = [row[0] for row in live]
        names = {row[0]: row[1] for row in live}
        hooks = {row[0]: row[2] for row in live}
        native = snapshot["native"]
        delegated = snapshot["delegated"]
        spawned = snapshot.get("spawned") or {}
        slots = snapshot["slots"]
        ledger = snapshot["ledger"]

        by_session: dict[str, dict[str, Any]] = {}
        native_total = 0
        aq_total = 0
        # Live sessions first, keyed by the name the dashboard shows.  Every
        # session that started a child inside the window follows, live or
        # not: on a pool fleet the session is usually gone by the time you
        # look, and dropping it would erase the only record of the work.
        for sid in ids:
            native_count = int(native.get(sid, 0))
            aq_count = int(delegated.get(sid, 0))
            spawn_count = int(spawned.get(sid, 0))
            if native_count == 0 and aq_count == 0 and spawn_count == 0:
                # A session with no children of any kind adds nothing to the
                # drill-down and would otherwise put one entry per idle
                # session into every stored sample.
                continue
            by_session[names.get(sid, sid)] = {
                "native": native_count,
                "aq": aq_count,
                "spawned": spawn_count,
                # False here means the launch never wired the harness hooks,
                # so this row's native count is a floor, not a total.
                "hooks": bool(hooks.get(sid, False)),
            }
            native_total += native_count
            aq_total += aq_count
        live_ids = set(ids)
        spawned_total = 0
        for sid, count in spawned.items():
            spawned_total += int(count)
            if sid in live_ids:
                continue
            # No name to show: the session row is gone, which is the whole
            # reason this entry exists.  The id is what the events carry.
            by_session[sid] = {
                "native": 0,
                "aq": 0,
                "spawned": int(count),
                "hooks": True,
            }
        if len(by_session) > _MAX_SESSION_ROWS:
            ranked = sorted(
                by_session.items(),
                key=lambda item: (
                    item[1]["native"] + item[1]["aq"] + item[1]["spawned"],
                    item[0],
                ),
                reverse=True,
            )
            by_session = dict(ranked[:_MAX_SESSION_ROWS])

        by_model: dict[str, dict[str, float]] = {}
        totals = dict.fromkeys(
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 0
        )
        recent_totals = dict(totals)
        unattributed = 0
        recent_unattributed = 0
        for row in ledger:
            attributed = sum(row.get(name, 0) for name in totals)
            recent_attributed = sum(row.get(f"recent_{name}", 0) for name in totals)
            for name in totals:
                totals[name] += row.get(name, 0)
                recent_totals[name] += row.get(f"recent_{name}", 0)
            unattributed += max(0, row.get("tokens_used", 0) - attributed)
            recent_unattributed += max(
                0, row.get("recent_tokens_used", 0) - recent_attributed
            )
            model = row["model"]
            if model:
                by_model[model] = {
                    "input_per_min": _per_min(row.get("input_tokens", 0), token_window),
                    "output_per_min": _per_min(row.get("output_tokens", 0), token_window),
                    "cache_read_per_min": _per_min(
                        row.get("cache_read_tokens", 0), token_window
                    ),
                    "cache_write_per_min": _per_min(
                        row.get("cache_write_tokens", 0), token_window
                    ),
                    "total_per_min": _per_min(row.get("tokens_used", 0), token_window),
                }

        attributed_total = sum(totals.values())
        recent_attributed_total = sum(recent_totals.values())
        return {
            "subagents": {
                # ``complete`` is the conjunction over live sessions, matching
                # ``flock_rollup``: one session launched without hooks makes
                # the fleet total a lower bound.
                "complete": all(hooks.get(sid, False) for sid in ids) if ids else True,
                "native": native_total,
                "aq": aq_total,
                # Children open right now.  ``total`` is the same number under
                # its original name, kept so stored history stays readable.
                "active": native_total + aq_total,
                "total": native_total + aq_total,
                # Children *started* in the window, whether or not the session
                # that started them still exists.  This is the series that
                # actually moves on a pool fleet.
                "spawned_per_hour": round(spawned_total * (_HOUR / spawn_window), 3),
                "by_session": by_session,
            },
            "tokens": {
                # Measured over ``token_window`` and scaled to a minute, so a
                # per-turn usage flush is a rate rather than a spike.
                "input_per_min": _per_min(totals["input_tokens"], token_window),
                "output_per_min": _per_min(totals["output_tokens"], token_window),
                "cache_read_per_min": _per_min(totals["cache_read_tokens"], token_window),
                "cache_write_per_min": _per_min(
                    totals["cache_write_tokens"], token_window
                ),
                # Everything the ledger recorded, cache included.  This is the
                # line the tab plots: input+output alone is a few percent of a
                # cached agent's traffic.
                "total_per_min": _per_min(
                    attributed_total + unattributed, token_window
                ),
                # Ledger volume no column could account for — rows written
                # before the cache columns existed, or by a writer that
                # reports only a total.
                "unattributed_per_min": _per_min(unattributed, token_window),
                # The raw trailing minute, unsmoothed, for anyone who needs to
                # see the flush pattern rather than the rate.
                "input_per_min_1m": float(recent_totals["input_tokens"]),
                "output_per_min_1m": float(recent_totals["output_tokens"]),
                "total_per_min_1m": float(
                    recent_attributed_total + recent_unattributed
                ),
                "window_seconds": token_window,
                "by_model": by_model,
            },
            "slots": {
                "used": slots["locked"],
                "total": slots["total"],
                "cap": _slot_cap(self.config),
            },
        }

    async def _collect_hourly(self, now: float) -> dict[str, Any]:
        """Throughput read off the durable completion records."""
        self._hourly_at = now
        window = await self.db.metrics_completion_window(now - _HOUR)
        return {
            "throughput": {
                "completions_per_hour": float(window["completions"]),
                # Completions carrying a PR url — the durable proxy for merge
                # sweep throughput, distinct from the live ``merge.succeeded``
                # count which resets with the daemon.
                "prs_per_hour": float(window["with_pr"]),
            }
        }

    # -- roll-up and retention --------------------------------------------

    async def roll_up(self, now: float) -> dict[str, int]:
        """Collapse finished 1s buckets into 1m, and 1m into 1h.

        Only *closed* buckets are rolled up — the bucket containing ``now``
        is still filling, and averaging it early would publish a minute that
        later changes.

        The resume point comes from the newest bucket already stored at the
        coarser tier, so a daemon that was down for ten minutes backfills
        those ten minutes on its first pass instead of leaving a hole.  It is
        then held in memory and advanced past every bucket examined, empty
        ones included.  Without that, an idle install — where the target tier
        stays empty and ``latest`` stays ``None`` — would re-walk its whole
        retention window (720 hour-buckets) on every roll-up, forever.

        Each pass reads its source range once and groups in Python rather
        than issuing a query per bucket, and stops after
        :data:`_MAX_BUCKETS_PER_PASS` so a long backfill catches up over
        successive roll-ups instead of stalling a tick.
        """
        written = {"1m": 0, "1h": 0}
        for source, target in (("1s", "1m"), ("1m", "1h")):
            step = RESOLUTIONS[target]
            newest_closed = floor_bucket(now, step) - step
            # Buckets older than the *source* tier's retention have no rows
            # left to average, so that is the floor on any backfill.
            horizon = floor_bucket(now - self._backfill_span(source), step)
            start = self._rollup_from.get(target)
            if start is None:
                start = await self._resume_point(source, target, step, horizon, newest_closed)
            start = max(start, horizon)
            if start > newest_closed:
                self._rollup_from[target] = start
                continue
            end = min(newest_closed, start + (_MAX_BUCKETS_PER_PASS - 1) * step)
            rows = await self.db.read_metrics_samples(
                source, start, end + step - _EPSILON, limit=_ROLLUP_ROW_LIMIT
            )
            grouped: dict[float, list[dict]] = {}
            for row in rows:
                grouped.setdefault(floor_bucket(row["ts"], step), []).append(row)
            batch: list[tuple[float, dict[str, Any]]] = []
            for bucket in sorted(grouped):
                merged = aggregate_samples(grouped[bucket])
                merged["ts"] = bucket
                batch.append((bucket, merged))
            if batch:
                await self.db.write_metrics_samples(target, batch)
                written[target] += len(batch)
            self._rollup_from[target] = end + step
        return written

    async def _resume_point(
        self, source: str, target: str, step: int, horizon: float, newest_closed: float
    ) -> float:
        """Where a cold backfill should begin for *target*.

        Newest stored target bucket when there is one.  Otherwise the oldest
        bucket the *source* tier actually holds, clamped to the horizon —
        starting at the horizon itself would mean crawling hundreds of empty
        buckets, at :data:`_MAX_BUCKETS_PER_PASS` a pass, before reaching any
        data.  With no source rows at all there is nothing to roll up, so the
        resume point jumps straight to the open bucket.
        """
        latest = await self.db.latest_metrics_bucket(target)
        if latest is not None:
            return latest + step
        oldest = await self.db.oldest_metrics_bucket(source)
        if oldest is None:
            return newest_closed + step
        return max(horizon, floor_bucket(oldest, step))

    def _retention(self, resolution: str) -> float:
        """Configured horizon for *resolution*.  ``0`` means keep forever."""
        default = {"1s": 3600, "1m": 30 * 86400, "1h": 365 * 86400}[resolution]
        value = getattr(self._settings, f"retain_seconds_{resolution}", None)
        return float(default if value is None else value)

    def _backfill_span(self, resolution: str) -> float:
        """How far back a cold roll-up will look for source rows.

        Retention is the natural bound — older buckets have no source rows
        left — but "keep forever" (``0``) is not a bound at all, so the tier's
        default horizon stands in.
        """
        configured = self._retention(resolution)
        if configured > 0:
            return configured
        return float({"1s": 3600, "1m": 30 * 86400, "1h": 365 * 86400}[resolution])

    async def prune(self, now: float) -> dict[str, int]:
        """Drop each tier past its own horizon.  ``0`` retention disables it."""
        horizons = {
            resolution: now - self._retention(resolution)
            for resolution in RESOLUTIONS
            if self._retention(resolution) > 0
        }
        deleted = dict.fromkeys(RESOLUTIONS, 0)
        deleted.update(await self.db.prune_metrics_samples(horizons))
        return deleted


def _per_min(tokens: float, window_seconds: float) -> float:
    """*tokens* observed over *window_seconds*, expressed per minute."""
    if window_seconds <= 0:
        return 0.0
    return round(float(tokens) * (60.0 / window_seconds), 3)


def _slot_cap(config) -> int | None:
    """Fleet-wide worktree slot cap, or ``None`` when worktrees are off.

    A slot is only ever handed to a session, and ``resources`` is what caps
    concurrent sessions on this box — so that is the ceiling the used/cap
    gauge is honestly measured against.
    """
    worktrees = getattr(config, "worktrees", None)
    if worktrees is not None and not getattr(worktrees, "enabled", True):
        return None
    resources = getattr(config, "resources", None)
    cap = getattr(resources, "max_concurrent_agents", None)
    return int(cap) if cap else None


def _fold_sessions(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Pivot the grouped session count into the maps the charts want."""
    by_state: dict[str, int] = {}
    by_harness: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    by_lifecycle: dict[str, int] = {}
    running = 0
    for row in rows:
        count = int(row.get("count") or 0)
        state = row.get("state") or "unknown"
        by_state[state] = by_state.get(state, 0) + count
        if state == "draining":
            # Counted in ``by_state`` so the drain is visible, but excluded
            # from every "how many agents are working" breakdown.
            continue
        running += count
        harness = row.get("harness") or "unknown"
        profile = row.get("profile_id") or "unknown"
        lifecycle = row.get("lifecycle") or "unknown"
        by_harness[harness] = by_harness.get(harness, 0) + count
        by_profile[profile] = by_profile.get(profile, 0) + count
        by_lifecycle[lifecycle] = by_lifecycle.get(lifecycle, 0) + count
    return {
        "total": running,
        "by_state": by_state,
        "by_harness": by_harness,
        "by_profile": by_profile,
        "by_lifecycle": by_lifecycle,
    }


#: Statuses the tab graphs explicitly.  Anything else is summed into
#: ``other`` rather than dropped, so the totals still add up.
_GRAPHED_STATUSES = ("READY", "IN_PROGRESS", "ASSIGNED", "PAUSED", "BLOCKED", "WAITING_INPUT")


def _fold_tasks(counts: Mapping[str, int]) -> dict[str, int]:
    out = {status: int(counts.get(status, 0)) for status in _GRAPHED_STATUSES}
    out["other"] = sum(
        int(value) for key, value in counts.items() if key not in _GRAPHED_STATUSES
    )
    out["total"] = sum(int(value) for value in counts.values())
    return out
