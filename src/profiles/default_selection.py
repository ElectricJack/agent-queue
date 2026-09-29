"""Profile-id sets for the stage profiles that never serve as an ordinary worker.

This module used to pick a fallback profile for a project
(``select_default_profile_id``), which ``create_project``, onboarding and the
agent reconciler stamped onto ``projects.default_profile_id``.  Mandatory task
routing (spec 2026-09-28 §8) retired project defaults: every project is bound
to a router instead, and the column is dropped.  The sets below stay, because
:func:`src.profiles.catalog._stage_profile_ids` builds the stage-profile set
from them.
"""

from __future__ import annotations

#: Profiles written for one specific pipeline stage: never a worker route.
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

#: The supervisor is a daemon-wide singleton that lives outside the agents
#: table, and is never a task route.
EXCLUDED_PROFILE_IDS: frozenset[str] = frozenset({"supervisor"})
