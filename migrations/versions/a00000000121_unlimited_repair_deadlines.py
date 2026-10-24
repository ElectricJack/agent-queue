"""Allow resolution reservations to retain an unlimited repair stage deadline."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000121"
down_revision = "a00000000120"
branch_labels = None
depends_on = None

_TABLE = "integration_candidate_resolutions"
_COLUMN = "stage_deadline_at"
_BUDGET_TABLE = "integration_subjects"
_BUDGET_CONSTRAINT = "ck_integration_subjects_budget"


def _budget_constraint(*, unlimited: bool):
    schema = sa.inspect(op.get_bind())
    if not schema.has_table(_BUDGET_TABLE):
        return
    existing = next(
        (c for c in schema.get_check_constraints(_BUDGET_TABLE)
         if c["name"] == _BUDGET_CONSTRAINT),
        None,
    )
    if existing:
        requires_deadline = "budget_deadline_at IS NOT NULL" in existing["sqltext"]
        if requires_deadline != unlimited:
            return
        op.drop_constraint(_BUDGET_CONSTRAINT, _BUDGET_TABLE, type_="check")
    clock = (
        "(budget_deadline_at IS NULL OR budget_deadline_at >= budget_started_at)"
        if unlimited else
        "budget_deadline_at IS NOT NULL AND budget_deadline_at >= budget_started_at"
    )
    op.create_check_constraint(
        _BUDGET_CONSTRAINT,
        _BUDGET_TABLE,
        "(budget_ordinal IS NULL AND budget_class IS NULL AND budget_started_at IS NULL "
        "AND budget_deadline_at IS NULL AND budget_attempt_limit IS NULL "
        "AND budget_attempts = 0) OR "
        "(budget_ordinal IS NOT NULL AND budget_ordinal >= 0 AND budget_class IS NOT NULL "
        "AND budget_started_at IS NOT NULL AND " + clock + ")",
    )


def upgrade():
    schema = sa.inspect(op.get_bind())
    if schema.has_table(_TABLE):
        column = next(c for c in schema.get_columns(_TABLE) if c["name"] == _COLUMN)
        if not column["nullable"]:
            op.alter_column(_TABLE, _COLUMN, existing_type=sa.Float(), nullable=True)
    _budget_constraint(unlimited=True)


def downgrade():
    # NULL is part of an immutable reservation identity. Do not invent a
    # deadline or destroy an unlimited reservation to restore the old schema.
    schema = sa.inspect(op.get_bind())
    if schema.has_table(_TABLE):
        column = next(c for c in schema.get_columns(_TABLE) if c["name"] == _COLUMN)
        if column["nullable"]:
            op.alter_column(_TABLE, _COLUMN, existing_type=sa.Float(), nullable=False)
    _budget_constraint(unlimited=False)
