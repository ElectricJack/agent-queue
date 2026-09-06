#!/usr/bin/env python3
"""Rebuild the reviewed V2 artifact fixtures for the shipped playbooks.

**Nothing in CI, the daemon, or the release check runs this.**  It is the
recording aid for the human procedure in Package 6's child plan §5.3 (T-8):
a person runs it, reads the semantic diff, resolves every compiler question,
and only then checks the output in beside a hand-written ``review.md``.  The
fixtures are the approved recording; the suites validate them and never
regenerate them (child plan §5.3, "Determinism note").

Two of the four shipped playbooks retain reviewer-approved deterministic
semantic bodies, without an LLM:

* ``default-pipeline`` — reads the semantic body from its reviewed artifact and
  remaps source references onto the current prose.
* ``default-assignment-routing`` — a reviewer-authored deterministic graph
  (``_default_assignment_routing_body``): read options, decide (LLM) when the
  class is not explicit, write the route.  Spec:
  ``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``.

``memory-consolidation`` uses a deterministic, reviewer-authored semantic body
that preserves the prose as the LLM prompt and adds the typed envelope:
triggers, profiles, budgets, tool ceilings, output schemas, and terminal
transitions. ``pr-merge-sweep`` follows the same reviewed-artifact approach as
``default-pipeline``.

Usage::

    python scripts/rebuild-reviewed-playbook-artifacts.py            # rewrite fixtures
    python scripts/rebuild-reviewed-playbook-artifacts.py --check    # diff only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.playbooks.authoring import PlaybookSource  # noqa: E402
from src.playbooks.definition import canonical_bytes  # noqa: E402
from src.playbooks.profiles import shipped_profile_lookup  # noqa: E402
from src.playbooks.proposal import propose  # noqa: E402
from src.playbooks.validation import (  # noqa: E402
    RegisteredEventLookup,
    RegistryContractLookup,
)

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2"
SHIPPED = {
    "default-pipeline": "src/prompts/default_playbooks/default-pipeline.md",
    "default-assignment-routing": "src/prompts/default_playbooks/default-assignment-routing.md",
    "memory-consolidation": "src/prompts/default_playbooks/memory-consolidation.md",
    "pr-merge-sweep": "src/prompts/project_playbooks/agent-queue/pr-merge-sweep.md",
    "ci-main-sentinel": "src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md",
    "hierarchical-delivery": "src/prompts/default_playbooks/hierarchical-delivery.md",
    "root-integration-train": "src/prompts/default_playbooks/root-integration-train.md",
}
SOURCES = SHIPPED

#: Which numbered prose item authorises which lowered step (§5.3 review record).
#: A terminal step is authorised by the "Failure handling, uniformly" section,
#: which is the prose that says a rule ends rather than retries.
PIPELINE_STEP_PROSE: Mapping[str, tuple[str, int | None]] = {
    "per-task-review--create-review": ("per-task-review", 1),
    "per-task-review--link-discovered-from": ("per-task-review", 2),
    "per-task-review--fetch-downstream": ("per-task-review", 3),
    "per-task-review--gate-downstream": ("per-task-review", 4),
    "per-task-review--gate-downstream-body": ("per-task-review", 4),
    "per-task-review--done": ("per-task-review", None),
    "per-branch-final-review--ensure-final": ("per-branch-final-review", 1),
    "per-branch-final-review--ensure-review": ("per-branch-final-review", 2),
    "per-branch-final-review--link-blocks": ("per-branch-final-review", 3),
    "per-branch-final-review--fetch-downstream-branch": ("per-branch-final-review", 4),
    "per-branch-final-review--gate-downstream-pr-merged": ("per-branch-final-review", 5),
    "per-branch-final-review--gate-downstream-pr-merged-body": ("per-branch-final-review", 5),
    "per-branch-final-review--done": ("per-branch-final-review", None),
    "spec-ingest-on-approve--spec_ingest_gate": ("spec-ingest-on-approve", 1),
    "spec-ingest-on-approve--done": ("spec-ingest-on-approve", None),
    "proposal-ready-gate--proposal_ready_gate": ("proposal-ready-gate", 1),
    "proposal-ready-gate--done": ("proposal-ready-gate", None),
    "commit-on-gate-resolve--commit_proposal": ("commit-on-gate-resolve", 1),
    "commit-on-gate-resolve--done": ("commit-on-gate-resolve", None),
}

_TERMINAL_HEADING = "## Failure handling, uniformly"

PR_MERGE_SWEEP_STEP_PROSE: Mapping[str, tuple[str, int | None]] = {
    "sweep-open-prs--ensure_sweep_task": ("sweep-open-prs", 1),
    "sweep-open-prs--route_sweep_task": ("sweep-open-prs", 2),
    "sweep-open-prs--done": ("sweep-open-prs", None),
}


class ProseIndex:
    """1-based line numbers for a prose source's rule headings and list items."""

    def __init__(self, source: PlaybookSource, vault_path: str) -> None:
        self.vault_path = vault_path
        self._lines = source.raw.splitlines()
        self._rules: dict[str, int] = {}
        self._items: dict[str, dict[int, int]] = {}
        self._terminal = 1
        current: str | None = None
        for index, line in enumerate(self._lines, start=1):
            heading = re.match(r"^## Rule: (\S+)\s*$", line)
            if heading:
                current = heading.group(1)
                self._rules[current] = index
                self._items[current] = {}
                continue
            if line.startswith(_TERMINAL_HEADING):
                self._terminal = index
                current = None
                continue
            if line.startswith("## "):
                current = None
                continue
            ordered = re.match(r"^(\d+)\. ", line)
            if ordered and current is not None:
                self._items[current][int(ordered.group(1))] = index

    def _ref(self, line: int, heading: str | None) -> dict[str, Any]:
        excerpt = self._lines[line - 1].strip()
        ref: dict[str, Any] = {
            "path": self.vault_path,
            "start_line": line,
            "end_line": line,
        }
        if heading:
            ref["heading"] = heading
        if excerpt:
            ref["excerpt"] = excerpt
        return ref

    def rule_ref(self, rule_id: str) -> dict[str, Any]:
        return self._ref(self._rules[rule_id], f"Rule: {rule_id}")

    def step_ref(self, rule_id: str, ordinal: int | None) -> dict[str, Any]:
        if ordinal is None:
            return self._ref(self._terminal, _TERMINAL_HEADING.removeprefix("## "))
        return self._ref(self._items[rule_id][ordinal], f"Rule: {rule_id}")


