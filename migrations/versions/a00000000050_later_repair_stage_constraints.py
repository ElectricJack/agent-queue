"""Let candidate resolutions and ref mutations record any retained repair stage.

``a00000000048`` let a repair operation keep exhausted stages and allocate
successors beyond ordinal one, but the two columns that copy the active stage
kept their two-stage checks, so a stage-2 root promotion or candidate repair
was refused by PostgreSQL. See
docs/superpowers/specs/2026-10-01-later-repair-stage-constraints-design.md.

``a00000000049`` is already taken by the in-flight repair-ejection revision on
the live candidate; join the two with a merge revision, never a re-chain.

Revision ID: a00000000050
Revises: a00000000048
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000050"
down_revision = "a00000000048"
branch_labels = None
depends_on = None

#: (table, column, constraint) for every column that copies the active stage.
_STAGE_CHECKS = (
    (
        "integration_candidate_resolutions",
        "stage_ordinal",
        "ck_integration_candidate_resolutions_stage",
    ),
    (
        "integration_candidate_ref_mutations",
        "operation_stage",
        "ck_integration_candidate_ref_mutations_stage",
    ),
)


def _normalized(sqltext: str) -> str:
    return sqltext.replace(" ", "").strip("()")


def upgrade() -> None:
    bind = op.get_bind()
    for table, column, name in _STAGE_CHECKS:
        # The squashed baseline is constructed from current live metadata.
        inspector = sa.inspect(bind)
        if not inspector.has_table(table):
            continue
        current = next(
            (item for item in inspector.get_check_constraints(table) if item["name"] == name),
            None,
        )
        if current is not None and _normalized(current["sqltext"]) == f"{column}>=0":
            continue
        if current is not None:
            op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} >= 0")


def downgrade() -> None:
    # Retained successor-stage evidence cannot fit the old two-stage schema.
    bind = op.get_bind()
    for table, column, _name in _STAGE_CHECKS:
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {column} > 1)")):
            raise RuntimeError(
                f"Cannot downgrade while {table} records a repair stage above 1"
            )
    for table, column, name in _STAGE_CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} IN (0, 1)")
