"""Receipt the Playbook V2 attempt-start fence.

Revision ID: d3f2c0de0005
Revises: c3f2c0de0004

The engine fences a ``CommandStep``/``LlmStep``/``AgentTaskStep`` attempt
before its first external side effect.  That fence used to be a bare CAS
write with no receipt; it is now an ordinary ``commit_boundary`` whose
receipt has ``receipt_kind='attempt_start'`` and ``outcome='started'``,
so every snapshot version keeps exactly one receipt.  ``turn_index`` on such
a receipt is the zero-based start ordinal of the attempt identity, which is
what keeps ``uq_playbook_step_receipts_boundary`` satisfied when a replay or
operator retry deliberately reuses the attempt number.  Existing rows are
untouched; the two CHECK constraints simply admit the new values.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f2c0de0005"
down_revision: str | Sequence[str] | None = "c3f2c0de0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OUTCOMES_BEFORE = (
    "outcome IN ('success', 'failure', 'skipped', 'timeout', 'cancelled', "
    "'operator_decision_required')"
)
_OUTCOMES_AFTER = (
    "outcome IN ('success', 'failure', 'skipped', 'timeout', 'cancelled', "
    "'operator_decision_required', 'started')"
)
_KINDS_BEFORE = (
    "receipt_kind IN ('step', 'tool_turn', 'llm_call', 'interrupted', "
    "'operator_decision')"
)
_KINDS_AFTER = (
    "receipt_kind IN ('step', 'tool_turn', 'llm_call', 'interrupted', "
    "'operator_decision', 'attempt_start')"
)


def upgrade() -> None:
    with op.batch_alter_table("playbook_step_receipts") as batch:
        batch.drop_constraint("ck_playbook_step_receipts_outcome", type_="check")
        batch.drop_constraint("ck_playbook_step_receipts_kind", type_="check")
        batch.create_check_constraint("ck_playbook_step_receipts_outcome", _OUTCOMES_AFTER)
        batch.create_check_constraint("ck_playbook_step_receipts_kind", _KINDS_AFTER)


def downgrade() -> None:
    connection = op.get_bind()
    fenced = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM playbook_step_receipts "
            "WHERE receipt_kind = 'attempt_start' OR outcome = 'started'"
        )
    ).scalar_one()
    if fenced:
        raise RuntimeError(
            "cannot downgrade while attempt_start playbook receipts exist; "
            "retain or explicitly remove them first"
        )
    with op.batch_alter_table("playbook_step_receipts") as batch:
        batch.drop_constraint("ck_playbook_step_receipts_kind", type_="check")
        batch.drop_constraint("ck_playbook_step_receipts_outcome", type_="check")
        batch.create_check_constraint("ck_playbook_step_receipts_kind", _KINDS_BEFORE)
        batch.create_check_constraint("ck_playbook_step_receipts_outcome", _OUTCOMES_BEFORE)
