"""Router readiness: which projects only claim routed work (mandatory routing §9.1).

A project's router is **ready** when the playbook its ``assignment_playbook_id``
names has an enabled system activation, or an enabled activation scoped to
that project, whose artifact grants ``task_route_apply``.  From then on the
claim frontier and the push scheduler accept only ``router``, ``override`` and
``role`` routes, and route-needed emission treats a ``legacy`` route as no
route at all.  Before that, ``legacy`` work keeps running -- a router
activation that failed to import cannot stop the factory at the cutover.

The orchestrator refreshes the set once per cycle (:meth:`RouterReadiness.refresh`)
and hands it to emission, the push scheduler and pool demand; a pool claim
reads the same cached set.  Grants are read from the immutable artifact, so a
hash's answer is cached for the life of the process.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The command whose grant makes a bound playbook a router (spec §6.6).
ROUTER_GRANT = "task_route_apply"


def _value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def ready_router_projects(
    projects: Iterable[Any],
    activations: Iterable[Any],
    grants_of: Callable[[str], frozenset[str]],
) -> frozenset[str]:
    """The ids of *projects* whose bound router is ready (§9.1).

    *activations* are ``playbook_activations`` rows (mappings or objects);
    *grants_of* maps an artifact hash to the AQ commands it grants.  Pure:
    the caller reads the rows and owns the artifact store.
    """
    enabled = [
        row for row in activations
        if _value(row, "enabled") is True and _value(row, "active_artifact_sha256")
    ]
    ready: set[str] = set()
    for project in projects:
        project_id = str(_value(project, "id") or "")
        bound = str(_value(project, "assignment_playbook_id") or "").strip()
        if not project_id or not bound:
            continue
        for row in enabled:
            if _value(row, "playbook_id") != bound:
                continue
            scope = _value(row, "scope")
            identifier = _value(row, "scope_identifier") or ""
            if not (
                (scope == "system" and identifier == "")
                or (scope == "project" and identifier == project_id)
            ):
                continue
            if ROUTER_GRANT in grants_of(str(_value(row, "active_artifact_sha256"))):
                ready.add(project_id)
                break
    return frozenset(ready)


class RouterReadiness:
    """The per-cycle ready set, and the grant cache behind it.

    *db_getter* returns the database adapter; *artifact_loader* loads an
    artifact definition by hash (``ArtifactStore.load``).  An artifact that
    cannot be loaded grants nothing and is not cached, so a repaired store
    is read again on the next refresh.
    """

    def __init__(
        self,
        *,
        db_getter: Callable[[], Any],
        artifact_loader: Callable[[str], Any],
    ) -> None:
        self._db_getter = db_getter
        self._load = artifact_loader
        self._grants: dict[str, frozenset[str]] = {}
        self._ready: frozenset[str] | None = None

    def grants(self, artifact_sha256: str) -> frozenset[str]:
        cached = self._grants.get(artifact_sha256)
        if cached is not None:
            return cached
        from src.playbooks.definition import granted_aq_commands

        try:
            definition = self._load(artifact_sha256)
        except Exception:  # an unreadable artifact routes nothing
            logger.debug("router readiness: artifact %s unreadable", artifact_sha256,
                         exc_info=True)
            return frozenset()
        granted = granted_aq_commands(definition)
        self._grants[artifact_sha256] = granted
        return granted

    async def refresh(self, projects: Iterable[Any] | None = None) -> frozenset[str]:
        """Recompute the ready set from the live activations."""
        db = self._db_getter()
        if projects is None:
            projects = await db.list_projects()
        activations = await db.list_playbook_activations(enabled_only=True)
        ready = ready_router_projects(projects, activations, self.grants)
        if self._ready is not None and ready != self._ready:
            logger.info(
                "router readiness: ready projects %s -> %s",
                sorted(self._ready), sorted(ready),
            )
        self._ready = ready
        return ready

    async def ready_projects(self) -> frozenset[str]:
        """The last refreshed set, refreshing once when there is none yet."""
        if self._ready is None:
            return await self.refresh()
        return self._ready

    async def is_ready(self, project_id: str) -> bool:
        return project_id in await self.ready_projects()


async def orchestrator_ready_projects(orchestrator: Any) -> frozenset[str]:
    """*orchestrator*'s ready set; empty when it carries no :class:`RouterReadiness`.

    Command handlers built around a stub orchestrator see every project as
    not ready: legacy work stays claimable and unrouted work never is.
    """
    readiness = getattr(orchestrator, "router_readiness", None)
    if not isinstance(readiness, RouterReadiness):
        return frozenset()
    return await readiness.ready_projects()


async def orchestrator_router_ready(orchestrator: Any, project_id: str) -> bool:
    return project_id in await orchestrator_ready_projects(orchestrator)


__all__ = [
    "ROUTER_GRANT",
    "RouterReadiness",
    "orchestrator_ready_projects",
    "orchestrator_router_ready",
    "ready_router_projects",
]
