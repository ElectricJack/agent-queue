"""Integration history names tasks by id, not by foreign key.

Revision ID: a00000000011
Revises: a00000000010

Four integration history tables referenced ``tasks.id`` with RESTRICT/NO
ACTION.  ``archive_task`` and ``delete_task`` both remove the ``tasks`` row, so
a completed root with an episode — or a delegate of an operation that was
cancelled months ago — could be neither archived nor deleted, and the operator
saw a bare ``ForeignKeyViolationError`` naming one constraint at a time.

Liveness is now enforced by ``guard_integration_mutation``, which can name the
operation, its state and the command that releases it.  The columns and their
values are untouched: history still names the task.

See docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000011"
down_revision = "a00000000010"
branch_labels = None
depends_on = None

#: ``(table, constraint)`` pairs whose referenced side is ``tasks.id``.
_TASK_REFERENCES = (
    ("integration_candidate_resolutions", "fk_integration_candidate_resolutions_task"),
    ("integration_repair_operations", "fk_integration_repair_operations_verifier_task"),
    ("integration_parent_episodes", "fk_integration_parent_episodes_parent_task"),
    ("integration_parent_verifications", "fk_integration_parent_verifications_parent_task"),
)


def _existing_constraints(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in set(inspector.get_table_names()):
        return set()
    return {
        fk["name"]
        for fk in inspector.get_foreign_keys(table)
        if fk.get("name")
    }


def upgrade() -> None:
    from src.database.tables import integration_delegate_releases

    bind = op.get_bind()
    for table, constraint in _TASK_REFERENCES:
        if constraint in _existing_constraints(bind, table):
            op.drop_constraint(constraint, table, type_="foreignkey")
    if "integration_delegate_releases" not in set(sa.inspect(bind).get_table_names()):
        integration_delegate_releases.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "integration_delegate_releases" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("integration_delegate_releases")
    # The constraints are deliberately not restored: a database that has run
    # this revision may hold history rows naming an archived or deleted task,
    # and re-adding the foreign key would fail on exactly the rows the
    # revision exists to permit.
