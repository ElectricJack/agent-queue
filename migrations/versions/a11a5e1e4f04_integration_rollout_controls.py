"""integration rollout control persistence

Revision ID: a11a5e1e4f04
Revises: a10c5e1e4f03
Create Date: 2026-09-06 23:15:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "a11a5e1e4f04"
down_revision: str | Sequence[str] | None = "a10c5e1e4f03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES = "('disabled', 'observe', 'hierarchy', 'train')"
_IMMUTABLE_TABLES = (
    "integration_history_waivers",
    "integration_rollout_transitions",
    "integration_history_waiver_consumptions",
    "integration_legacy_gate_applicability",
)


def _create_tables() -> None:
    op.create_table(
        "integration_history_waivers",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("operator_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("blocker_digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_integration_history_waivers"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_history_waivers_project", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(operator_id) > 0", name="ck_integration_history_waivers_operator"
        ),
        sa.CheckConstraint("length(reason) > 0", name="ck_integration_history_waivers_reason"),
        sa.CheckConstraint(
            "length(blocker_digest) = 71 AND blocker_digest LIKE 'sha256:%'",
            name="ck_integration_history_waivers_blocker_digest",
        ),
    )
    op.create_table(
        "integration_rollout_transitions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("old_effective_mode", sa.Text(), nullable=False),
        sa.Column("new_effective_mode", sa.Text(), nullable=False),
        sa.Column("old_desired_mode", sa.Text(), nullable=False),
        sa.Column("new_desired_mode", sa.Text(), nullable=False),
        sa.Column("draining", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("operator_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("blocker_digest", sa.Text(), nullable=False),
        sa.Column("old_legacy_policy", sa.JSON(), nullable=False),
        sa.Column("new_legacy_policy", sa.JSON(), nullable=False),
        sa.Column("waiver_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_integration_rollout_transitions"),
        sa.UniqueConstraint(
            "project_id", "generation", name="uq_integration_rollout_transitions_generation"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_rollout_transitions_project", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["waiver_id"], ["integration_history_waivers.id"],
            name="fk_integration_rollout_transitions_waiver", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "generation > 0", name="ck_integration_rollout_transitions_generation"
        ),
        sa.CheckConstraint(
            f"old_effective_mode IN {_MODES} AND new_effective_mode IN {_MODES} AND "
            f"old_desired_mode IN {_MODES} AND new_desired_mode IN {_MODES}",
            name="ck_integration_rollout_transitions_modes",
        ),
        sa.CheckConstraint(
            "length(operator_id) > 0", name="ck_integration_rollout_transitions_operator"
        ),
        sa.CheckConstraint(
            "length(reason) > 0", name="ck_integration_rollout_transitions_reason"
        ),
        sa.CheckConstraint(
            "length(blocker_digest) = 71 AND blocker_digest LIKE 'sha256:%'",
            name="ck_integration_rollout_transitions_blocker_digest",
        ),
    )
    op.create_table(
        "integration_history_waiver_consumptions",
        sa.Column("waiver_id", sa.Text(), nullable=False),
        sa.Column("transition_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("blocker_digest", sa.Text(), nullable=False),
        sa.Column("consumed_by", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("waiver_id", name="pk_integration_history_waiver_consumptions"),
        sa.UniqueConstraint(
            "transition_id", name="uq_integration_history_waiver_consumptions_transition"
        ),
        sa.ForeignKeyConstraint(
            ["waiver_id"], ["integration_history_waivers.id"],
            name="fk_integration_history_waiver_consumptions_waiver", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"], ["integration_rollout_transitions.id"],
            name="fk_integration_history_waiver_consumptions_transition", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_history_waiver_consumptions_project", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(consumed_by) > 0", name="ck_integration_waiver_consumptions_actor"
        ),
        sa.CheckConstraint(
            "length(blocker_digest) = 71 AND blocker_digest LIKE 'sha256:%'",
            name="ck_integration_waiver_consumptions_blocker_digest",
        ),
    )
    op.create_table(
        "integration_legacy_gate_applicability",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("gate_id", sa.Text(), nullable=False),
        sa.Column("waiver_id", sa.Text(), nullable=False),
        sa.Column("transition_id", sa.Text(), nullable=False),
        sa.Column("blocker_digest", sa.Text(), nullable=False),
        sa.Column("applicable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint(
            "project_id", "gate_id", name="pk_integration_legacy_gate_applicability"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_legacy_gate_applicability_project", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gate_id"], ["gates.id"],
            name="fk_integration_legacy_gate_applicability_gate", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["waiver_id"], ["integration_history_waivers.id"],
            name="fk_integration_legacy_gate_applicability_waiver", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"], ["integration_rollout_transitions.id"],
            name="fk_integration_legacy_gate_applicability_transition", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(blocker_digest) = 71 AND blocker_digest LIKE 'sha256:%'",
            name="ck_integration_legacy_gate_applicability_blocker_digest",
        ),
    )
    op.create_table(
        "integration_legacy_suppression",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "merge_sweep_suppressed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "final_review_route_suppressed", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "legacy_gate_creation_suppressed", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", name="pk_integration_legacy_suppression"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_legacy_suppression_project", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_integration_legacy_suppression_generation"
        ),
    )


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_control_history_immutable() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'integration control history is immutable'; END;
            $$ LANGUAGE plpgsql"""
        )
        for table in _IMMUTABLE_TABLES:
            op.execute(
                f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION integration_control_history_immutable()"
            )
    else:
        for table in _IMMUTABLE_TABLES:
            for action in ("update", "delete"):
                op.execute(
                    f"CREATE TRIGGER trg_{table}_{action} BEFORE {action.upper()} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'integration control history is immutable'); END"
                )


