"""Allow supervisor-only object experiment reviews.

Revision ID: a00000000072
Revises: a00000000071
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "a00000000072"
down_revision = "a00000000071"
branch_labels = None
depends_on = None

_TABLE = "doc_reviews"
_CHECK = "ck_doc_reviews_decider"


def _replace(*, internal: bool) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    checks = {item["name"]: item["sqltext"]
              for item in inspector.get_check_constraints(_TABLE)}
    current = checks.get(_CHECK)
    if current and ("'supervisor'" in current) == internal:
        return
    if current:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    values = "'user', 'user_or_supervisor'" + (", 'supervisor'" if internal else "")
    op.create_check_constraint(_CHECK, _TABLE, f"decider IN ({values})")


def upgrade() -> None:
    _replace(internal=True)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not all(inspector.has_table(table) for table in
               (_TABLE, "task_metadata", "tasks")):
        return
    markers = {}
    for task_id, value in bind.execute(sa.text(
        "SELECT task_id, value FROM task_metadata WHERE key = 'object_experiment'"
    )):
        try:
            marker = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(marker, dict) and marker.get("object_id"):
            markers[task_id] = marker
    if not markers:
        return
    parents = dict(bind.execute(sa.text("SELECT id, parent_task_id FROM tasks")).all())
    audiences = {"supervisor": [], "user": []}
    for (author,) in bind.execute(sa.text(
        "SELECT DISTINCT author_task_id FROM doc_reviews WHERE author_task_id IS NOT NULL"
    )):
        task_id, seen = author, set()
        while task_id and task_id not in seen and len(seen) < 3:
            seen.add(task_id)
            if marker := markers.get(task_id):
                audience = ("user" if marker.get("purpose") == "finalize" and len(seen) == 1
                            else "supervisor")
                audiences[audience].append(author)
                break
            task_id = parents.get(task_id)
    reviews = sa.table(_TABLE, sa.column("author_task_id"), sa.column("decider"))
    for audience, authors in audiences.items():
        if authors:
            bind.execute(sa.update(reviews).where(reviews.c.author_task_id.in_(authors))
                         .values(decider=audience))


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE) and bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM doc_reviews WHERE decider = 'supervisor')"
    )).scalar_one():
        raise RuntimeError("supervisor-only reviews must be handled before downgrade")
    _replace(internal=False)
