"""Who wrote a task's worker route: ``tasks.route_source`` (routing spec §3 I1, §4).

* ``unrouted`` — no route yet; ``profile_id`` is NULL and the router owes one.
* ``router`` — the bound routing playbook wrote it.
* ``override`` — an audited emergency override (spec §7, D2).
* ``role`` — a control-plane stage profile (``ROLE_PROFILE_IDS``) that is
  never routed (D3).
* ``legacy`` — written before the router existed, or by a writer that has not
  declared a source yet.

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

#: The stage profiles a role task keeps (spec §4, D3).  ``supervisor`` and
#: ``playbook-compiler`` are absent: they are never task routes at all.
ROLE_PROFILE_IDS: frozenset[str] = frozenset(
    {"triage", "spec-ingest", "reviewer", "final-reviewer"}
)

#: The playbook a project is bound to when nothing names another one: the
#: default of the ``routing.default_router`` config key (spec §8).
DEFAULT_ROUTER_PLAYBOOK_ID = "default-assignment-routing"


def stamped_route_source(profile_id: str | None, route_source: str | None = None) -> str:
    """The source to store for a write of *profile_id* declaring *route_source*.

    No profile is always ``unrouted``.  A declared source other than
    ``unrouted`` is kept.  A profile written with no source — every writer
    until the filing surfaces declare one — is ``role`` for a role profile
    and ``legacy`` for any other: the transitional stamping of spec §9.2,
    which revision 2's ``ck_tasks_route_source_profile`` ends.
    """
    if not profile_id:
        return UNROUTED
    if route_source and route_source != UNROUTED:
        return route_source
    return ROLE if profile_id in ROLE_PROFILE_IDS else LEGACY
