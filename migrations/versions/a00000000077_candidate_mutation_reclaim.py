"""Retain superseded candidate ref reservations as well as root-main claims.

Revision ID: a00000000077
Revises: a00000000076
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000077"
down_revision = "a00000000076"
branch_labels = None
depends_on = None

TABLE = "integration_candidate_ref_mutations"
CONSTRAINT = "ck_integration_candidate_ref_mutations_remote"
CURRENT = (
    "(state = 'reserved' AND remote_sha IS NULL) OR "
    "(state = 'applied' AND remote_sha = desired_sha) OR "
    "(state = 'superseded' AND remote_sha IS NULL)"
)
PREVIOUS = CURRENT.replace(
    "state = 'superseded'", "state = 'superseded' AND purpose = 'root_main'"
)
TERMINAL_FUNCTION = "integration_candidate_supersession_is_terminal"
TERMINAL_TRIGGER = "trg_candidate_supersession_terminal"


def _replace(sqltext: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return
    if any(check["name"] == CONSTRAINT for check in inspector.get_check_constraints(TABLE)):
        op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, sqltext)


def upgrade() -> None:
    _replace(CURRENT)
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    # The baseline snapshot remains immutable. Add a guard for the newly
    # permitted tombstones without changing root-main's existing protocol.
    op.execute(sa.text(f"""
        CREATE OR REPLACE FUNCTION {TERMINAL_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF OLD.state = 'superseded' AND OLD.purpose <> 'root_main'
                AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'superseded candidate mutation is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {TERMINAL_TRIGGER} ON {TABLE}"))
    op.execute(sa.text(
        f"CREATE TRIGGER {TERMINAL_TRIGGER} BEFORE UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {TERMINAL_FUNCTION}()"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if bind.scalar(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM {TABLE} "
        "WHERE state = 'superseded' AND purpose <> 'root_main')"
    )):
        raise RuntimeError("Cannot downgrade while superseded candidate mutation evidence exists")
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {TERMINAL_TRIGGER} ON {TABLE}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {TERMINAL_FUNCTION}()"))
    _replace(PREVIOUS)
