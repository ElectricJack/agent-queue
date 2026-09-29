"""A task's profile always names who wrote it: ``ck_tasks_route_source_profile``.

Revision ID: a00000000042
Revises: a00000000041

Revision 2 of the mandatory-task-routing spec (2026-09-28, §9.2, §11; Task 6):

* re-runs revision 1's route-source backfill for rows written since then --
  a profile still marked ``unrouted`` is ``role`` for a stage profile and
  ``legacy`` for any other, and a row whose profile was nulled (``delete_profile``
  before this revision left the source behind) is ``unrouted``;
* adds ``ck_tasks_route_source_profile``:
  ``(profile_id IS NULL) = (route_source = 'unrouted')``.

With the check in place the query layer no longer stamps a profile written
with no source: a writer that names none fails at the database.

Revision 1's ``class_hint`` copy is deliberately not re-run.  Every row
written since revision 1 records the filer's hint itself, and copying a
router's class into ``class_hint`` would turn the router's choice into a
filer's hint on the next re-route.

Idempotent: both backfills only touch rows that violate the rule, and the
check is created only when missing.  Downgrade drops the check.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000042"
down_revision = "a00000000041"
branch_labels = None
depends_on = None

_CHECK = "ck_tasks_route_source_profile"
_RULE = "(profile_id IS NULL) = (route_source = 'unrouted')"
# Literal copy of src.routing.sources.ROLE_PROFILE_IDS: a revision must not
# change meaning when the code it shipped with moves on.
_ROLE_PROFILES = ("triage", "spec-ingest", "reviewer", "final-reviewer")


def _has_check(bind) -> bool:
    return any(c["name"] == _CHECK for c in sa.inspect(bind).get_check_constraints("tasks"))


def upgrade() -> None:
    bind = op.get_bind()
    roles = sa.bindparam("roles", _ROLE_PROFILES, expanding=True)
    bind.execute(
        sa.text(
            "UPDATE tasks SET route_source = 'role' "
            "WHERE route_source = 'unrouted' AND profile_id IN :roles"
        ).bindparams(roles)
    )
    bind.execute(
        sa.text(
            "UPDATE tasks SET route_source = 'legacy' "
            "WHERE route_source = 'unrouted' AND profile_id IS NOT NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE tasks SET route_source = 'unrouted' "
            "WHERE route_source <> 'unrouted' AND profile_id IS NULL"
        )
    )
    if not _has_check(bind):
        op.create_check_constraint(_CHECK, "tasks", _RULE)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_check(bind):
        op.drop_constraint(_CHECK, "tasks", type_="check")
