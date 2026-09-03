"""cutover_authorized event kind

Playbook V2 Package 7 §3.9 (``docs/superpowers/plans/2026-09-01-playbook-v2-
cutover-cleanup.md``).  Gate G2 — two named humans authorizing the switch to
the V2 runtime — is recorded as one ``cutover_authorized`` row per signature
in the append-only ``playbook_cutover_events`` table.  ``kind`` is a closed set
enforced by ``ck_playbook_cutover_events_kind``, so admitting the new kind
means rewriting that constraint; nothing else about the table changes.

Widening a CHECK constraint is a metadata change on PostgreSQL.  On SQLite it
is a table rebuild, which ``batch_alter_table`` performs; the table holds at
most a handful of rows on any fleet, and the rebuild copies them unchanged.

No data migration and no backfill: a fleet that has not begun gate G2 has no
authorizations, and "no rows" is the correct description of that state.
Downgrade restores the previous set and would refuse if any
``cutover_authorized`` row existed — deliberately, because an audit row is not
something a downgrade should quietly delete.

Revision ID: e6a1b2c3d4f5
Revises: d4e7c9a1f2b8
Create Date: 2026-09-03

"""

from alembic import op

revision = "e6a1b2c3d4f5"
down_revision = "d4e7c9a1f2b8"
branch_labels = None
depends_on = None

_TABLE = "playbook_cutover_events"
_CONSTRAINT = "ck_playbook_cutover_events_kind"

_PREVIOUS_KINDS = (
    "v1_admission_closed",
    "v1_admission_reopened",
    "drain_completed",
    "switched_to_v2",
    "rolled_back_to_v1",
    "window_coverage_rehearsal",
    "rollback_window_closed",
)
_KINDS = _PREVIOUS_KINDS[:3] + ("cutover_authorized",) + _PREVIOUS_KINDS[3:]


def _kind_in(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{kind}'" for kind in kinds) + ")"


def _rewrite_constraint(kinds: tuple[str, ...]) -> None:
    # Same shape as ``0ff97e8792f8``'s ``ck_gates_type`` rewrite.  SQLite
    # reflects the named CHECK constraint, so the batch rebuild drops it by
    # name and writes the new one; passing it again via ``table_args`` would
    # leave the old constraint in the rebuilt table next to the new one.
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _kind_in(kinds))


def upgrade() -> None:
    _rewrite_constraint(_KINDS)


def downgrade() -> None:
    _rewrite_constraint(_PREVIOUS_KINDS)
