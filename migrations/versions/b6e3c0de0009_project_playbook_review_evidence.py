"""Persist exact-artifact review evidence for project playbook activations.

Revision ID: b6e3c0de0009
Revises: a5d2c0de0008

Checked-in review fixtures remain the authority for shipped playbooks.  A
project playbook has no repository fixture, so its successful activation now
records the exact approved artifact hash, the server-derived reviewer, and the
decision time on the activation row.  The check constraint makes the evidence
all-or-none, project-only, and inseparable from the active bytes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e3c0de0009"
down_revision: str | Sequence[str] | None = "a5d2c0de0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REVIEW_EVIDENCE = (
    "(reviewed_artifact_sha256 IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL) "
    "OR (scope = 'project' AND active_artifact_sha256 IS NOT NULL "
    "AND reviewed_artifact_sha256 IS NOT NULL AND reviewed_by IS NOT NULL "
    "AND reviewed_at IS NOT NULL "
    "AND reviewed_artifact_sha256 = active_artifact_sha256 "
    "AND length(trim(reviewed_by)) > 0)"
)


def upgrade() -> None:
    with op.batch_alter_table("playbook_activations") as batch:
        batch.add_column(sa.Column("reviewed_artifact_sha256", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reviewed_by", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reviewed_at", sa.Float(), nullable=True))
        batch.create_check_constraint(
            "ck_playbook_activations_review_evidence", _REVIEW_EVIDENCE
        )


def downgrade() -> None:
    with op.batch_alter_table("playbook_activations") as batch:
        batch.drop_constraint("ck_playbook_activations_review_evidence", type_="check")
        batch.drop_column("reviewed_at")
        batch.drop_column("reviewed_by")
        batch.drop_column("reviewed_artifact_sha256")
