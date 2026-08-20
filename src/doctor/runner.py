"""Doctor registry and concurrent runner.

Doctor owns the runner; subsystems own their checks (design §5.5).  The
runner's contract is that it never hangs and never dies on one bad check:
every check gets its own :func:`asyncio.timeout`, and a crash or timeout is
reported as an ``error`` result rather than propagating.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.doctor.models import (
    RESERVED_CHECK_IDS,
    SEVERITY_RANK,
    CheckResult,
    DoctorCheck,
    DoctorContext,
    Severity,
)

logger = logging.getLogger(__name__)


class DoctorRegistry:
    """In-memory registry of doctor checks, keyed by check id."""

    def __init__(self) -> None:
        self._checks: dict[str, DoctorCheck] = {}

    def register(self, check: DoctorCheck) -> None:
        """Register *check*.

        Raises:
            ValueError: If a check with the same id is already registered.
                Reserved ids (see ``RESERVED_CHECK_IDS``) are *not* registered
                by doctor itself, so the owning subsystem can claim them
                without conflict.
        """
        if check.id in self._checks:
            raise ValueError(f"duplicate doctor check id: {check.id!r}")
        self._checks[check.id] = check

    def unregister(self, check_id: str) -> bool:
        """Remove a check.  Returns True when something was removed."""
        return self._checks.pop(check_id, None) is not None

    def checks(self) -> list[DoctorCheck]:
        """All registered checks, ordered by id."""
        return [self._checks[k] for k in sorted(self._checks)]

    def get(self, check_id: str) -> DoctorCheck | None:
        return self._checks.get(check_id)

    def ids(self) -> list[str]:
        return sorted(self._checks)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._checks)

    def __contains__(self, check_id: object) -> bool:
        return check_id in self._checks


def exit_code_for(results: list[CheckResult]) -> int:
    """Map result severities to a CI exit code (design §5.6).

    errors → 2, else warns → 1, else 0.  The CLI maps a runner crash to 3.
    """
    worst = max((SEVERITY_RANK[r.severity] for r in results), default=0)
    if worst >= SEVERITY_RANK[Severity.ERROR]:
        return 2
    if worst >= SEVERITY_RANK[Severity.WARN]:
        return 1
    return 0


async def _run_one(check: DoctorCheck, ctx: DoctorContext) -> CheckResult:
    """Run one check with its own timeout, never raising."""
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(check.run(ctx), timeout=check.timeout_s)
    except asyncio.TimeoutError:
        return CheckResult(
            id=check.id,
            severity=Severity.ERROR,
            detail=f"check failed: timed out after {check.timeout_s:g}s",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except asyncio.CancelledError:  # pragma: no cover - propagate shutdown
        raise
    except Exception as exc:
        logger.debug("doctor check %s raised", check.id, exc_info=True)
        return CheckResult(
            id=check.id,
            severity=Severity.ERROR,
            detail=f"check failed: {type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if not isinstance(result, CheckResult):  # defensive: a badly behaved check
        return CheckResult(
            id=check.id,
            severity=Severity.ERROR,
            detail=f"check failed: returned {type(result).__name__}, expected CheckResult",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if not result.duration_ms:
        result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


async def _apply_fix(check: DoctorCheck, ctx: DoctorContext) -> CheckResult:
    """Run a check's fix then re-run the check, reporting the post-fix state."""
    try:
        await asyncio.wait_for(check.fix(ctx), timeout=check.timeout_s)  # type: ignore[misc]
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except Exception as exc:
        logger.debug("doctor fix %s raised", check.id, exc_info=True)
        return CheckResult(
            id=check.id,
            severity=Severity.ERROR,
            detail=f"fix failed: {type(exc).__name__}: {exc}",
            fixable=True,
        )
    post = await _run_one(check, ctx)
    post.fixable = True
    post.fix_applied = True
    return post


def _reserved_placeholders(registered: set[str], only: set[str] | None) -> list[CheckResult]:
    """INFO results for reserved ids nobody has registered yet."""
    out: list[CheckResult] = []
    for check_id, owner in sorted(RESERVED_CHECK_IDS.items()):
        if check_id in registered:
            continue
        if only is not None and check_id not in only:
            continue
        out.append(
            CheckResult(
                id=check_id,
                severity=Severity.INFO,
                detail="check not registered (subsystem not enabled)",
                data={"owner": owner, "reserved": True},
            )
        )
    return out


async def run_doctor(
    registry: DoctorRegistry,
    ctx: DoctorContext,
    *,
    fix: bool = False,
    only: list[str] | None = None,
) -> dict:
    """Run all (or *only*) checks concurrently and summarise the outcome.

    When ``fix=True``, any check that came back WARN or ERROR *and* declares a
    ``fix`` has its fix awaited, then the check is re-run; the post-fix result
    is reported with ``fix_applied=True``.

    An *only* entry that matches neither a registered check nor a reserved id
    is reported as an ERROR result for that id (exit code 2).  A CI gate
    pinned to a misspelled check id must fail, not pass against an empty
    table.

    Returns ``{"checks": [...], "summary": {...}, "exit_code": 0|1|2}``.
    """
    only_set = set(only) if only else None
    selected = [c for c in registry.checks() if only_set is None or c.id in only_set]

    results: list[CheckResult] = []
    if selected:
        results = list(await asyncio.gather(*(_run_one(c, ctx) for c in selected)))

    fixes_applied = 0
    if fix and selected:
        by_id = {c.id: c for c in selected}
        # ``r.id`` is normally ``check.id``, but a check is free to return a
        # different id (plugin checks especially).  Look it up defensively —
        # a mismatch must not turn ``--fix`` into a KeyError.
        fix_targets = [
            r
            for r in results
            if r.severity in (Severity.WARN, Severity.ERROR)
            and getattr(by_id.get(r.id), "fix", None) is not None
        ]
        if fix_targets:
            fixed = await asyncio.gather(
                *(_apply_fix(by_id[r.id], ctx) for r in fix_targets)
            )
            fixed_by_id = {r.id: r for r in fixed}
            results = [fixed_by_id.get(r.id, r) for r in results]
            fixes_applied = len(fixed)

    # Mark fixability even when we didn't fix, so the CLI can hint at --fix.
    fixable_ids = {c.id for c in selected if c.fix is not None}
    for r in results:
        if r.id in fixable_ids:
            r.fixable = True

    registered_ids = {c.id for c in registry.checks()}
    results.extend(_reserved_placeholders(registered_ids, only_set))

    if only_set:
        unknown = sorted(only_set - registered_ids - set(RESERVED_CHECK_IDS))
        results.extend(
            CheckResult(
                id=check_id,
                severity=Severity.ERROR,
                detail=(
                    "unknown check id — not registered and not reserved "
                    f"(known: {', '.join(sorted(registered_ids)) or 'none'})"
                ),
                data={"unknown": True},
            )
            for check_id in unknown
        )

    results.sort(key=lambda r: r.id)

    summary = {
        "ok": sum(1 for r in results if r.severity is Severity.OK),
        "info": sum(1 for r in results if r.severity is Severity.INFO),
        "warn": sum(1 for r in results if r.severity is Severity.WARN),
        "error": sum(1 for r in results if r.severity is Severity.ERROR),
        "fixes_applied": fixes_applied,
    }
    return {
        "checks": [r.to_dict() for r in results],
        "summary": summary,
        "exit_code": exit_code_for(results),
    }