def _drop_immutable_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in _IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute("DROP FUNCTION IF EXISTS integration_control_history_immutable()")
    else:
        for table in _IMMUTABLE_TABLES:
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_{action}")


@contextmanager
def _sqlite_fk_suspended():
    """Let SQLite rebuild the referenced ``projects`` table with foreign keys on.

    Batch mode drops and recreates the table; with ``PRAGMA foreign_keys=ON``
    that implicit delete fails against every row that references a project.
    Same pattern as ``a7c91e4d2b63`` and ``882b77dc8495``.
    """
    bind = op.get_bind()
    foreign_keys = (
        bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if foreign_keys:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
    finally:
        if foreign_keys:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    with _sqlite_fk_suspended(), op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column(
                "hierarchical_integration_desired_mode",
                sa.Text(),
                nullable=False,
                server_default="disabled",
            )
        )
        batch.add_column(
            sa.Column(
                "hierarchical_integration_draining",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "hierarchical_integration_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint(
            "ck_projects_hierarchical_integration_desired_mode",
            f"hierarchical_integration_desired_mode IN {_MODES}",
        )
        batch.create_check_constraint(
            "ck_projects_hierarchical_integration_generation",
            "hierarchical_integration_generation >= 0",
        )
    op.execute(
        "UPDATE projects SET hierarchical_integration_desired_mode = "
        "hierarchical_integration_mode"
    )
    _create_tables()
    _create_immutable_guards()


def downgrade() -> None:
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT id FROM projects WHERE hierarchical_integration_draining = true OR "
            "hierarchical_integration_generation <> 0 LIMIT 1"
        )
    ).scalar_one_or_none()
    evidence = None
    for table in (*_IMMUTABLE_TABLES, "integration_legacy_suppression"):
        evidence = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).scalar_one_or_none()
        if evidence is not None:
            break
    if active is not None or evidence is not None:
        identity = active if active is not None else table
        raise RuntimeError(
            f"drain integration rollout/control evidence {identity} before downgrade"
        )

    _drop_immutable_guards()
    op.drop_table("integration_legacy_suppression")
    op.drop_table("integration_legacy_gate_applicability")
    op.drop_table("integration_history_waiver_consumptions")
    op.drop_table("integration_rollout_transitions")
    op.drop_table("integration_history_waivers")
    with _sqlite_fk_suspended(), op.batch_alter_table("projects") as batch:
        batch.drop_constraint(
            "ck_projects_hierarchical_integration_generation", type_="check"
        )
        batch.drop_constraint(
            "ck_projects_hierarchical_integration_desired_mode", type_="check"
        )
        batch.drop_column("hierarchical_integration_generation")
        batch.drop_column("hierarchical_integration_draining")
        batch.drop_column("hierarchical_integration_desired_mode")
