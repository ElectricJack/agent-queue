"""Classify a PostgreSQL integrity error by SQLSTATE and constraint.

Only a ``unique_violation`` can mean another writer inserted the same row
first.  A check, foreign-key or not-null violation is the database refusing
the row itself, and treating it as a lost race hides the constraint that
refused (the 2026-10-01 stage-2 root promotion outage, see
docs/superpowers/specs/2026-10-01-later-repair-stage-constraints-design.md).
"""

from __future__ import annotations

from dataclasses import dataclass

UNIQUE_VIOLATION = "23505"

_CONDITION_NAMES = {
    "23502": "not_null_violation",
    "23503": "foreign_key_violation",
    UNIQUE_VIOLATION: "unique_violation",
    "23514": "check_violation",
    "23P01": "exclusion_violation",
}


@dataclass(frozen=True)
class IntegrityViolation:
    """What the driver reported: SQLSTATE and constraint, either possibly unknown."""

    sqlstate: str | None
    constraint: str | None

    @property
    def is_unique(self) -> bool:
        return self.sqlstate == UNIQUE_VIOLATION

    def describe(self) -> str:
        """``check_violation (SQLSTATE 23514) on ck_…`` — one line for errors and logs."""
        condition = _CONDITION_NAMES.get(self.sqlstate or "", "integrity violation")
        state = f" (SQLSTATE {self.sqlstate})" if self.sqlstate else ""
        target = f" on {self.constraint}" if self.constraint else ""
        return f"{condition}{state}{target}"


def integrity_violation(exc: BaseException) -> IntegrityViolation:
    """The SQLSTATE and constraint asyncpg reports, wherever SQLAlchemy buried them.

    SQLAlchemy wraps the driver error twice: ``IntegrityError.orig`` is the
    asyncpg *adapter*'s exception, and only its ``__cause__`` is the real
    ``asyncpg.exceptions`` error that carries ``constraint_name``.  Walk the
    whole wrapping chain and take the first value of each.
    """
    sqlstate: str | None = None
    constraint: str | None = None
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and (sqlstate is None or constraint is None):
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if sqlstate is None and getattr(current, "sqlstate", None):
            sqlstate = str(current.sqlstate)
        if constraint is None and getattr(current, "constraint_name", None):
            constraint = str(current.constraint_name)
        for link in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(link, BaseException):
                pending.append(link)
    return IntegrityViolation(sqlstate=sqlstate, constraint=constraint)
