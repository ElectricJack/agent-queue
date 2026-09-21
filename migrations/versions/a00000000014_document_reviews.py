"""Document reviews: review tables, the ``review`` gate type, project delegation default.

Revision ID: a00000000014
Revises: a00000000013

* ``doc_reviews``, ``doc_review_revisions``, ``doc_review_comments`` — a
  submitted document, the text of every revision and the anchored comments
  (document-review spec §3.2).  Task ids are soft text columns with **no
  foreign key to ``tasks``**, so a review can never block a task's archive
  or delete.
* ``gates.gate_type`` gains ``review`` (``ck_gates_type`` is rebuilt).
* ``projects.review_delegate_to`` — ``user | supervisor``, NULL = ``user``,
  with the named CHECK ``ck_projects_review_delegate_to``.

**Every step is conditional**, for the same reason ``a00000000013``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after these were
declared already has them.

The gate-type lists are spelled out here rather than read from
``GATE_TYPES``, so a later revision that adds another type cannot change
what this one writes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000014"
down_revision = "a00000000013"
branch_labels = None
depends_on = None

_TABLES = ("doc_reviews", "doc_review_revisions", "doc_review_comments")
_GATE_CHECK = "ck_gates_type"
_DELEGATE_CHECK = "ck_projects_review_delegate_to"
_GATE_TYPES_BEFORE = ("human", "timer", "pr-merged", "ci-run", "event", "task", "routing")
_GATE_TYPES_AFTER = _GATE_TYPES_BEFORE + ("review",)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _checks(bind, table: str) -> dict[str, str]:
    return {
        c["name"]: c.get("sqltext", "") or ""
        for c in sa.inspect(bind).get_check_constraints(table)
        if c.get("name")
    }


def _gate_type_sql(types: tuple[str, ...]) -> str:
    return "gate_type IN (" + ", ".join(f"'{t}'" for t in types) + ")"


def upgrade() -> None:
    from src.database.tables import doc_review_comments, doc_review_revisions, doc_reviews

    bind = op.get_bind()
    existing = _tables(bind)
    for table in (doc_reviews, doc_review_revisions, doc_review_comments):
        if table.name not in existing:
            table.create(bind, checkfirst=True)

    if "review_delegate_to" not in _columns(bind, "projects"):
        op.add_column("projects", sa.Column("review_delegate_to", sa.Text(), nullable=True))
    if _DELEGATE_CHECK not in _checks(bind, "projects"):
        op.create_check_constraint(
            _DELEGATE_CHECK,
            "projects",
            "review_delegate_to IS NULL OR review_delegate_to IN ('user', 'supervisor')",
        )

    checks = _checks(bind, "gates")
    if "'review'" not in checks.get(_GATE_CHECK, ""):
        if _GATE_CHECK in checks:
            op.drop_constraint(_GATE_CHECK, "gates", type_="check")
        op.create_check_constraint(_GATE_CHECK, "gates", _gate_type_sql(_GATE_TYPES_AFTER))


def downgrade() -> None:
    from src.database.queries.blocked_state import blocked_predicate
    from src.database.tables import tasks

    bind = op.get_bind()
    waiters = list(
        bind.execute(
            sa.text(
                "SELECT DISTINCT task_id FROM task_gates WHERE gate_id IN "
                "(SELECT id FROM gates WHERE gate_type = 'review')"
            )
        ).scalars()
    )
    op.execute(
        "DELETE FROM task_gates WHERE gate_id IN (SELECT id FROM gates WHERE gate_type = 'review')"
    )
    op.execute("DELETE FROM gates WHERE gate_type = 'review'")
    if waiters:
        # The deleted gates held these tasks; recompute their persisted
        # ``is_blocked`` so none stays blocked on a gate that no longer exists.
        bind.execute(
            sa.update(tasks)
            .where(tasks.c.id.in_(waiters))
            .values(is_blocked=sa.case((blocked_predicate(), 1), else_=0))
        )
    if _GATE_CHECK in _checks(bind, "gates"):
        op.drop_constraint(_GATE_CHECK, "gates", type_="check")
    op.create_check_constraint(_GATE_CHECK, "gates", _gate_type_sql(_GATE_TYPES_BEFORE))

    if _DELEGATE_CHECK in _checks(bind, "projects"):
        op.drop_constraint(_DELEGATE_CHECK, "projects", type_="check")
    if "review_delegate_to" in _columns(bind, "projects"):
        op.drop_column("projects", "review_delegate_to")

    existing = _tables(bind)
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