def _remap_pipeline_refs(body: dict[str, Any], index: ProseIndex) -> dict[str, Any]:
    for rule in body["rules"]:
        rule["source"] = index.rule_ref(rule["id"])
    for step_id, step in body["steps"].items():
        rule_id, ordinal = PIPELINE_STEP_PROSE[step_id]
        step["source"] = index.step_ref(rule_id, ordinal)
    return body


def _recorded_semantic_body(playbook_id: str) -> dict[str, Any]:
    """Read executable semantics from the artifact that reviewers approved."""
    payload = json.loads(
        (FIXTURE_ROOT / playbook_id / "artifact.json").read_text(encoding="utf-8")
    )
    return {"rules": payload["rules"], "steps": payload["steps"]}


def profile_lookup(playbook_id: str | None = None) -> Any:
    """Resolve profiles from ``src/profiles/defaults/``, the shipped set.

    Production resolves profiles from the database (``_v2_lookups``); a fixture
    must not depend on one operator's install, so the reviewed artifact is held
    to the profiles this repository ships — the same lookup
    ``tests/test_default_playbook_v2_artifacts.py`` later holds the fixture to.
    ``pr-merger`` ships there too since the V2 cutover, so the sweep no longer
    needs a staged copy of its profile.
    """
    del playbook_id
    return shipped_profile_lookup()


