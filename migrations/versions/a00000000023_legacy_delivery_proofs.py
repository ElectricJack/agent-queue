"""Admit re-delivered, superseded and abandoned legacy delivery proofs.

Revision ID: a00000000023
Revises: a00000000022

``aq integration adopt-legacy-deliveries`` could prove a child only by
ancestry.  A child whose work reached the default branch under another commit
(cherry-picked or re-delivered) is now proven by ``content_equivalent`` (its
merge into the default branch changes nothing), a supervisor may attest the
re-delivering commit (``superseded``), and a child whose work was abandoned is
retired (``abandoned``, no delivered commit) without deleting anything.

**Idempotent**: the squashed baseline builds its constraints from the live
``src.database.tables.metadata``, so a fresh database already has the widened
checks.  Each is dropped if present and recreated from the declared text.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000023"
down_revision = "a00000000022"
branch_labels = None
depends_on = None

_TABLE = "integration_legacy_deliveries"
_PROOF = "ck_integration_legacy_deliveries_proof"
_DELIVERED = "ck_integration_legacy_deliveries_delivered_sha"

_UPGRADED = {
    _PROOF: (
        "proof IN ('development_delivery', 'branch_tip', 'content_equivalent', "
        "'superseded', 'operator_accepted', 'abandoned')"
    ),
    _DELIVERED: "proof IN ('operator_accepted', 'abandoned') OR delivered_sha IS NOT NULL",
}
_ORIGINAL = {
    _PROOF: "proof IN ('development_delivery', 'branch_tip', 'operator_accepted')",
    _DELIVERED: "proof = 'operator_accepted' OR delivered_sha IS NOT NULL",
}


def _replace(conditions: dict[str, str]) -> None:
    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    present = {c["name"] for c in sa.inspect(bind).get_check_constraints(_TABLE)}
    for name, condition in conditions.items():
        if name in present:
            op.drop_constraint(name, _TABLE, type_="check")
        op.create_check_constraint(name, _TABLE, sa.text(condition))


def upgrade() -> None:
    _replace(_UPGRADED)


def downgrade() -> None:
    # Rows recorded with a new proof make the narrower check fail: the
    # downgrade refuses rather than deleting audit records.
    _replace(_ORIGINAL)
