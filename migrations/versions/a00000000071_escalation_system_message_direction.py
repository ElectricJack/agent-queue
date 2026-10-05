"""Widen ``escalation_messages.direction`` with ``system``.

Spec §5.6 records the back-fill sweep's audit trail as
``escalation_messages(direction=system, text="sweep: <rule>")`` so a reader of an
incident's history can see *why* the daemon closed it without anybody speaking.
The table's ``ck_escalation_messages_direction`` allows only ``inbound`` and
``outbound``, so this revision adds the third author.

Nothing is backfilled: every existing row keeps the direction it was written
with, and the widening is purely additive.  It is the only way the three
directions can be read apart -- ``src/escalations/plan.py`` relays *outbound*
rows into the channel thread, an ``inbound`` row is what moves an incident to
§5.2's ``answered`` form, and a ``system`` row is neither, so the audit note
stays in the history and never becomes chatter in the channel.

The constraint is replaced only when its content differs, because the squashed
baseline builds ``escalation_messages`` from the live
``src.database.tables.metadata`` -- a database created after the vocabulary was
widened already carries the three-value constraint, and an unconditional
drop/recreate would churn every fresh database.

Revision ID: a00000000071
Revises: a00000000064
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000071"
down_revision = "a00000000070"
branch_labels = None
depends_on = None

_TABLE = "escalation_messages"
_CONSTRAINT = "ck_escalation_messages_direction"
_WIDENED = "direction IN ('inbound','outbound','system')"
_NARROWED = "direction IN ('inbound','outbound')"


def _existing_constraints(bind) -> dict[str, str]:
    return {
        item["name"]: item["sqltext"]
        for item in sa.inspect(bind).get_check_constraints(_TABLE)
        if item["name"]
    }


def _normalise(sqltext: str) -> str:
    return "".join(str(sqltext or "").lower().split()).replace("'", "")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    current = _existing_constraints(bind).get(_CONSTRAINT)
    if current is not None and _normalise(current) == _normalise(_WIDENED):
        return
    if current is not None:
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WIDENED)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    # Narrowing would reject the sweep's audit rows, and dropping them would
    # delete the record of why those incidents closed.  Refuse instead.
    written = bind.execute(
        sa.text(f"SELECT count(*) FROM \"{_TABLE}\" WHERE direction = 'system'")
    ).scalar_one()
    if written:
        raise RuntimeError(
            f"{written} escalation_messages rows record a system audit note; "
            "narrowing direction would reject them, so use read-only rollback"
        )
    constraints = _existing_constraints(bind)
    if _CONSTRAINT not in constraints:
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _NARROWED)