def _load(rel_path: str) -> PlaybookSource:
    path = REPO_ROOT / rel_path
    loaded = PlaybookSource.load(path, vault_root=path.parent)
    if not isinstance(loaded, PlaybookSource):
        raise SystemExit(f"{rel_path}: {loaded.errors}")
    return loaded


def semantic_body(playbook_id: str, source: PlaybookSource) -> dict[str, Any]:
    if playbook_id == "default-pipeline":
        body = _recorded_semantic_body(playbook_id)
        return _remap_pipeline_refs(json.loads(json.dumps(body)), ProseIndex(source, source.vault_path))
    if playbook_id == "default-assignment-routing":
        return _default_assignment_routing_body(source)
    if playbook_id == "pr-merge-sweep":
        body = _recorded_semantic_body(playbook_id)
        remapped = json.loads(json.dumps(body))
        index = ProseIndex(source, source.vault_path)
        for rule in remapped["rules"]:
            rule["source"] = index.rule_ref(rule["id"])
        for step_id, step in remapped["steps"].items():
            rule_id, ordinal = PR_MERGE_SWEEP_STEP_PROSE[step_id]
            step["source"] = index.step_ref(rule_id, ordinal)
        return remapped
    if playbook_id == "memory-consolidation":
        return _memory_consolidation_body(source)
    if playbook_id == "ci-main-sentinel":
        return _ci_main_sentinel_body(source)
    if playbook_id == "hierarchical-delivery":
        return _recorded_semantic_body(playbook_id)
    if playbook_id == "root-integration-train":
        return _root_integration_train_body(source)
    return {}


