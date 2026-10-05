"""Allow a supervisor-authored digest window to hold one author request.

Revision ID: a00000000070
Revises: a00000000063

The supervisor-authored digest (spec 2026-10-03 §4, phase P3) reuses the durable
supervisor author-request lifecycle that morning and hourly reports already own:
one reserved row per digest window, the frozen facts as its brief, the
deterministic digest as its fallback text, and a deadline at which the daemon posts
the fallback itself.  Nothing about the request's shape changes -- only the set of
``kind`` values the named check constraint admits gains ``'digest'``.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000070"
down_revision = "a00000000069"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_supervisor_report_requests_kind"
_DIGEST_ROWS = (
    "SELECT EXISTS (SELECT 1 FROM supervisor_report_requests WHERE kind = 'digest')"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("supervisor_report_requests"):
        return
    existing = {c["name"]: c for c in inspector.get_check_constraints("supervisor_report_requests")}
    current = existing.get(_CONSTRAINT)
    # Named constraints are what autogenerate matches on; an unnamed one yields a
    # spurious drop/add pair on every later run, so name it if it is missing.
    if current is None or "digest" in str(current.get("sqltext") or ""):
        return
    op.drop_constraint(_CONSTRAINT, "supervisor_report_requests", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "supervisor_report_requests",
        "kind IN ('hourly','morning','digest')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("supervisor_report_requests"):
        return
    if bind.execute(sa.text(_DIGEST_ROWS)).scalar_one():
        raise RuntimeError(
            "supervisor-authored digest requests exist; close their windows before rollback"
        )
    existing = {c["name"] for c in inspector.get_check_constraints("supervisor_report_requests")}
    if _CONSTRAINT not in existing:
        return
    op.drop_constraint(_CONSTRAINT, "supervisor_report_requests", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "supervisor_report_requests", "kind IN ('hourly','morning')"
    )
