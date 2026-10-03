"""Which project owns the target a command names (per-project elevated scope).

Every ``CommandArgs`` model sets ``extra="forbid"``, so a scope gate that
injects ``project_id`` into a command whose contract does not declare it turns
the call into a validation error the caller cannot fix:

    aq --json integration reserve-owner --task-id verify-04199964
    -> invalid reservation request: 1 validation error for
       IntegrationReserveOwnerArgs
       project_id  Extra inputs are not permitted [type=extra_forbidden]

Much of the registry is keyed by a target rather than by a project — the task-,
operation-, batch- and reservation-keyed integration recovery controls among
them — and a per-project supervisor token could not run any of them, so the
recovery its own runbooks assign to it needed the global operator.

:func:`~src.api.scope.check_command_scope` therefore stops injecting the field
for those commands, and isolation moves here: resolve the row the command
names, read its ``project_id``, compare it with the token's.  This is the one
mechanism for the whole surface — no contract gained a field, and
``extra="forbid"`` is untouched.

The policy fails closed, because "I could not work out who owns this" must not
mean "allowed":

* an argument that names a row but has no resolver here is refused, not
  ignored — a command whose real target this module cannot resolve is granted
  to nobody until its owner can be read;
* a command that names no target at all is refused;
* a target whose row is gone resolves to *no* project claim, and the handler
  reports ``not_found`` itself — the pre-existing answer for a missing row, not
  a cross-project read;
* a database the scope layer cannot query refuses the call rather than
  admitting it unchecked.

Single-row project ownership reads belong in :data:`TARGET_RESOLVERS`; a
project reference resolves the project row itself. A row that owns no project
of its own resolves through its owner — an operation through the row its ``target_kind`` names, a
branch owner through its repository, an escalation message through its
escalation — and anything polymorphic (an escalation action's ``target_id``, a
report request's ``request_id``, a transfer's branch ``target``) is
deliberately absent, so those commands stay with the global operator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from sqlalchemy import select

from src.database.tables import (
    escalation_messages,
    escalations,
    gates,
    integration_batches,
    integration_branch_owners,
    integration_candidate_resolutions,
    integration_check_evidence,
    integration_outbox,
    integration_parent_episodes,
    integration_promotion_intents,
    jobs,
    projects,
    repos,
    test_selection_promotions,
    test_selections,
)

#: Argument names that name a row owned by exactly one project.  An argument
#: whose name ends in ``_id`` is a *target reference*; anything else in a
#: command's args is a value, a fence, or an expectation.
TARGET_REFERENCE_SUFFIX: Final[str] = "_id"
TARGET_REFERENCE_NAMES: Final[frozenset[str]] = frozenset({"depends_on"})

#: ``_id``-shaped arguments that are values, not rows this database can look up.
#: ``external_message_id`` is the reply's idempotency key on the far side of a
#: transport (a Discord message id), so no project owns it and there is nothing
#: to resolve.  A name may only be added here with that kind of reason: a *row*
#: whose owner this module cannot read is simply absent from
#: :data:`TARGET_RESOLVERS`, which refuses the command rather than waving it
#: through.
VALUE_ARGUMENTS: Final[frozenset[str]] = frozenset({"external_message_id"})

Resolver = Callable[[Any, str], Awaitable[str | None]]


async def _project_id_of(db, table, value: str) -> str | None:
    """The ``project_id`` of the ``table`` row whose id is *value*, else ``None``."""
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(table.c.project_id).where(table.c.id == value)
            )
        ).mappings().one_or_none()
    return None if row is None else row["project_id"]


def _row_resolver(table) -> Resolver:
    """A resolver reading ``project_id`` from one row of *table*."""

    async def resolve(db, value: str) -> str | None:
        return await _project_id_of(db, table, value)

    return resolve


def _entity_resolver(getter: str) -> Resolver:
    """A resolver reading ``project_id`` from a ``Database.get_*`` row."""

    async def resolve(db, value: str) -> str | None:
        row = await getattr(db, getter)(value)
        if row is None:
            return None
        project_id = row.get("project_id") if isinstance(row, dict) else row.project_id
        return None if project_id is None else str(project_id)

    return resolve


async def _owner_column_value(db, table, column, value: str):
    """``column`` of the ``table`` row whose primary key is *value*."""
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(select(column).where(table.c.id == value))
        ).mappings().one_or_none()
    return None if row is None else row[column.name]


def _foreign_resolver(table, column, owner_argument: str) -> Resolver:
    """A resolver for a row with no project of its own, read through its owner.

    *value* names the row in *table*; *column* is the foreign key that names its
    owner, which is resolved as if the caller had passed it under
    *owner_argument*.
    """

    async def resolve(db, value: str) -> str | None:
        owner = await _owner_column_value(db, table, column, value)
        return None if owner is None else await _target_project(db, owner_argument, owner)

    return resolve


async def _task_project(db, task_id: str) -> str | None:
    """A task's project, live or archived.

    Archived identity matters: a delivery control that names a completed task
    still has to see which project completed it.
    """
    from src.database.queries.task_identity import resolve_task_identity_on

    async with db._engine.connect() as conn:
        identity = await resolve_task_identity_on(conn, task_id)
    return identity.project_id if identity is not None else None


async def _operation_project(db, operation_id: str) -> str | None:
    from src.integration.operation_ownership import operation_project_id_for

    return await operation_project_id_for(db, operation_id)


async def _project_reference(db, project_id: str) -> str | None:
    """A nested project preference names the project row itself."""
    async with db._engine.connect() as conn:
        return (
            await conn.execute(select(projects.c.id).where(projects.c.id == project_id))
        ).scalar_one_or_none()


#: Target reference argument -> the resolver that reads its owning project.
#: Written by hand on purpose: each entry is a claim about one table's ownership
#: rule, and ``tests/test_target_scope.py`` pins the surface this admits, so a
#: new command cannot quietly inherit a pass.
TARGET_RESOLVERS: Final[dict[str, Resolver]] = {
    "receive_new_work.project_id": _project_reference,
    # Tasks, live or archived.  ``parent_id`` is the parent task a graph write
    # files children under; ``depends_on`` is a list of other tasks.
    "task_id": _task_project,
    "parent_task_id": _task_project,
    "child_task_id": _task_project,
    "source_task_id": _task_project,
    "parent_id": _task_project,
    "depends_on": _task_project,
    # Integration targets.  A row with no project of its own resolves through
    # the owner it names: a parent episode through the task it collects, check
    # evidence through its operation, a branch owner through its repository.
    "expected_episode_id": _foreign_resolver(
        integration_parent_episodes, integration_parent_episodes.c.parent_task_id, "task_id"
    ),
    "operation_id": _operation_project,
    "reservation_id": _row_resolver(integration_candidate_resolutions),
    "owner_row_id": _foreign_resolver(
        integration_branch_owners,
        integration_branch_owners.c.repository_id,
        "repository_id",
    ),
    "evidence_id": _foreign_resolver(
        integration_check_evidence,
        integration_check_evidence.c.operation_id,
        "operation_id",
    ),
    "intent_id": _row_resolver(integration_promotion_intents),
    # Rows that carry their own project.
    "batch_id": _row_resolver(integration_batches),
    "event_id": _row_resolver(integration_outbox),
    "escalation_id": _row_resolver(escalations),
    "gate_id": _row_resolver(gates),
    "job_id": _row_resolver(jobs),
    "repository_id": _row_resolver(repos),
    "selection_id": _row_resolver(test_selections),
    "promotion_id": _row_resolver(test_selection_promotions),
    "session_id": _entity_resolver("get_session"),
    "workspace_id": _entity_resolver("get_workspace"),
    # An escalation message belongs to its escalation.
    "reply_id": _foreign_resolver(
        escalation_messages, escalation_messages.c.escalation_id, "escalation_id"
    ),
}


def target_references(args: dict, *, command: str | None = None) -> dict[str, list[str]]:
    """The target references *args* carries: argument name -> values.

    A list-valued reference (``depends_on``) contributes every member. The
    provider preference's nested project reference is supported only for
    ``provider_allocation_preview``; arbitrary nested values do not become
    scope targets.
    """
    found: dict[str, list[str]] = {}
    for name, value in args.items():
        # This resolver key labels a nested path, never a top-level argument.
        if name == "receive_new_work.project_id":
            continue
        if name in VALUE_ARGUMENTS or not (
            name.endswith(TARGET_REFERENCE_SUFFIX) or name in TARGET_REFERENCE_NAMES
        ):
            continue
        values = value if isinstance(value, list | tuple) else [value]
        ids = [item for item in values if isinstance(item, str) and item]
        if ids:
            found[name] = ids
    receive = args.get("receive_new_work") if command == "provider_allocation_preview" else None
    if isinstance(receive, dict):
        project_id = receive.get("project_id")
        if isinstance(project_id, str) and project_id:
            found["receive_new_work.project_id"] = [project_id]
    return found


def unresolvable_targets(args: dict, *, command: str | None = None) -> list[str]:
    """Target references this module has no ownership resolver for."""
    return sorted(
        name for name in target_references(args, command=command) if name not in TARGET_RESOLVERS
    )


async def _target_project(db, argument: str, value: str) -> str | None:
    resolver = TARGET_RESOLVERS[argument]
    return await resolver(db, value)


async def target_scope_error(command: str, args: dict, project_id: str, *, db) -> str | None:
    """Confine a per-project elevated *command* to *project_id*'s own targets.

    The returned string is the scope error.  ``None`` means every target the
    command names either belongs to *project_id* or names a row that is gone,
    which the handler answers for itself.
    """
    if db is None:
        return (
            f"out of scope: {command} names a project-owned target that cannot be "
            "verified without a database"
        )
    references = target_references(args, command=command)
    if not references:
        return (
            f"out of scope: {command} names no project-owned target, so a token "
            f"scoped to project {project_id} may not run it"
        )
    unknown = unresolvable_targets(args, command=command)
    if unknown:
        return (
            f"out of scope: {command} names a target whose owning project cannot be "
            f"resolved ({', '.join(unknown)})"
        )
    for name, ids in references.items():
        for value in ids:
            owning = await _target_project(db, name, value)
            if owning is None or owning == project_id:
                continue
            return (
                f"out of scope: {command} targets another project "
                f"({name} belongs to {owning})"
            )
    return None


__all__ = [
    "TARGET_REFERENCE_NAMES",
    "TARGET_REFERENCE_SUFFIX",
    "TARGET_RESOLVERS",
    "target_references",
    "target_scope_error",
    "unresolvable_targets",
]
