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

* ``default-pipeline`` — a reviewer-authored deterministic graph
  (``_default_pipeline_body``): three single-command rules whose refusals
  reach a ``failed`` terminal, the commit rule filtered to approved human gates.
* ``default-assignment-routing`` — a reviewer-authored deterministic graph
  (``_default_assignment_routing_body``): read options, decide (LLM) when the
  class is not explicit, write the route.  Spec:
  ``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``.

Usage::

    python scripts/rebuild-reviewed-playbook-artifacts.py            # rewrite fixtures
    python scripts/rebuild-reviewed-playbook-artifacts.py --check    # diff only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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
    "ci-main-sentinel": "src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md",
    "blocked-task-escalation": "src/prompts/default_playbooks/blocked-task-escalation.md",
    "provider-usage-probe": "src/prompts/default_playbooks/provider-usage-probe.md",
}
SOURCES = SHIPPED

#: A terminal step of a recorded body is authorised by the "Failure handling,
#: uniformly" section, the prose that says a rule ends rather than retries.
_TERMINAL_HEADING = "## Failure handling, uniformly"

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


def _recorded_semantic_body(playbook_id: str) -> dict[str, Any]:
    """Read executable semantics from the artifact that reviewers approved."""
    payload = json.loads(
        (FIXTURE_ROOT / playbook_id / "artifact.json").read_text(encoding="utf-8")
    )
    return {"rules": payload["rules"], "steps": payload["steps"]}


