"""Restore procedural guards omitted by the original squashed baseline.

Revision ID: a00000000002
Revises: a00000000001

The installer is idempotent: fresh databases already get these guards from
the corrected baseline, while databases created by the initial squash need
this revision to receive them. Legacy databases keep their existing semantics.
"""

from alembic import op

from migrations.integration_guards import install_integration_guards

revision = "a00000000002"
down_revision = "a00000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    install_integration_guards(op.get_bind())


def downgrade() -> None:
    # The corrected baseline includes these guards; downgrading must retain them.
    pass
