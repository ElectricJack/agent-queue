"""Source and artifact checks for the shipped ``pr-merge-sweep`` playbook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import (
    canonical_bytes,
    load_definition_json,
    source_digest,
)

FIXTURE = Path("tests/fixtures/playbooks/v2/pr-merge-sweep")


def test_authoring_accepts_a_project_qualified_scope() -> None:
    loaded = PlaybookSource.load(FIXTURE / "source.md", vault_root=FIXTURE)

    assert isinstance(loaded, PlaybookSource)
    assert loaded.frontmatter["scope"] == "project:agent-queue"


def test_source_is_prose_and_names_the_project_scope() -> None:
    source = (FIXTURE / "source.md").read_text(encoding="utf-8")

    assert "scope: project:agent-queue" in source
    assert "```json" not in source
    assert "`ensure_task`" in source
    assert "`task_route`" in source


def test_artifact_defines_the_merge_sweep_commands_and_arguments() -> None:
    definition = load_definition_json((FIXTURE / "artifact.json").read_text(encoding="utf-8"))
    steps = definition.steps
    ensure = steps["sweep-open-prs--ensure_sweep_task"]
    route = steps["sweep-open-prs--route_sweep_task"]

    assert definition.id == "pr-merge-sweep"
    assert definition.scope.type == "project"
    assert definition.scope.project_id == "agent-queue"
    assert [rule.trigger.event_type for rule in definition.rules] == ["timer.30m"]
    assert definition.rules[0].guard is None
    assert ensure.command == "ensure_task"
    assert route.command == "task_route"
    assert {name: value.model_dump(mode="json") for name, value in ensure.inputs.items()} == {
        "project_id": {"type": "literal", "value": "agent-queue"},
        "dedup_key": {"type": "literal", "value": "pr-merge-sweep"},
        "title": {"type": "literal", "value": "Merge open PRs (sweep)"},
        "description": {
            "type": "literal",
            "value": (
                "Batch-merge all open pull requests for this repo. Follow the pr-merger "
                "profile procedure exactly: skip drafts, do-not-merge/wip labels, and very "
                "fresh PRs; merge every MERGEABLE PR immediately with pr_merge (merge commit) "
                "WITHOUT running tests; for CONFLICTING PRs merge origin/main into the branch, "
                "resolve conflicts preserving both sides, then run ONLY the targeted tests for "
                "touched modules plus their area suite (-n auto), fix what they find, push, "
                "merge. Leave unsafe conflicts open with a PR comment. Close with the list of "
                "merged / conflict-resolved / skipped PRs (or 'no open PRs')."
            ),
        },
        "profile_id": {"type": "literal", "value": "pr-merger"},
        "priority": {"type": "literal", "value": 15},
    }
    assert ensure.save_result_as == "sweep"
    assert ensure.transitions == {
        "created": "sweep-open-prs--route_sweep_task",
        "reused": "sweep-open-prs--route_sweep_task",
        "rejected": "sweep-open-prs--done",
        "runtime_error": "sweep-open-prs--done",
    }
    assert {name: value.model_dump(mode="json") for name, value in route.inputs.items()} == {
        "task_id": {"type": "binding_ref", "binding": "sweep", "path": "task_id"},
        "profile_id": {"type": "literal", "value": "pr-merger"},
        "intelligence_class": {"type": "literal", "value": "deep-medium"},
    }
    assert route.transitions == {
        "routed": "sweep-open-prs--done",
        "rejected": "sweep-open-prs--done",
        "runtime_error": "sweep-open-prs--done",
    }


def test_profile_snapshot_is_the_profile_fingerprint_bound_into_the_artifact() -> None:
    """The artifact is bound to the *shipped* ``pr-merger`` profile's fingerprint."""
    from src.playbooks.profiles import shipped_profile_lookup

    definition = load_definition_json((FIXTURE / "artifact.json").read_text(encoding="utf-8"))
    policy = shipped_profile_lookup().policy("pr-merger")
    assert policy is not None

    assert definition.compiled_against.profiles == {"pr-merger": policy.fingerprint()}


def test_artifact_is_canonical_and_bound_to_the_source() -> None:
    raw = (FIXTURE / "artifact.json").read_bytes()
    definition = load_definition_json(raw.decode("utf-8"))
    source = (FIXTURE / "source.md").read_text(encoding="utf-8")

    assert canonical_bytes(definition) == raw
    assert definition.source_hash == source_digest(source)
    assert (FIXTURE / "artifact.sha256").read_text(encoding="utf-8").strip() == (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )
    assert json.loads((FIXTURE / "diagnostics.json").read_text(encoding="utf-8")) == []


def test_parity_coverage_records_the_timer_case_and_project_boundary() -> None:
    parity = json.loads((FIXTURE / "parity.json").read_text(encoding="utf-8"))

    assert parity["artifact_sha256"] == (FIXTURE / "artifact.sha256").read_text().strip()
    assert parity["coverage"] == [
        {
            "event_type": "timer.30m",
            "guard": "absent",
            "project_id": "agent-queue",
            "result": "commands, arguments, binding, and terminal transitions covered",
        }
    ]