def _root_integration_train_body(source: PlaybookSource) -> dict[str, Any]:
    def event(path: str) -> dict[str, Any]:
        return {"type": "event_ref", "path": path}

    def bound(binding: str, path: str) -> dict[str, Any]:
        return {"type": "binding_ref", "binding": binding, "path": path}

    def ref(rule: str) -> dict[str, Any]:
        return _source_ref_for_heading(source, f"## Rule: {rule}")

    terminal_ref = _source_ref_for_heading(source, "## Failure handling")
    rules: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}

    def terminals(rule: str) -> tuple[str, str]:
        done, failed = f"{rule}--done", f"{rule}--failed"
        steps[done] = _terminal(rule, "completed", terminal_ref)
        steps[failed] = _terminal(rule, "failed", terminal_ref)
        return done, failed

    rule = "seal-due-frontier"
    done, failed = terminals(rule)
    seal, release = f"{rule}--seal", f"{rule}--release-empty"
    rules.append({"id": rule, "name": rule, "trigger": {"event_type": "integration.sweep_due"},
                  "entry_step": seal, "source": ref(rule)})
    steps[seal] = {"type": "command", "rule": rule, "title": "seal", "source": ref(rule),
                   "command": "integration_seal", "save_result_as": "sealed",
                   "inputs": {"project_id": event("project_id"), "request_id": event("operation_id")},
                   "transitions": {"sealed": done, "empty": release, "busy": failed,
                                   "runtime_error": failed}}
    steps[release] = {"type": "command", "rule": rule, "title": "release-empty",
                      "source": ref(rule), "command": "integration_release",
                      "inputs": {"batch_id": bound("sealed", "batch_id")},
                      "transitions": {name: (done if name in {"released", "already_released", "empty"} else failed)
                                      for name in ("released", "already_released", "empty", "wait", "stale",
                                                   "invariant_error", "runtime_error")}}

    rule = "construct-and-test"
    done, failed = terminals(rule)
    build, ci, dispatch = (f"{rule}--build", f"{rule}--ci", f"{rule}--dispatch")
    rules.append({"id": rule, "name": rule, "trigger": {"event_type": "integration.sealed"},
                  "entry_step": build, "source": ref(rule)})
    build_transitions = {name: failed for name in (
        "source_moved", "base_moved", "stale_revision", "wait", "human_required",
        "configuration_blocked", "runtime_error")}
    build_transitions.update({"empty": done, "built": ci, "already_built": ci,
                              "conflict": dispatch})
    steps[build] = {"type": "command", "rule": rule, "title": "build", "source": ref(rule),
                    "command": "integration_build_candidate", "save_result_as": "candidate",
                    "inputs": {"batch_id": event("batch_id")}, "transitions": build_transitions}
    ci_transitions = {name: failed for name in (
        "full_suite_required", "stale_subject", "configuration_blocked", "runtime_error")}
    ci_transitions.update({"green": done, "red": done, "pending": done})
    steps[ci] = {"type": "command", "rule": rule, "title": "ci", "source": ref(rule),
                 "command": "integration_ci_evidence", "inputs": {
                     "batch_id": event("batch_id"), "revision": bound("candidate", "revision")},
                 "transitions": ci_transitions}
    steps[dispatch] = {"type": "command", "rule": rule, "title": "dispatch", "source": ref(rule),
                       "command": "integration_repair_dispatch", "inputs": {
                           "operation_id": event("operation_id")},
                       "transitions": {name: (done if name in {"dispatched", "already_dispatched", "writer_reused"}
                                              else failed) for name in (
                           "dispatched", "already_dispatched", "writer_reused", "busy",
                           "configuration_blocked", "stale", "human_required", "runtime_error")}}

    rule = "promote-green-candidate"
    done, failed = terminals(rule)
    promote, rebuild, ci = (f"{rule}--promote", f"{rule}--rebuild", f"{rule}--ci")
    rules.append({"id": rule, "name": rule, "trigger": {"event_type": "integration.candidate_green"},
                  "entry_step": promote, "source": ref(rule)})
    promote_transitions = {name: failed for name in (
        "ci_missing", "non_fast_forward", "wait", "reconciliation_blocked", "stale",
        "configuration_blocked", "runtime_error")}
    promote_transitions.update({"promoted": done, "already_promoted": done,
                                "base_moved": rebuild})
    steps[promote] = {"type": "command", "rule": rule, "title": "promote", "source": ref(rule),
                      "command": "integration_promote_main", "inputs": {
                          "batch_id": event("batch_id"), "revision": event("revision")},
                      "transitions": promote_transitions}
    rebuild_transitions = {name: failed for name in (
        "source_moved", "base_moved", "stale_revision", "wait", "human_required",
        "configuration_blocked", "runtime_error")}
    rebuild_transitions.update({"empty": done, "built": ci, "already_built": ci,
                                "conflict": failed})
    steps[rebuild] = {"type": "command", "rule": rule, "title": "rebuild", "source": ref(rule),
                      "command": "integration_build_candidate", "inputs": {"batch_id": event("batch_id")},
                      "save_result_as": "rebuilt", "transitions": rebuild_transitions}
    green_ci_transitions = {name: failed for name in (
        "full_suite_required", "stale_subject", "configuration_blocked", "runtime_error")}
    green_ci_transitions.update({"green": done, "red": done, "pending": done})
    steps[ci] = {"type": "command", "rule": rule, "title": "ci-rebuilt", "source": ref(rule),
                 "command": "integration_ci_evidence", "inputs": {
                     "batch_id": event("batch_id"), "revision": bound("rebuilt", "revision")},
                 "transitions": green_ci_transitions}

    rule = "repair-red-candidate"
    done, failed = terminals(rule)
    dispatch = f"{rule}--dispatch"
    rules.append({"id": rule, "name": rule, "trigger": {"event_type": "integration.candidate_red"},
                  "entry_step": dispatch, "source": ref(rule)})
    steps[dispatch] = {"type": "command", "rule": rule, "title": "dispatch", "source": ref(rule),
                       "command": "integration_repair_dispatch", "inputs": {
                           "operation_id": event("operation_id"),
                           "batch_id": event("batch_id"),
                           "revision": event("revision"),
                           "head_sha": event("head_sha")},
                       "transitions": {name: (done if name in {"dispatched", "already_dispatched", "writer_reused"}
                                              else failed) for name in (
                           "dispatched", "already_dispatched", "writer_reused", "busy",
                           "configuration_blocked", "stale", "human_required", "runtime_error")}}

    rule = "dispatch-debug"
    done, failed = terminals(rule)
    entry = f"{rule}--dispatch"
    rules.append({"id": rule, "name": rule, "trigger": {"event_type": "integration.repair_exhausted"},
                  "entry_step": entry, "source": ref(rule)})
    steps[entry] = {"type": "command", "rule": rule, "title": "dispatch", "source": ref(rule),
                    "command": "integration_repair_dispatch", "inputs": {
                        "operation_id": event("operation_id"),
                        "stage": {"type": "literal", "value": 1}},
                    "transitions": {name: (done if name in {"dispatched", "already_dispatched", "writer_reused"}
                                           else failed) for name in (
                        "dispatched", "already_dispatched", "writer_reused", "busy",
                        "configuration_blocked", "stale", "human_required", "runtime_error")}}

    for rule, event_type, command in (
        ("release-promoted", "integration.batch_promoted", "integration_release"),
        ("cleanup-promoted", "integration.cleanup_requested", "integration_cleanup"),
    ):
        done, failed = terminals(rule)
        entry = f"{rule}--run"
        rules.append({"id": rule, "name": rule, "trigger": {"event_type": event_type},
                      "entry_step": entry, "source": ref(rule)})
        successes = {"released", "already_released", "empty"} if command == "integration_release" else {
            "materialized", "advanced", "complete", "already_complete"}
        outcomes = (
            ("released", "already_released", "empty", "wait", "stale", "invariant_error", "runtime_error")
            if command == "integration_release"
            else ("materialized", "advanced", "complete", "already_complete", "wait", "retryable",
                  "conflict", "failed", "stale", "invariant_error", "runtime_error")
        )
        steps[entry] = {"type": "command", "rule": rule, "title": "run", "source": ref(rule),
                        "command": command, "inputs": {"batch_id": event("batch_id")},
                        "transitions": {name: (done if name in successes else failed) for name in outcomes}}
    return {"rules": rules, "steps": steps}


