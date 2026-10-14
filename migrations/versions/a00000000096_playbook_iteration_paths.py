"""Full foreach paths in durable receipts and waits; preserve legacy keys."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000096"
down_revision = "a00000000095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # A partial or restored schema may lack either table: nothing to widen there.
    present = {
        table
        for table in ("playbook_step_receipts", "playbook_waits")
        if sa.inspect(bind).has_table(table)
    }
    for table in sorted(present):
        if "iteration_path" not in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
            op.add_column(
                table, sa.Column("iteration_path", sa.Text(), nullable=False, server_default="")
            )

    table = "playbook_step_receipts"
    name = "uq_playbook_step_receipts_boundary"
    columns = [
        "run_id",
        "step_id",
        "iteration",
        "iteration_path",
        "attempt",
        "turn_index",
        "receipt_kind",
    ]
    existing = (
        {c["name"]: c["column_names"] for c in sa.inspect(bind).get_unique_constraints(table)}
        if table in present
        else {}
    )
    if table in present and existing.get(name) != columns:
        if name in existing:
            op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, columns)

    for table, name, columns, unique in (
        (
            "playbook_step_receipts",
            "idx_playbook_step_receipts_turn",
            ["run_id", "step_id", "iteration", "iteration_path", "attempt", "turn_index"],
            False,
        ),
        (
            "playbook_waits",
            "uq_playbook_waits_active_step",
            ["run_id", "step_id", "iteration", "iteration_path"],
            True,
        ),
    ):
        if table not in present:
            continue
        indexes = {i["name"]: i["column_names"] for i in sa.inspect(bind).get_indexes(table)}
        if indexes.get(name) != columns:
            if name in indexes:
                op.drop_index(name, table_name=table)
            kwargs = {"postgresql_where": sa.text("state = 'active'")} if unique else {}
            op.create_index(name, table, columns, unique=unique, **kwargs)


def downgrade() -> None:
    # Old executors must not resume nested artifacts. Retain additive evidence.
    pass
