"""Retire the unused legacy gate applicability control table.

No command ever wrote or read ``integration_legacy_gate_applicability``. The
table is dropped only while it is empty; a database holding rows keeps it as
historical audit rather than losing history.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000119"
down_revision = "a00000000118"
branch_labels = depends_on = None

TABLE = "integration_legacy_gate_applicability"
TRIGGER = "trg_integration_legacy_gate_applicability_immutable"


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE})")).scalar():
        return
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}")
    op.drop_table(TABLE)


def downgrade():
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        *[sa.Column(name, sa.Text(), nullable=False) for name in (
            "project_id", "gate_id", "waiver_id", "transition_id", "blocker_digest")],
        sa.Column("applicable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint(
            "project_id", "gate_id", name="pk_integration_legacy_gate_applicability"),
        *[sa.ForeignKeyConstraint([column], [target], name=f"fk_{TABLE}_{suffix}",
                                  ondelete="RESTRICT")
          for column, target, suffix in (
              ("project_id", "projects.id", "project"),
              ("gate_id", "gates.id", "gate"),
              ("waiver_id", "integration_history_waivers.id", "waiver"),
              ("transition_id", "integration_rollout_transitions.id", "transition"))],
        sa.CheckConstraint(
            "length(blocker_digest) = 71 AND blocker_digest LIKE 'sha256:%'",
            name="ck_integration_legacy_gate_applicability_blocker_digest",
        ),
    )
    op.execute(
        f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION integration_control_history_immutable()"
    )
