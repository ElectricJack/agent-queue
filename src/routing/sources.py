"""Who wrote a task's worker route: ``tasks.route_source`` (routing spec §3 I1, §4).

* ``unrouted`` — no route yet; ``profile_id`` is NULL and the router owes one.
* ``router`` — the bound routing playbook wrote it.
* ``override`` — an audited emergency override (spec §7, D2).
* ``role`` — a control-plane stage profile (``ROLE_PROFILE_IDS``) that is
  never routed (D3).
* ``legacy`` — written before the router existed, or by a manual route that
  is not an override.  Claimable only while the project's router is not
  ready (§9.1).

``ck_tasks_route_source_profile`` holds ``(profile_id IS NULL) =
(route_source = 'unrouted')``: every profile write declares its source.

Dependency-free on purpose: :mod:`src.models` and the migrations import it.
"""

from __future__ import annotations

UNROUTED = "unrouted"
ROUTER = "router"
OVERRIDE = "override"
ROLE = "role"
LEGACY = "legacy"

#: Every value ``ck_tasks_route_source`` admits, in the spec's order.
ROUTE_SOURCES: tuple[str, ...] = (UNROUTED, ROUTER, OVERRIDE, ROLE, LEGACY)

#: The sources a pool claim or a push launch accepts in a project whose
#: router is ready (spec I3).  Before readiness ``legacy`` is accepted too;
#: ``unrouted`` never is.
CLAIMABLE_SOURCES: tuple[str, ...] = (ROUTER, OVERRIDE, ROLE)

#: The stage profiles a role task keeps (spec §4, D3).  ``supervisor`` and
#: ``playbook-compiler`` are absent: they are never task routes at all.
ROLE_PROFILE_IDS: frozenset[str] = frozenset(
    {"triage", "spec-ingest", "reviewer", "final-reviewer"}
)

#: The playbook a project is bound to when nothing names another one: the
#: default of the ``routing.default_router`` config key (spec §8).
DEFAULT_ROUTER_PLAYBOOK_ID = "default-assignment-routing"


class UndeclaredRouteSource(ValueError):
    """A profile write that names no ``route_source`` (spec §9.2)."""


def claimable_sources(router_ready: bool) -> tuple[str, ...]:
    """The route sources a claim accepts in a project whose router is (not) ready."""
    return CLAIMABLE_SOURCES if router_ready else (*CLAIMABLE_SOURCES, LEGACY)


def route_is_claimable(
    profile_id: str | None, route_source: str | None, *, router_ready: bool
) -> bool:
    """Spec I3 for one task row: a profile and a source the project accepts."""
    return bool(profile_id) and (route_source or UNROUTED) in claimable_sources(router_ready)


def declared_route_source(profile_id: str | None, route_source: str | None = None) -> str:
    """The source to store for a write of *profile_id* declaring *route_source*.

    No profile is always ``unrouted``.  A profile needs a declared source other
    than ``unrouted``: the transitional ``role``/``legacy`` stamp of spec §9.2
    ended with ``ck_tasks_route_source_profile``, so a writer that names none
    is refused here rather than stored as a route nobody chose.
    """
    if not profile_id:
        return UNROUTED
    if route_source and route_source != UNROUTED:
        return route_source
    raise UndeclaredRouteSource(
        f"a write of profile '{profile_id}' must declare its route_source "
        f"({', '.join(s for s in ROUTE_SOURCES if s != UNROUTED)}); "
        "only the router, an override, a role creator or a failover move writes a route"
    )