def _default_pipeline_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``default-pipeline``.

    Three rules of one command step each.  The success outcomes the prose names
    reach the rule's ``completed`` terminal; ``rejected``, ``runtime_error`` and
    the commit's ``not_approved`` reach a distinct ``failed`` terminal, so a
    refused step never reads as a finished run.  The commit rule dispatches
    only for an approved human gate that names a proposal, and hands the gate
    and project to ``task_batch_commit`` so the command re-checks the exact
    decision (policy-simplification task agile-glacier.2).
    """
    index = ProseIndex(source, source.vault_path)
    terminal_ref = _source_ref_for_heading(source, "## Failure handling")
    rules: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}

    def lit(value: Any) -> dict[str, Any]:
        return {"type": "literal", "value": value}

    def event(path: str) -> dict[str, Any]:
        return {"type": "event_ref", "path": path}

    def template(*parts: dict[str, Any]) -> dict[str, Any]:
        return {"type": "template", "parts": list(parts)}

    def add(
        rule: str,
        trigger: dict[str, Any],
        title: str,
        command: str,
        inputs: dict[str, Any],
        successes: tuple[str, ...],
        failures: tuple[str, ...] = ("rejected", "runtime_error"),
        guard: dict[str, Any] | None = None,
    ) -> None:
        entry, done, failed = f"{rule}--{title}", f"{rule}--done", f"{rule}--failed"
        declared: dict[str, Any] = {
            "id": rule, "name": rule, "trigger": trigger, "entry_step": entry,
            "source": index.rule_ref(rule),
        }
        if guard is not None:
            declared["guard"] = guard
        rules.append(declared)
        transitions = {name: done for name in successes}
        transitions.update({name: failed for name in failures})
        steps[entry] = {
            "type": "command", "rule": rule, "title": title,
            "source": index.step_ref(rule, 1), "command": command,
            "inputs": inputs, "transitions": transitions,
        }
        steps[done] = _terminal(rule, "completed", terminal_ref)
        steps[failed] = _terminal(rule, "failed", terminal_ref)

    add(
        "spec-ingest-on-approve",
        {"event_type": "spec.approved"},
        "spec_ingest_gate",
        "ensure_task",
        {
            "project_id": event("project_id"),
            "dedup_key": template(lit("spec-ingest:"), event("spec_path")),
            "title": template(lit("Ingest spec "), event("spec_path")),
            "profile_id": lit("spec-ingest"),
            "intelligence_class": lit("standard-high"),
            "description": lit(
                "Read this spec, list existing tasks in the project, and emit "
                "task_batch_propose with the derived task graph. Iterate on "
                "validation errors."
            ),
        },
        ("created", "reused"),
    )
    add(
        "proposal-ready-gate",
        {"event_type": "proposal.ready"},
        "proposal_ready_gate",
        "gate_create",
        {
            "project_id": event("project_id"),
            "gate_type": lit("human"),
            "title": lit("Approve task batch?"),
            "question": template(lit("Approve proposal "), event("proposal_id"), lit("?")),
            "await_id": event("proposal_id"),
        },
        ("created", "reused", "skipped"),
    )
    add(
        "commit-on-gate-resolve",
        {
            "event_type": "gate.resolved",
            "filter": {"gate_type": "human", "resolution": ["approve", "approved"]},
        },
        "commit_proposal",
        "task_batch_commit",
        {
            "proposal_id": event("await_id"),
            "gate_id": event("gate_id"),
            "project_id": event("project_id"),
        },
        ("committed", "already_committed"),
        failures=("not_approved", "rejected", "runtime_error"),
        guard={"type": "exists", "value": event("await_id"), "mode": "truthy"},
    )
    return {"rules": rules, "steps": steps}


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
        return _default_pipeline_body(source)
    if playbook_id == "default-assignment-routing":
        return _default_assignment_routing_body(source)
    if playbook_id == "ci-main-sentinel":
        return _ci_main_sentinel_body(source)
    if playbook_id == "blocked-task-escalation":
        return _blocked_task_escalation_body(source)
    if playbook_id == "provider-usage-probe":
        return _provider_usage_probe_body(source)
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
    promote, rebuild, ci, dispatch = (
        f"{rule}--promote",
        f"{rule}--rebuild",
        f"{rule}--ci",
        f"{rule}--dispatch",
    )
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
                                "conflict": dispatch})
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
    steps[dispatch] = {
        "type": "command",
        "rule": rule,
        "title": "dispatch-rebuilt-conflict",
        "source": ref(rule),
        "command": "integration_repair_dispatch",
        "inputs": {"operation_id": event("operation_id")},
        "transitions": {
            name: (
                done
                if name in {"dispatched", "already_dispatched", "writer_reused"}
                else failed
            )
            for name in (
                "dispatched",
                "already_dispatched",
                "writer_reused",
                "busy",
                "configuration_blocked",
                "stale",
                "human_required",
                "runtime_error",
            )
        },
    }

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


def _blocked_task_escalation_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``blocked-task-escalation``.

    One command step and two terminals: a ``task.failed`` event filtered to
    ``status == "BLOCKED"`` wakes the task's one durable recovery incident
    through ``task_recovery_notify`` -- the record the periodic recovery scan
    also reaches, so the event, its replay and the scan never notify twice.
    See ``docs/superpowers/specs/2026-09-06-blocked-task-escalation-design.md``.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "escalate-blocked-task"
    notify = f"{rule}--notify_supervisor"
    done = f"{rule}--done"
    failed = f"{rule}--failed"

    def event(path: str) -> dict[str, Any]:
        return {"type": "event_ref", "path": path}

    return {
        "rules": [
            {
                "id": rule,
                "name": rule,
                "trigger": {"event_type": "task.failed", "filter": {"status": "BLOCKED"}},
                "entry_step": notify,
                "source": index.rule_ref(rule),
            }
        ],
        "steps": {
            notify: {
                "type": "command",
                "rule": rule,
                "title": "notify_supervisor",
                "source": index.step_ref(rule, 1),
                "command": "task_recovery_notify",
                "inputs": {
                    "task_id": event("task_id"),
                    "project_id": event("project_id"),
                },
                "save_result_as": "incident",
                "transitions": {
                    "queued": done,
                    "existing": done,
                    "not_actionable": done,
                    "retired": done,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            done: _terminal(rule, "completed", index.step_ref(rule, None)),
            failed: _terminal(rule, "failed", index.step_ref(rule, None)),
        },
    }


def _provider_usage_probe_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``provider-usage-probe``.

    One command step and two terminals: every ``timer.10m`` tick calls
    ``provider_usage_probe`` for ``claude``.  The five outcomes the prose
    names as successful all reach the completed terminal; a rejection or a
    runtime error reaches the failed one.  See
    ``docs/superpowers/specs/2026-09-07-provider-usage-implementation.md`` T4.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "probe-claude-usage"
    probe = f"{rule}--probe"
    done = f"{rule}--done"
    failed = f"{rule}--failed"

    transitions = {name: done for name in (
        "probed", "unparsed", "not_applicable", "unavailable", "disabled")}
    transitions["rejected"] = failed
    transitions["runtime_error"] = failed

    return {
        "rules": [
            {
                "id": rule,
                "name": rule,
                "trigger": {"event_type": "timer.10m"},
                "entry_step": probe,
                "source": index.rule_ref(rule),
            }
        ],
        "steps": {
            probe: {
                "type": "command",
                "rule": rule,
                "title": "probe",
                "source": index.step_ref(rule, 1),
                "command": "provider_usage_probe",
                "inputs": {"provider": {"type": "literal", "value": "claude"}},
                "save_result_as": "usage",
                "transitions": transitions,
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
