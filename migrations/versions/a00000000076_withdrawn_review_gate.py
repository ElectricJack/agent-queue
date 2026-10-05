"""Close the gates of withdrawn reviews instead of orphaning them.

Revision ID: a00000000076
Revises: a00000000075

``aq review withdraw`` used to close a review and leave its gate ``open``
forever: nothing could resolve a ``review`` gate (``aq gate resolve`` refused
them, pointing at a decision the withdrawn review could not take), so the
gate's escalation stayed live and went on asking the human about a review that
no longer existed.  This revision backfills the rows that path already
produced.

For every ``open`` gate of type ``review`` whose review is ``withdrawn``:

* the gate becomes ``resolved`` with ``resolution = 'withdrawn'`` — the fact
  that tells every reader it closed *without* an approval;
* each waiter is put on ``hold:review_withdrawn`` and flagged
  ``needs_attention``, exactly as the live withdrawal now does, so releasing
  the gate never releases work onto a document nobody approved;
* its persisted ``is_blocked`` is recomputed, so no task stays blocked on a
  gate that is now resolved.

``rejected`` reviews are deliberately untouched.  A rejection is not terminal:
the author resubmits into ``in_review`` against this same gate, and resolving
it now would release the waiters onto a design the human turned down.

Live escalations bound to those gates (``source_kind = 'gate'``) are retired
``stale`` — §5.2 renders that as *obsolete*, which is what "the question is no
longer being asked" means — recording ``gate_resolved`` as the outcome, so
Discord cards and dashboards stop asking.

Every step is guarded because the squashed baseline builds these tables from
the live ``src.database.tables.metadata``: a database created after this
revision is already in the fixed shape and there is nothing to backfill.
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision = "a00000000076"
down_revision = "a00000000075"
branch_labels = None
depends_on = None

_TABLES = (
    "gates",
    "task_gates",
    "doc_reviews",
    "tasks",
    "task_labels",
    "task_metadata",
    "escalations",
)

_RESOLVED_BY = "migration:a00000000076"
_RESOLUTION = "withdrawn"
_HOLD = "hold:review_withdrawn"
_ATTENTION = "review_withdrawn"
_OPEN_ESCALATION_STATES = ("needs_human", "reply_received", "resolving")


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _waiters(bind, gate_ids: list[str]) -> list[str]:
    return list(bind.execute(sa.text(
        "SELECT DISTINCT task_id FROM task_gates WHERE gate_id IN :ids"
    ).bindparams(sa.bindparam("ids", value=list(gate_ids), expanding=True))).scalars())


def _orphan_gate_ids(bind) -> list[str]:
    """The open gates of withdrawn reviews — the rows this revision closes."""
    return list(bind.execute(sa.text(
        "SELECT g.id FROM gates g "
        "JOIN doc_reviews r ON r.gate_id = g.id "
        "WHERE g.gate_type = 'review' AND g.status = 'open' AND r.state = 'withdrawn'"
    )).scalars())


def _hold_waiters(bind, waiters: list[str]) -> None:
    """Hold and flag every waiter, mirroring the live withdrawal's writes.

    ``needs_attention`` is JSON-encoded the way :meth:`set_task_meta` encodes
    it — a bare string value is stored as ``"review_withdrawn"``, with the
    quotes.
    """
    labels = sa.table("task_labels", sa.column("task_id"), sa.column("label"))
    bind.execute(
        pg_insert(labels)
        .values([{"task_id": tid, "label": _HOLD} for tid in waiters])
        .on_conflict_do_nothing()
    )
    encoded = f'"{_ATTENTION}"'
    meta = sa.table("task_metadata", sa.column("task_id"), sa.column("key"), sa.column("value"))
    bind.execute(
        pg_insert(meta)
        .values([
            {"task_id": tid, "key": "needs_attention", "value": encoded} for tid in waiters
        ])
        .on_conflict_do_update(index_elements=["task_id", "key"], set_={"value": encoded})
    )


def _recompute_blocked(bind, waiters: list[str]) -> None:
    from src.database.queries.blocked_state import blocked_predicate
    from src.database.tables import tasks

    bind.execute(
        sa.update(tasks)
        .where(tasks.c.id.in_(waiters))
        .values(is_blocked=sa.case((blocked_predicate(), 1), else_=0))
    )


def _retire_gate_escalations(bind, gate_ids: list[str], now: float) -> None:
    from src.database.tables import escalations

    bind.execute(
        sa.update(escalations)
        .where(
            escalations.c.source_kind == "gate",
            escalations.c.source_identity.in_(gate_ids),
            escalations.c.state.in_(_OPEN_ESCALATION_STATES),
        )
        .values(
            state="stale",
            revision=escalations.c.revision + 1,
            updated_at=now,
            terminal_at=now,
            terminal_outcome=f"Gate resolved: review {_RESOLUTION}",
            terminal_evidence={"resolution": _RESOLUTION, "source": _RESOLVED_BY},
            outcome="gate_resolved",
        )
    )


def upgrade() -> None:
    from src.database.tables import gates

    bind = op.get_bind()
    if not _tables(bind).issuperset(_TABLES):
        return
    orphans = _orphan_gate_ids(bind)
    if not orphans:
        return
    waiters = _waiters(bind, orphans)
    # Hold first, exactly as the live path does: a held task is excluded from
    # the frontier check, so no released waiter is announced as ready work.
    if waiters:
        _hold_waiters(bind, waiters)
    bind.execute(
        sa.update(gates)
        .where(gates.c.id.in_(orphans), gates.c.status == "open")
        .values(status="resolved", resolved_by=_RESOLVED_BY, resolution=_RESOLUTION)
    )
    if waiters:
        _recompute_blocked(bind, waiters)
    _retire_gate_escalations(bind, orphans, time.time())


def downgrade() -> None:
    from src.database.tables import gates

    bind = op.get_bind()
    if not _tables(bind).issuperset(_TABLES):
        return
    reopened = list(bind.execute(sa.text(
        "SELECT id FROM gates WHERE gate_type = 'review' AND resolved_by = :by "
        "AND resolution = :resolution"
    ), {"by": _RESOLVED_BY, "resolution": _RESOLUTION}).scalars())
    if not reopened:
        return
    waiters = _waiters(bind, reopened)
    bind.execute(
        sa.update(gates)
        .where(gates.c.id.in_(reopened))
        .values(status="open", resolved_by=None, resolution=None)
    )
    # The re-opened gates hold these tasks again, so their projection must go
    # back to blocked.  The hold labels and the ``needs_attention`` flags stay:
    # they record the withdrawal, which is still true, and the live path writes
    # them again.
    if waiters:
        _recompute_blocked(bind, waiters)
