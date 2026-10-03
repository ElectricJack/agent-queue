"""Stateful escalation columns: ``escalations.outcome`` and ``collapsed_at``.

Adds the two additive columns the stateful-escalations phase (spec §5.2, §5.5,
§7.1) reads and writes:

* ``outcome`` — which §5.5 rule closed the incident (``human``, ``gate_resolved``,
  ``task_terminal``, ``stale_expired``, …).  It is what the collapsed post's "no
  longer needed: …" line names and what the §5.6 sweep reads back.
* ``collapsed_at`` — when the incident's one channel post was last edited into
  its collapsed one-line form.  It is the retention clock §5.2's
  ``delete_collapsed_after_hours`` needs, and it makes "already collapsed" a
  durable idempotency check instead of a guess about the channel.  A closed
  incident is not *required* to carry one: an incident closed before this
  revision, or one whose post never reached the channel, reads as NULL.

Both are nullable and unbacked: an incident created before this revision has
NULL for each and keeps the create-only post behaviour, which is exactly the
``discord.escalations.stateful`` off state.  The adds are inspector-guarded
because the squashed baseline builds ``escalations`` from the live
``src.database.tables.metadata``, so a database created after these columns were
declared already has them.

The two named check constraints are installed only when they are missing, and
both are idempotent in content: a database that already carries them (because
it was created from the updated metadata) is left alone rather than dropped and
recreated.

Revision ID: a00000000062
Revises: a00000000061
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000062"
down_revision = "a00000000061"
branch_labels = None
depends_on = None

_TABLE = "escalations"
_COLUMNS = {
    "outcome": sa.Column("outcome", sa.Text(), nullable=True),
    "collapsed_at": sa.Column("collapsed_at", sa.Float(), nullable=True),
}
_CONSTRAINTS = {
    "ck_escalations_outcome": (
        "outcome IS NULL OR outcome IN ("
        "'human','gate_resolved','notice_delivered','supervisor_started',"
        "'task_terminal','stale_expired','sweep')"
    ),
    "ck_escalations_collapsed": (
        "collapsed_at IS NULL OR state IN ('resolved','cancelled','stale')"
    ),
}



def _existing_columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}


def _existing_constraints(bind) -> dict[str, str]:
    return {
        item["name"]: item["sqltext"]
        for item in sa.inspect(bind).get_check_constraints(_TABLE)
        if item["name"]
    }


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    columns = _existing_columns(bind)
    for name, column in _COLUMNS.items():
        if name not in columns:
            op.add_column(_TABLE, column)
    constraints = _existing_constraints(bind)
    for name, sqltext in _CONSTRAINTS.items():
        current = constraints.get(name)
        if current is not None and _normalise(current) == _normalise(sqltext):
            continue
        if current is not None:
            op.drop_constraint(name, _TABLE, type_="check")
        op.create_check_constraint(name, _TABLE, sqltext)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    # An incident whose only collapse record is the timestamp it is losing is
    # no longer distinguishable from one that was never collapsed, so refuse
    # rather than quietly destroy that fact.
    marked = bind.execute(
        sa.text(f'SELECT count(*) FROM "{_TABLE}" WHERE collapsed_at IS NOT NULL')
    ).scalar_one()
    if marked:
        raise RuntimeError(
            f"{marked} escalations record a collapsed post; drop the retention "
            "clock first or use read-only rollback"
        )
    constraints = _existing_constraints(bind)
    for name in _CONSTRAINTS:
        if name in constraints:
            op.drop_constraint(name, _TABLE, type_="check")
    columns = _existing_columns(bind)
    for name in _COLUMNS:
        if name in columns:
            op.drop_column(_TABLE, name)


def _normalise(sqltext: str) -> str:
    return "".join(str(sqltext or "").lower().split()).replace("'", "")