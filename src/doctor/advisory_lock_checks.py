"""``db.idle_advisory_locks``: name and release advisory locks held idle in a transaction.

Symptom (2026-10-27, brisk-glacier-94): one backend sat ``idle in
transaction`` for ~20 minutes holding ``pg_advisory_xact_lock`` while the
coroutine that opened it awaited Git I/O on other connections.  Pool claims
stalled behind the load, and nobody could say which daemon work owned
``classid 2848513581, objid 457584937``.

The check lists every advisory lock in the daemon's database, joins each to
its backend, and names it where the key is derivable: a session lock taken
through :func:`src.database.advisory_locks.session_advisory_lock` in this
process, or the project behind a hierarchy lock.  A granted lock whose
backend has been idle in a transaction for more than :data:`IDLE_LIMIT_S`
is a warning.  ``--fix`` terminates exactly that backend, and only while it
is still in the same idle state it was observed in.  An idle backend has no
running query, so ``pg_cancel_backend`` cannot release it; termination
rolls its transaction back and releases its locks.  A plain ``idle`` session
lock holder is intended (a dedicated autocommit connection), so it is listed
and never terminated.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.doctor.models import (
    CheckResult,
    DoctorCheck,
    DoctorContext,
    ObservationUnavailable,
    Severity,
)

logger = logging.getLogger(__name__)

CHECK_ID = "db.idle_advisory_locks"

#: A transaction holding an advisory lock may stay idle this long, no longer.
IDLE_LIMIT_S = 30.0

_LOCKS = text("""
SELECT a.pid, a.application_name, a.state, a.backend_start, a.state_change,
       EXTRACT(EPOCH FROM now() - a.state_change)::float8 AS idle_s,
       EXTRACT(EPOCH FROM now() - a.xact_start)::float8 AS xact_s,
       left(a.query, 300) AS query,
       l.classid::bigint AS classid, l.objid::bigint AS objid, l.objsubid,
       l.mode, l.granted,
       CASE WHEN l.granted THEN '{}'::int[] ELSE pg_blocking_pids(a.pid) END AS blocked_by
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory'
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND a.pid <> pg_backend_pid()
ORDER BY a.state_change, a.pid, l.classid, l.objid
""")

_TERMINATE = text("""
SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a
WHERE a.pid = :pid AND a.backend_start = :backend_start
  AND a.state_change = :state_change AND a.state LIKE 'idle in transaction%'
