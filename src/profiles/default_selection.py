"""Pick a sensible fallback agent profile for a project.

Background — the "no resolvable profile_id" stall
-------------------------------------------------
:class:`~src.orchestrator.agent_reconciler.AgentReconciler` only creates
agent rows for profile ids it can resolve from a READY task:
``task.profile_id or project.default_profile_id``.  Tasks created by
playbooks, the supervisor, or plan-splitting almost never carry an
explicit ``profile_id``, and ``create_project`` historically left
``default_profile_id`` NULL.  The result was a deadlock that the system
could not heal on its own: READY tasks, zero idle agents, and a
once-per-project WARN reading ``project=X has READY tasks but no
resolvable profile_id``.

This module supplies the missing third rung of the cascade — a
deterministic system-wide default — so a project always has *some*
profile to build agents from.  It is used in two places:

* ``create_project`` — stamps a default onto new projects up front.
* ``AgentReconciler`` — backfills existing projects whose
  ``default_profile_id`` is NULL, healing already-stuck projects
  without a migration.

Selection order (see ``docs/superpowers/specs/2026-05-07-agent-\
reconciliation-design.md`` §2, "Auto-picking a project default profile"):

1. ``claude-opus``
2. ``claude-sonnet``
3. ``worker-standard-medium-claude``
4. any remaining general-purpose profile, alphabetically by id
5. any remaining non-supervisor profile, alphabetically by id

Steps 4 and 5 differ only in whether special-purpose profiles (reviewer,
planner, triage, …) are eligible: they are a poor default because they
are written for one pipeline stage, but they beat returning ``None`` and
stalling the queue.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Tried first, in this exact order, when picking a project default.
#: ``worker-standard-medium-claude`` is named explicitly because the
#: alphabetical fallback below picked ``worker-deep-high-claude`` out of the
#: shipped worker ladder (deep < fast < standard), quietly making the most
#: expensive tier every project's default for tasks that carry no profile of
#: their own.  The legacy pre-rename ids are kept as trailing entries so a
#: vault seeded before the provider-explicit rename still resolves to the
#: standard tier instead of falling through to alphabetical order.
PREFERRED_DEFAULT_PROFILE_IDS: tuple[str, ...] = (
    "claude-opus",
    "claude-sonnet",
    "worker-standard-medium-claude",
    "worker-standard",
)

#: Profiles written for one specific pipeline stage.  Usable as a
#: last-resort default (better than stalling) but never preferred.
SPECIAL_PURPOSE_PROFILE_IDS: frozenset[str] = frozenset(
    {
        "final-reviewer",
        "planner",
        "playbook-compiler",
        "reviewer",
        "spec-ingest",
        "triage",
    }
)

#: Never a valid project default — the supervisor is a daemon-wide
#: singleton that lives outside the agents table.
EXCLUDED_PROFILE_IDS: frozenset[str] = frozenset({"supervisor"})


def _is_project_scoped(profile_id: str) -> bool:
    """True for a retired ``project:{pid}:{profile_id}`` override row.

    Project-scoped profiles were removed; a row left behind by an older
    release (until the startup migration drops it) is never a valid default.
    """
    return profile_id.startswith("project:")


def select_default_profile_id(profile_ids: Iterable[str]) -> str | None:
    """Return the best fallback profile id, or ``None`` if there are none.

    ``profile_ids`` is any iterable of registered profile ids (e.g. the
    keys of ``{p.id: p for p in await db.list_profiles()}``).  Selection
    is deterministic: the same profile set always yields the same answer,
    so the reconciler does not flap between profiles across ticks.
    """
    candidates = {
        pid
        for pid in profile_ids
        if pid and pid not in EXCLUDED_PROFILE_IDS and not _is_project_scoped(pid)
    }
    if not candidates:
        return None

    for preferred in PREFERRED_DEFAULT_PROFILE_IDS:
        if preferred in candidates:
            return preferred

    general = sorted(candidates - SPECIAL_PURPOSE_PROFILE_IDS)
    if general:
        return general[0]

    return sorted(candidates)[0]
