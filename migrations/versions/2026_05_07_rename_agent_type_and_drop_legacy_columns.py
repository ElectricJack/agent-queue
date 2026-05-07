"""rename agents.agent_type to profile_id; drop legacy agent_type columns

Revision ID: e4f2a8b1d6c9
Revises: d8e4b2c5f1a7
Create Date: 2026-05-07 00:00:00.000000

OPERATIONAL NOTE
================

Four operations, each idempotent (inspects the schema first):

1. Rename ``agents.agent_type`` → ``agents.profile_id``.  Always *was*
   the profile id by string match in actual usage.

2. Drop ``tasks.agent_type``.  Coordination-category-filter never
   implemented; live DB has 1 task with NULL.

3. Drop ``archived_tasks.agent_type`` for symmetry.

4. Drop ``projects.default_agent_type``.  Was used as the
   project-scoped-profile lookup key; ``_resolve_profile`` is rewritten
   to use ``profile_id`` directly, making this column dead.

See docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4f2a8b1d6c9'
down_revision: Union[str, Sequence[str], None] = 'd8e4b2c5f1a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, col: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return col in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("agents", "agent_type") and not _has_column("agents", "profile_id"):
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.alter_column("agent_type", new_column_name="profile_id")
    if _has_column("tasks", "agent_type"):
        with op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.drop_column("agent_type")
    if _has_column("archived_tasks", "agent_type"):
        with op.batch_alter_table("archived_tasks", schema=None) as batch_op:
            batch_op.drop_column("agent_type")
    if _has_column("projects", "default_agent_type"):
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.drop_column("default_agent_type")


def downgrade() -> None:
    if not _has_column("projects", "default_agent_type"):
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.add_column(sa.Column("default_agent_type", sa.Text(), nullable=True))
    if not _has_column("archived_tasks", "agent_type"):
        with op.batch_alter_table("archived_tasks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("agent_type", sa.Text(), nullable=True))
    if not _has_column("tasks", "agent_type"):
        with op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("agent_type", sa.Text(), nullable=True))
    if _has_column("agents", "profile_id") and not _has_column("agents", "agent_type"):
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.alter_column("profile_id", new_column_name="agent_type")