""")


def _int8_key(classid: int, objid: int) -> int:
    """The bigint passed to ``pg_advisory_lock(bigint)`` (``objsubid`` 1)."""
    key = (classid << 32) | objid
    return key - (1 << 64) if key >= 1 << 63 else key


def _int4(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


async def _observe(conn) -> list[dict]:
    from src.database.advisory_locks import held_advisory_locks
    from src.database.queries.hierarchy_queries import HIERARCHY_LOCK_NAMESPACE

    rows = [dict(row) for row in (await conn.execute(_LOCKS)).mappings()]
    held = held_advisory_locks()
    by_key = {key: name for (_pid, key), (name, _at) in held.items()}
    hierarchy = {_int4(row["objid"]) for row in rows
                 if row["objsubid"] == 2 and row["classid"] == HIERARCHY_LOCK_NAMESPACE}
    projects = {}
    if hierarchy:
        projects = {int(h): project_id for project_id, h in (await conn.execute(text(
            "SELECT id, hashtext(id) FROM projects WHERE hashtext(id) = ANY(:hashes)"),
            {"hashes": sorted(hierarchy)})).all()}
    for row in rows:
        name = None
        if row["objsubid"] == 1:
            row["key"] = _int8_key(row["classid"], row["objid"])
            name = by_key.get(row["key"])
        elif row["classid"] == HIERARCHY_LOCK_NAMESPACE:
            project = projects.get(_int4(row["objid"]))
            name = f"hierarchy:{project}" if project else None
        row["name"] = name
    return rows


def _offenders(rows: list[dict]) -> dict[int, dict]:
    stuck = {}
    for row in rows:
        if (row["granted"] and (row["state"] or "").startswith("idle in transaction")
                and (row["idle_s"] or 0.0) > IDLE_LIMIT_S):
            stuck.setdefault(row["pid"], row)
    return stuck


def _lock_label(row: dict) -> str:
    ident = (f"key {row['key']}" if "key" in row
             else f"classid {row['classid']}, objid {row['objid']}")
    return f"{row['name']} ({ident})" if row["name"] else ident


def _summary(rows: list[dict]) -> list[dict]:
    backends: dict[int, dict] = {}
    for row in rows:
        entry = backends.setdefault(row["pid"], {
            "pid": row["pid"], "application_name": row["application_name"],
            "state": row["state"], "idle_s": round(row["idle_s"] or 0.0, 1),
            "xact_s": None if row["xact_s"] is None else round(row["xact_s"], 1),
            "query": row["query"], "held": [], "waiting": [],
        })
        lock = {"lock": _lock_label(row), "mode": row["mode"]}
        if row["granted"]:
            entry["held"].append(lock)
        else:
            entry["waiting"].append({**lock, "blocked_by": list(row["blocked_by"] or [])})
    return list(backends.values())


async def _locks(ctx: DoctorContext) -> list[dict]:
    engine = getattr(ctx.db, "_engine", None)
    if engine is None:
        raise ObservationUnavailable("no database engine on this doctor context")
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        return await _observe(conn)


async def _check_idle_advisory_locks(ctx: DoctorContext) -> CheckResult:
    try:
        rows = await _locks(ctx)
    except ObservationUnavailable as exc:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail=str(exc),
                           execution_status="unavailable", reasons=["advisory_locks_unobserved"])
    backends = _summary(rows)
    stuck = _offenders(rows)
    data = {"idle_limit_s": IDLE_LIMIT_S, "backends": backends, "idle_in_transaction": sorted(stuck)}
    if stuck:
        named = "; ".join(
            f"pid {pid} ({row['application_name'] or 'unnamed'}) idle in transaction "
            f"{row['idle_s']:.0f}s holding "
            + ", ".join(_lock_label(r) for r in rows if r["pid"] == pid and r["granted"])
            for pid, row in stuck.items()
        )
        waiting = sum(1 for row in rows if not row["granted"])
        return CheckResult(
            id=CHECK_ID, severity=Severity.WARN,
            detail=(f"{named}; {waiting} advisory lock wait(s) queued; "
                    f"--fix terminates the idle backend(s)"),
            data=data, execution_status="succeeded", complete=True,
        )
    return CheckResult(
        id=CHECK_ID, severity=Severity.OK,
        detail=(f"{sum(1 for row in rows if row['granted'])} advisory lock(s) held; none by a "
                f"transaction idle over {IDLE_LIMIT_S:.0f}s"),
        data=data, execution_status="succeeded", complete=True,
    )


async def _fix_idle_advisory_locks(ctx: DoctorContext) -> CheckResult:
    rows = await _locks(ctx)
    terminated = []
    async with ctx.db._engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for pid, row in _offenders(rows).items():
            # The same backend in the same idle state, never a later transaction.
            if await conn.scalar(_TERMINATE, {
                "pid": pid, "backend_start": row["backend_start"],
                "state_change": row["state_change"],
            }):
                terminated.append(pid)
                logger.warning("db.idle_advisory_locks terminated pid %s (%s): %s", pid,
                               row["application_name"], _lock_label(row))
    return CheckResult(id=CHECK_ID, severity=Severity.OK,
                       detail=f"terminated {len(terminated)} idle backend(s)",
                       data={"terminated": terminated}, fix_applied=bool(terminated))


def advisory_lock_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=CHECK_ID, run=_check_idle_advisory_locks,
                        fix=_fix_idle_advisory_locks, timeout_s=15.0, owner="core")]