def _section(source: PlaybookSource, heading: str, until: str) -> str:
    """The prose between *heading* and the next heading starting with *until*."""
    lines = source.raw.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(until)), len(lines)
    )
    return "\n".join(lines[start : end]).strip()


def _default_assignment_routing_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``default-assignment-routing``.

    One rule on ``task.route_needed``: read the task's routing options, let
    the LLM decide only when the class is not explicit, then write the route
    with ``task_route``.  Every step carries the numbered prose line that
    authorises it.  Spec:
    ``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "route-task"
    read = f"{rule}--read_options"
    choose = f"{rule}--choose"
    apply_explicit = f"{rule}--apply_explicit"
    apply_decision = f"{rule}--apply_decision"
    done = f"{rule}--done"
    failed = f"{rule}--failed"
    task_id = {"type": "event_ref", "path": "task_id"}

    def routing(path: str) -> dict[str, Any]:
        return {"type": "binding_ref", "binding": "routing", "path": path}

    def decision(path: str) -> dict[str, Any]:
        return {"type": "binding_ref", "binding": "decision", "path": path}

    return {
        "rules": [
            {
                "id": rule,
                "name": rule,
                "trigger": {"event_type": "task.route_needed"},
                "entry_step": read,
                "source": index.rule_ref(rule),
            }
        ],
        "steps": {
            read: {
                "type": "command",
                "rule": rule,
                "title": "read_options",
                "source": index.step_ref(rule, 1),
                "command": "task_route_options",
                "inputs": {"task_id": task_id},
                "save_result_as": "routing",
                "transitions": {
                    "already_routed": done,
                    "explicit": apply_explicit,
                    "undecided": choose,
                    "no_options": failed,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            choose: {
                "type": "llm",
                "rule": rule,
                "title": "choose",
                "source": index.step_ref(rule, 2),
                "profile_id": "playbook-compiler",
                "prompt": {
                    "type": "literal",
                    "value": _section(source, "## Choosing a class", "## "),
                },
                "inputs": {
                    "title": routing("title"),
                    "description": routing("description"),
                    "priority": routing("priority"),
                    "task_type": routing("task_type"),
                    "options": routing("options"),
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "intelligence_class": {"type": "string"},
                        "provider": {"type": ["string", "null"]},
                        "profile_id": {"type": "string"},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                    },
                    "required": ["intelligence_class", "provider", "profile_id", "reason"],
                    "additionalProperties": False,
                },
                "budget": {
                    "max_calls": 1,
                    "max_output_tokens": 4096,
                    "max_total_tokens": 4096,
                    "timeout_seconds": 300,
                },
                "tool_use": {"enabled": False, "aq_commands": [], "plugin_tools": []},
                "save_result_as": "decision",
                "transitions": _llm_transitions(apply_decision, failed),
            },
            apply_explicit: {
                "type": "command",
                "rule": rule,
                "title": "apply_explicit",
                "source": index.step_ref(rule, 3),
                "command": "task_route",
                "inputs": {
                    "task_id": task_id,
                    "profile_id": routing("explicit_profile_id"),
                    "intelligence_class": routing("intelligence_class"),
                    "reason": {"type": "literal", "value": "explicit intelligence class"},
                },
                "transitions": {"routed": done, "rejected": failed, "runtime_error": failed},
            },
            apply_decision: {
                "type": "command",
                "rule": rule,
                "title": "apply_decision",
                "source": index.step_ref(rule, 4),
                "command": "task_route",
                "inputs": {
                    "task_id": task_id,
                    "profile_id": decision("profile_id"),
                    "intelligence_class": decision("intelligence_class"),
                    "reason": decision("reason"),
                },
                "transitions": {"routed": done, "rejected": failed, "runtime_error": failed},
            },
            done: _terminal(rule, "completed", index.step_ref(rule, None)),
            failed: _terminal(rule, "failed", index.step_ref(rule, None)),
        },
    }


def _ci_main_sentinel_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``ci-main-sentinel``.

    Three command steps and two terminals, lowered from the numbered prose
    items so every step carries the line that authorises it.  See
    ``docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md``.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "keep-main-green"
    observe = f"{rule}--read_baseline"
    repair = f"{rule}--ensure_repair_task"
    escalate = f"{rule}--escalate_to_human"
    done = f"{rule}--done"
    failed = f"{rule}--failed"
    project = {"type": "literal", "value": "agent-queue"}

    def bound(path: str) -> dict[str, Any]:
        return {"type": "binding_ref", "binding": "baseline", "path": path}

    return {
        "rules": [
            {
                "id": rule,
                "name": rule,
                "trigger": {"event_type": "timer.15m"},
                "entry_step": observe,
                "source": index.rule_ref(rule),
            }
        ],
        "steps": {
            observe: {
                "type": "command",
                "rule": rule,
                "title": "read_baseline",
                "source": index.step_ref(rule, 1),
                "command": "ci_baseline_status",
                "inputs": {"project_id": project},
                "save_result_as": "baseline",
                "transitions": {
                    "green": done,
                    "pending": done,
                    "unknown": done,
                    "red": repair,
                    "red_escalated": escalate,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            repair: {
                "type": "command",
                "rule": rule,
                "title": "ensure_repair_task",
                "source": index.step_ref(rule, 2),
                "command": "ensure_task",
                "inputs": {
                    "project_id": project,
                    "dedup_key": bound("dedup_key"),
                    "title": bound("title"),
                    "description": bound("description"),
                    "priority": {"type": "literal", "value": 5},
                    "intelligence_class": {"type": "literal", "value": "deep-high"},
                },
                "save_result_as": "repair",
                "transitions": {
                    "created": done,
                    "reused": done,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            escalate: {
                "type": "command",
                "rule": rule,
                "title": "escalate_to_human",
                "source": index.step_ref(rule, 3),
                "command": "gate_create",
                "inputs": {
                    "project_id": project,
                    "gate_type": {"type": "literal", "value": "human"},
                    "title": bound("escalation_title"),
                    "question": bound("escalation_question"),
                    "await_id": bound("escalation_key"),
                },
                "transitions": {
                    "created": done,
                    "reused": done,
                    "skipped": done,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            done: _terminal(rule, "completed", index.step_ref(rule, None)),
            failed: _terminal(rule, "failed", index.step_ref(rule, None)),
        },
    }


def _source_ref_for_heading(source: PlaybookSource, heading: str) -> dict[str, Any]:
    for line_no, line in enumerate(source.raw.splitlines(), start=1):
        if line.strip() == heading:
            return {
                "path": source.vault_path,
                "start_line": line_no,
                "end_line": line_no,
                "heading": heading.lstrip("# "),
                "excerpt": line,
            }
    raise ValueError(f"{source.vault_path}: missing heading {heading!r}")


def _llm_transitions(done: str, failed: str) -> dict[str, str]:
    return {"completed": done, "runtime_error": failed}


def _terminal(rule: str, outcome: str, source_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "terminal",
        "rule": rule,
        "title": outcome.title(),
        "source": source_ref,
        "outcome": outcome,
    }


def _memory_consolidation_body(source: PlaybookSource) -> dict[str, Any]:
    rule = "memory-consolidation"
    run = "memory-consolidation--run"
    done = "memory-consolidation--done"
    failed = "memory-consolidation--failed"
    source_ref = _source_ref_for_heading(source, "# Memory Consolidation")
    terminal_ref = _source_ref_for_heading(source, "## Step 3 — No-op terminal")
    return {
        "rules": [
            {
                "id": rule,
                "name": "Consolidate project memories",
                "trigger": {"event_type": "timer.24h"},
                "entry_step": run,
                "source": source_ref,
            }
        ],
        "steps": {
            run: {
                "type": "llm",
                "rule": rule,
                "title": "Select projects and create consolidation tasks",
                "source": source_ref,
                "profile_id": "supervisor",
                "prompt": {"type": "literal", "value": source.body.strip()},
                "inputs": {
                    "tick_time": {"type": "event_ref", "path": "tick_time"},
                    "interval": {"type": "event_ref", "path": "interval"},
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "tasks_created": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "project_id": {"type": "string"},
                                    "task_id": {"type": "string"},
                                },
                                "required": ["project_id", "task_id"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["tasks_created"],
                    "additionalProperties": False,
                },
                "budget": {
                    "max_calls": 50,
                    "max_output_tokens": 4096,
                    "max_total_tokens": 65536,
                    "timeout_seconds": 900,
                },
                "tool_use": {
                    "enabled": True,
                    "aq_commands": ["list_projects", "render_prompt", "create_task"],
                    "plugin_tools": [
                        "read_project_memory_file",
                        "count_project_memory_files",
                    ],
                },
                "transitions": _llm_transitions(done, failed),
            },
            done: _terminal(rule, "completed", terminal_ref),
            failed: _terminal(rule, "failed", terminal_ref),
        },
    }


def build(playbook_id: str) -> dict[str, Any]:
    """Compile one shipped source and report what a reviewer would see."""
    rel_path = SOURCES[playbook_id]
    source = _load(rel_path)
    body = semantic_body(playbook_id, source)
    if not body:
        return {
            "id": playbook_id,
            "rel_path": rel_path,
            "artifact": None,
            "diagnostics": [
                {
                    "severity": "question",
                    "code": "requires_agent_proposal",
                    "message": (
                        "prose playbook requires a compiler-agent proposal "
                        "that a human reviews"
                    ),
                }
            ],
        }
    proposal = propose(
        source,
        body,
        contracts=RegistryContractLookup(),
        profiles=profile_lookup(playbook_id),
        events=RegisteredEventLookup(),
        version=1,
        enforce_inventory=True,
    )
    diagnostics = [
        {
            "severity": d.severity,
            "code": d.code,
            "message": d.message,
            **({"rule_id": d.rule_id} if d.rule_id else {}),
            **({"step_id": d.step_id} if d.step_id else {}),
        }
        for d in proposal.diagnostics
    ]
    blocking = [d for d in diagnostics if d["severity"] in {"error", "question"}]
    return {
        "id": playbook_id,
        "rel_path": rel_path,
        "artifact": None if blocking or proposal.artifact is None else proposal.artifact,
        "artifact_sha256": proposal.artifact_sha256,
        "diagnostics": diagnostics,
    }


#: The one field a rebuild is expected to change.  `compiled_at` records when
#: the compile ran, so it differs on every rebuild by construction; comparing it
#: would make `--check` say "drift" every time and mean nothing.  Every other
#: byte of the artifact is deterministic, which is what `--check` verifies.
NON_DETERMINISTIC_FIELDS = ("compiled_at",)


def comparable(artifact_bytes: bytes) -> str:
    """Artifact JSON with the non-deterministic fields removed, canonically ordered."""
    payload = json.loads(artifact_bytes.decode("utf-8"))
    for field in NON_DETERMINISTIC_FIELDS:
        payload.pop(field, None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift in the deterministic fields, write nothing",
    )
    parser.add_argument("ids", nargs="*", default=None)
    args = parser.parse_args()

    drift = 0
    for playbook_id in args.ids or list(SOURCES):
        print(f"{playbook_id}:")
        result = build(playbook_id)
        directory = FIXTURE_ROOT / playbook_id
        directory.mkdir(parents=True, exist_ok=True)
        files: dict[Path, bytes] = {
            directory / "source.md": (REPO_ROOT / result["rel_path"]).read_bytes(),
            directory / "diagnostics.json": (
                json.dumps(result["diagnostics"], indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
        }
        artifact = result["artifact"]
        if artifact is not None:
            files[directory / "artifact.json"] = canonical_bytes(artifact)
            files[directory / "artifact.sha256"] = (
                result["artifact_sha256"] + "\n"
            ).encode("utf-8")
            print(f"  approvable: {result['artifact_sha256']}")
        else:
            for diagnostic in result["diagnostics"]:
                if diagnostic["severity"] in {"error", "question"}:
                    print(f"  BLOCKED {diagnostic['code']}: {diagnostic['message']}")
            for stale in ("artifact.json", "artifact.sha256"):
                if (directory / stale).exists():
                    print(f"  refusing to keep stale {stale}")
                    drift += 1
        for path, payload in files.items():
            existing = path.read_bytes() if path.exists() else None
            if existing == payload:
                continue
            if (
                args.check
                and path.name == "artifact.json"
                and existing is not None
                and comparable(existing) == comparable(payload)
            ):
                continue
            if args.check and path.name == "artifact.sha256" and existing is not None:
                # The hash covers `compiled_at`, so it moves whenever that does;
                # `artifact.json` above is the assertion that matters.
                continue
            drift += 1
            rel = path.relative_to(REPO_ROOT)
            if args.check:
                print(f"  DRIFT {rel}")
            else:
                path.write_bytes(payload)
                print(f"  wrote {rel}")
    if args.check and drift:
        print(f"\n{drift} fixture file(s) differ from a fresh build")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
