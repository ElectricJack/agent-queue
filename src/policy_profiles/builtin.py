"""Portable, deliberately applied project policy shipped with AQ.

Built from the same reviewed sources used by export, without reading an
operator vault or installing anything. It round-trips through aq.policy.v1.
"""

from __future__ import annotations

import json
from importlib.metadata import version

from src.playbooks.required import reviewed_bundle_source
from src.policy_profiles.models import Bundle, PlaybookPolicy, RoutingPreferences, TemplatePolicy
from src.policy_profiles.service import item

DEFAULT_PROJECT_PLAYBOOK_IDS = ("default-pipeline", "supervisor-failure-triage")


def default_policy() -> Bundle:
    entries = []
    for identifier in (*DEFAULT_PROJECT_PLAYBOOK_IDS, "default-assignment-routing"):
        root = reviewed_bundle_source() / identifier
        entries.append(item(
            identifier,
            "project" if identifier in DEFAULT_PROJECT_PLAYBOOK_IDS else "system",
            PlaybookPolicy(
                source=(root / "source.md").read_text(encoding="utf-8"),
                artifact=json.loads((root / "artifact.json").read_text(encoding="utf-8")),
            ),
        ))
    entries.append(item(
        "preferences", "project",
        RoutingPreferences(assignment_playbook_id="default-assignment-routing"),
    ))
    for kind, content in (
        ("spec", "# Specification\n\n## Problem\n\n## Behavior\n\n## Acceptance criteria\n"),
        ("plan", "# Implementation plan\n\n## Scope\n\n## Changes\n\n## Verification\n"),
    ):
        entries.append(item(f"{kind}.default", "project", TemplatePolicy(kind=kind, content=content)))
    return Bundle(name="default", source_aq_version=version("agent-queue"), items=entries)
