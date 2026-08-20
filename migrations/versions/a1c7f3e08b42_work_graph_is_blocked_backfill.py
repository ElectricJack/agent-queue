"""work-graph: is_blocked backfill + plan-parent edge retype

Revision ID: a1c7f3e08b42
Revises: 93a8a9e48fb8
Create Date: 2026-08-19 00:00:00.000000

**Data only** — no DDL.  Every column, index and table this revision touches
was created by the Wave 0 substrate revision (``93a8a9e48fb8``), which
deliberately deferred the ``tasks.is_blocked`` backfill so the work-graph
lane could iterate on the predicate first (work-graph spec §2.2).

Two statements, in order:

1. **Retype plan-parent edges.**  Before typed edges existed, a plan subtask
   was wired to its plan task with a plain ``blocks`` edge, and
   ``_check_defined_tasks`` carried an ``is_plan_subtask`` special case that
   treated an IN_PROGRESS parent as satisfied.  That special case is gone;
   ``parent-child`` expresses the same rule as data (design §3.1).  Rows
   where the child is flagged ``is_plan_subtask`` **and** the edge points at
   its own ``parent_task_id`` are exactly the edges that special case
   covered, so they are retyped in place.  Anything else keeps ``blocks``.

2. **Backfill ``is_blocked``.**  Runs the full §3.2 predicate over every
   task.  ANSI SQL, ``EXISTS`` subqueries only — the same statement text
   executes on SQLite and PostgreSQL.

Both are idempotent: re-running produces the same rows.

Downgrade resets ``is_blocked`` to 0 and turns the retyped edges back into
``blocks``.  It cannot distinguish a ``parent-child`` edge this migration
created from one authored afterwards, so it applies the same
``is_plan_subtask``-and-``parent_task_id`` test in reverse; edges outside
that shape are left alone.

**Collision handling.**  ``dep_type`` is part of the primary key
``(task_id, depends_on_task_id, dep_type)``, so a bare ``UPDATE … SET
dep_type`` can violate it: once typed edges exist the same pair may legally
carry both ``blocks`` and ``parent-child``.  A first upgrade on a legacy DB
cannot hit that (every row is ``'blocks'``), but a **downgrade** after normal
operation can — the supervisor writes ``parent-child`` for exactly the pair
this revision retypes, and ``add_dependency(child, parent, 'blocks')`` stays
legal alongside it.  Both directions therefore run in two steps: ``DELETE``
the row that would lose the collision, then ``UPDATE`` only the rows whose
target type is free.  Retyping onto an existing row is a *merge*, not a
duplicate — the survivor already expresses the same pair.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c7f3e08b42"
down_revision: Union[str, Sequence[str], None] = "93a8a9e48fb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The plan-subtask edges the old `_check_defined_tasks` special case covered:
# a `blocks` edge from an is_plan_subtask child straight to its own parent.
_PLAN_PARENT_EDGE_PREDICATE = """
    EXISTS (
        SELECT 1 FROM tasks c
         WHERE c.id = task_dependencies.task_id
           AND c.is_plan_subtask = 1
           AND c.parent_task_id = task_dependencies.depends_on_task_id
    )
"""

def _target_exists(dep_type: str) -> str:
    """"The same pair already carries *dep_type*" — the PK-collision test."""
    return f"""
    EXISTS (
        SELECT 1 FROM task_dependencies o
         WHERE o.task_id = task_dependencies.task_id
           AND o.depends_on_task_id = task_dependencies.depends_on_task_id
           AND o.dep_type = '{dep_type}'
    )
"""


# Upgrade: `blocks` → `parent-child`.  Drop the source row when the target
# already exists (merge), retype the rest.
_DROP_COLLIDING_BLOCKS_EDGES = f"""
DELETE FROM task_dependencies
 WHERE dep_type = 'blocks'
   AND {_PLAN_PARENT_EDGE_PREDICATE}
   AND {_target_exists('parent-child')}
"""

_RETYPE_PLAN_EDGES = f"""
UPDATE task_dependencies
   SET dep_type = 'parent-child'
 WHERE dep_type = 'blocks'
   AND {_PLAN_PARENT_EDGE_PREDICATE}
   AND NOT {_target_exists('parent-child')}
"""

# Downgrade: `parent-child` → `blocks`, same two-step shape.
_DROP_COLLIDING_PARENT_EDGES = f"""
DELETE FROM task_dependencies
 WHERE dep_type = 'parent-child'
   AND {_PLAN_PARENT_EDGE_PREDICATE}
   AND {_target_exists('blocks')}
"""

_UNTYPE_PLAN_EDGES = f"""
UPDATE task_dependencies
   SET dep_type = 'blocks'
 WHERE dep_type = 'parent-child'
   AND {_PLAN_PARENT_EDGE_PREDICATE}
   AND NOT {_target_exists('blocks')}
"""

# The blocked-state predicate, verbatim from work-graph spec §3.2.  Kept as
# literal SQL rather than importing src.database.queries.blocked_state: a
# migration must keep describing the schema *as of this revision*, and the
# Python predicate is free to evolve.  `tests/test_blocked_state.py` asserts
# the two agree on a seeded graph.
_BACKFILL_IS_BLOCKED = """
UPDATE tasks SET is_blocked = CASE WHEN
  EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.id = d.depends_on_task_id
          WHERE d.task_id = tasks.id AND d.dep_type = 'blocks'
            AND dep.status != 'COMPLETED')
  OR EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id
          WHERE d.task_id = tasks.id AND d.dep_type = 'parent-child'
            AND p.status IN ('DEFINED','AWAITING_PLAN_APPROVAL'))
  OR EXISTS (SELECT 1 FROM task_dependencies w
          WHERE w.task_id = tasks.id AND w.dep_type = 'waits-for'
            AND EXISTS (SELECT 1 FROM task_dependencies pc JOIN tasks c ON c.id = pc.task_id
                        WHERE pc.dep_type = 'parent-child'
                          AND pc.depends_on_task_id = w.depends_on_task_id
                          AND c.status != 'COMPLETED'))
  OR EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.id = d.depends_on_task_id
          WHERE d.task_id = tasks.id AND d.dep_type = 'conditional-blocks'
            AND NOT (dep.status = 'BLOCKED'
                     OR (dep.status = 'FAILED' AND dep.retry_count >= dep.max_retries)))
  OR EXISTS (SELECT 1 FROM task_gates tg JOIN gates g ON g.id = tg.gate_id
          WHERE tg.task_id = tasks.id AND g.status != 'resolved')
THEN 1 ELSE 0 END
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(_DROP_COLLIDING_BLOCKS_EDGES))
    bind.execute(sa.text(_RETYPE_PLAN_EDGES))
    bind.execute(sa.text(_BACKFILL_IS_BLOCKED))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(_DROP_COLLIDING_PARENT_EDGES))
    bind.execute(sa.text(_UNTYPE_PLAN_EDGES))
    bind.execute(sa.text("UPDATE tasks SET is_blocked = 0"))
