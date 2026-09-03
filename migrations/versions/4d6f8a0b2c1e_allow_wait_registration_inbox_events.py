"""allow wait registration inbox events

Revision ID: 4d6f8a0b2c1e
Revises: c5d7e9f1a3b4
Create Date: 2026-09-02

The existing pending-event table is also the durable inbox named by the
Playbook V2 wait design.  Widen its reason constraint so resolved delivery
rows can be distinguished from events retained behind unhealthy activations.
"""

from alembic import op
import sqlalchemy as sa


revision = "4d6f8a0b2c1e"
down_revision = "c5d7e9f1a3b4"
branch_labels = None
depends_on = None

_OLD_REASON_CHECK = (
    "reason IN ('stale_contract', 'invalid_artifact', 'disabled', "
    "'unavailable', 'question_required')"
)
_NEW_REASON_CHECK = (
    "reason IN ('stale_contract', 'invalid_artifact', 'disabled', "
    "'unavailable', 'question_required', 'wait_registration')"
)


def _replace_reason_check(predicate: str) -> None:
    with op.batch_alter_table("playbook_pending_events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_playbook_pending_events_reason", type_="check")
        batch_op.create_check_constraint("ck_playbook_pending_events_reason", predicate)


def upgrade() -> None:
    _replace_reason_check(_NEW_REASON_CHECK)


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM playbook_pending_events WHERE reason = 'wait_registration'")
    )
    _replace_reason_check(_OLD_REASON_CHECK)
