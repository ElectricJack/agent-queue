"""human resume re-arms repair stage attempts

Revision ID: a12a5e1e4f05
Revises: a11a5e1e4f04
Create Date: 2026-09-07

Revision 3f30b34c7e7c made ``integration_repair_stages.attempts`` monotone:
consumed repair attempts never decrease.  The operator controls that landed
later (``IntegrationRecoveryControls.resume``) re-arm a ``failed`` /
``expired`` / ``cancelled`` stage after a human repaired the branch: the stage
goes back to ``active`` with a fresh deadline and ``attempts = 0``, which is
the only way the resumed stage is not immediately reported as budget-exhausted
again.  On PostgreSQL the original guard rejected that write; on SQLite the
guard had been silently dropped by an earlier batch rebuild, which is why it
was never noticed.

This revision narrows the guard to the one transition the design allows: a
decrease is permitted only when a terminal stage is re-armed to ``active``.
Every other update keeps the monotone rule.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

from alembic import op

revision: str = "a12a5e1e4f05"
down_revision: str | Sequence[str] | None = "a11a5e1e4f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TRIGGER = "trg_integration_repair_attempts_monotone"
_FUNCTION = "integration_repair_attempts_monotone"
_MESSAGE = "integration repair attempts cannot decrease"
_TERMINAL_STATES = "('failed', 'expired', 'cancelled')"
_REARM = f"(OLD.state IN {_TERMINAL_STATES} AND NEW.state = 'active')"


def _sqlite_original_ddl() -> str:
    origin = importlib.import_module(
        "migrations.versions.3f30b34c7e7c_hierarchical_integration_state"
    )
    for trigger, table, condition, message in origin._SQLITE_MONOTONE_GUARDS:
        if trigger == _TRIGGER:
            return origin._sqlite_monotone_guard_ddl(trigger, table, condition, message)
    raise RuntimeError(f"{_TRIGGER} missing from 3f30b34c7e7c guard table")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
            BEGIN
                IF NEW.attempts < OLD.attempts AND NOT {_REARM} THEN
                    RAISE EXCEPTION '{_MESSAGE}';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER}")
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON integration_repair_stages "
        f"WHEN NEW.attempts < OLD.attempts AND NOT {_REARM} "
        f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
            BEGIN
                IF NEW.attempts < OLD.attempts THEN RAISE EXCEPTION '{_MESSAGE}'; END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER}")
    op.execute(_sqlite_original_ddl())
