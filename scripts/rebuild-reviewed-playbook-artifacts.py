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
* ``default-assignment-routing`` — the routing semantic body
  (``_default_assignment_routing_body``): plan with ``task_route_plan`` over
  the source's ``## Routing policy`` block, passed verbatim as a string
  literal; classify (LLM) only when the plan asks; write the plan with
  ``task_route_apply``.  Spec: ``projects/agent-queue/specs/
  2026-09-28-mandatory-task-routing.md`` §6.3 and §6.7.

This writes the fixture bundle only.  Two trees hold byte-identical copies of
a reviewed recording and are not touched here — copy them across by hand after
a rebuild. Existing fixture ``manifest.md`` digests are refreshed from the
compiler output without changing their policy or review prose:

* ``src/prompts/reviewed_playbooks/<id>/`` — what the daemon seeds into the
  vault and activates (``src/playbooks/required.py``).  Guarded by
  ``test_daemon_shipped_bundle_is_the_reviewed_fixture``.  **This copy is the
  only supply line an install has**: ``playbook_v2_import`` refuses every path
  outside the vault root, so a rebuild that stops at the fixture leaves every
  install importing the superseded bytes it already had.
* ``docs/playbooks/integration-only/default-pipeline/`` — the operator-importable
  copy.  Guarded by ``test_integration_only_bundle_is_the_reviewed_fixture``.

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

import yaml

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
    "object-loop": "src/prompts/project_playbooks/matter-engine-cpp/object-loop.md",
    "supervisor-digest": "src/prompts/default_playbooks/supervisor-digest.md",
    "supervisor-hourly-report": "src/prompts/default_playbooks/supervisor-hourly-report.md",
    "default-pipeline": "src/prompts/default_playbooks/default-pipeline.md",
    "default-assignment-routing": "src/prompts/default_playbooks/default-assignment-routing.md",
    "ci-main-sentinel": "src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md",
    "agent-queue-root-train": (
        "src/prompts/project_playbooks/agent-queue/agent-queue-root-train.md"
    ),
    "agent-queue-parent-integration": (
        "src/prompts/project_playbooks/agent-queue/agent-queue-parent-integration.md"
    ),
    "blocked-task-escalation": "src/prompts/default_playbooks/blocked-task-escalation.md",
    "supervisor-failure-triage": "src/prompts/default_playbooks/supervisor-failure-triage.md",
    "provider-usage-probe": "src/prompts/default_playbooks/provider-usage-probe.md",
    "morning-report": "src/prompts/default_playbooks/morning-report.md",
    "provider-failover": "src/prompts/default_playbooks/provider-failover.md",
    "github-issue-triage": "src/prompts/project_playbooks/agent-queue/github-issue-triage.md",
    "parent-integration": "src/prompts/integration_playbooks/parent-integration.md",
    "root-train": "src/prompts/integration_playbooks/root-train.md",
    "promotion-request": "src/prompts/integration_playbooks/promotion-request.md",
    "promotion-continuous": "src/prompts/integration_playbooks/promotion-continuous.md",
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


def _rebased_recorded_body(template_id: str, source: PlaybookSource) -> dict[str, Any]:
    """A reviewed template's graph with every source ref moved onto ``source``.

    ``parent-integration`` is ``agent-queue-parent-integration`` at system
    scope: its rule prose is verbatim, so the reviewed graph is reused as is and
    each ref, which names a rule heading, moves to that heading's line in the
    new source.  A heading the new source lacks is a refusal, not a guess.
    """
    body = _recorded_semantic_body(template_id)
    template_lines = (
        (FIXTURE_ROOT / template_id / "source.md").read_text(encoding="utf-8").splitlines()
    )
    headings = {
        line.strip(): number
        for number, line in enumerate(source.raw.splitlines(), start=1)
        if line.startswith("## ")
    }

    def rebase(ref: dict[str, Any]) -> dict[str, Any]:
        heading = template_lines[ref["start_line"] - 1].strip()
        if heading not in headings:
            raise SystemExit(f"{source.vault_path}: missing template heading {heading!r}")
        line = headings[heading]
        return {**ref, "path": source.vault_path, "start_line": line, "end_line": line}

    for rule in body["rules"]:
        rule["source"] = rebase(rule["source"])
    for step in body["steps"].values():
        step["source"] = rebase(step["source"])
    return body


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
            "intelligence_class": lit("deep-high"),
            "description": template(
                lit("spec_path: "), event("spec_path"),
                lit(". Read spec_kind and check implementation grounding. Design specs get "
                    "one deep-high implementation-spec authoring child in an epic, submitted "
                    "to Jack's review queue. Implementation specs get parallel phase epics "
                    "and self-contained children. Validate, then task_batch_propose followed "
                    "directly by task_batch_commit; no human proposal gate. The batch "
                    "creates work only; updates to existing tasks need an approved change set."),
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
    if playbook_id in {"promotion-request", "promotion-continuous"}:
        return _promotion_policy_body(source)
    if playbook_id == "object-loop":
        return _object_loop_body(source)
    if playbook_id == "supervisor-hourly-report":
        return _supervisor_hourly_report_body(source)
    if playbook_id == "default-pipeline":
        return _default_pipeline_body(source)
    if playbook_id == "default-assignment-routing":
        return _default_assignment_routing_body(source)
    if playbook_id == "ci-main-sentinel":
        return _ci_main_sentinel_body(source)
    if playbook_id in ("agent-queue-root-train", "root-train"):
        return _root_integration_train_body(source)
    if playbook_id in ("parent-integration", "agent-queue-parent-integration"):
        body = (
            _rebased_recorded_body("agent-queue-parent-integration", source)
            if playbook_id == "parent-integration"
            else _recorded_semantic_body(playbook_id)
        )
        transitions = body["steps"]["reconcile-resolution-push--reconcile"]["transitions"]
        # Exact remote reconciliation may settle through a successor or wait
        # for its writer. Preserve the contract's success/refusal distinction.
        transitions.update({
            "continued": "reconcile-resolution-push--done",
            "superseded": "reconcile-resolution-push--done",
            "waiting": "reconcile-resolution-push--failed",
            "target_moved": "reconcile-resolution-push--failed",
        })
        return body
    if playbook_id == "blocked-task-escalation":
        return _blocked_task_escalation_body(source)
    if playbook_id == "supervisor-failure-triage":
        return _supervisor_failure_triage_body(source)
    if playbook_id == "provider-failover":
        return _provider_failover_body(source)
    if playbook_id == "provider-usage-probe":
        return _provider_usage_probe_body(source)
    if playbook_id == "morning-report":
        return _morning_report_body(source)
    if playbook_id == "supervisor-digest":
        return _supervisor_digest_body(source)
    if playbook_id == "github-issue-triage":
        return _github_issue_triage_body(source)
    return {}


def _object_loop_body(source: PlaybookSource) -> dict[str, Any]:
    """Acyclic per-event policy with finite foreach bodies and durable reads."""
    from src.commands.contracts.object_loop import ObjectLoopStartArgs, ObjectScoreRecordArgs

    rules, steps = [], {}
    sweep_ref = _source_ref_for_heading(source, "## Sweep")
    terminal_ref = _source_ref_for_heading(source, "## Failure handling, uniformly")

    def lit(value):
        return {"type": "literal", "value": value}

    def bound(binding, path):
        return {"type": "binding_ref", "binding": binding, "path": path}

    def item(path, binding="object"):
        return {"type": "loop_ref", "binding": binding, "path": path}

    def compare(left, op, right):
        return {"type": "comparison", "left": left, "op": op, "right": right}

    def truth(value):
        return {"type": "exists", "value": value, "mode": "truthy"}

    for rule, event in (
        ("on-formula", "formula.cooked"), ("on-completion", "task.completed"),
        ("on-failure", "task.failed"), ("on-review", "review.decided"),
        ("recover", "timer.5m"),
    ):
        def sid(name, rule=rule):
            return f"{rule}--{name}"

        rules.append({
            "id": rule, "name": rule, "trigger": {"event_type": event},
            "entry_step": sid("read"),
            "source": _source_ref_for_heading(source, f"## Rule: {rule}"),
        })

        def step(name, kind, *, rule=rule, **fields):
            steps[sid(name)] = {
                "type": kind, "rule": rule, "title": name,
                "source": sweep_ref, **fields,
            }

        def command(name, cmd, inputs, target, binding=None, rejected="failed"):
            step(name, "command", command=cmd, inputs=inputs,
                 transitions={"completed": sid(target), "rejected": sid(rejected),
                              "runtime_error": sid("failed")},
                 retry={"max_attempts": 2, "backoff_seconds": 1,
                        "retry_on": ["runtime_error"]},
                 **({"save_result_as": binding} if binding else {}))

        def decision(name, cases, default):
            step(name, "decision", cases=[{"when": cond, "goto": sid(target)}
                                           for cond, target in cases], default=sid(default))

        def foreach(name, collection, binding, body, target):
            step(name, "foreach", collection=collection, item_binding=binding,
                 failure_policy="halt", max_iterations=32, body_entry=sid(body),
                 transitions={"completed": sid(target), "failed": sid("failed"),
                              "runtime_error": sid("failed")})

        identity = {"project_id": lit("matter-engine-cpp"), "object_id": item("object_id")}
        artifact = {"type": "context_ref", "path": "artifact_sha256"}
        command("read", "object_loop_inputs", {"project_id": lit("matter-engine-cpp"),
                                               "limit": lit(32)}, "starts", "inputs")
        foreach("starts", bound("inputs", "starts"), "start", "start-check", "loops")
        decision("start-check", [({"type": "bool", "op": "and", "operands": [
            truth(item("proposal_approved", "start")),
            compare(item("policy_artifact", "start"), "eq", artifact),
        ]}, "start")], "starts")
        command("start", "object_loop_start", {
            key: item("request." + key, "start") for key in ObjectLoopStartArgs.model_fields
        }, "start-reconcile", "started")
        command("start-reconcile", "object_loop_reconcile", {
            "project_id": lit("matter-engine-cpp"), "object_id": bound("started", "object_id"),
        }, "starts")
        foreach("loops", bound("inputs", "loops"), "object", "policy", "done")
        decision("policy", [(compare(item("policy_artifact"), "eq", artifact), "choose")],
                 "loops")
        decision("choose", [
            (compare(item("state.status"), "eq", lit("stopped")), "reconcile"),
            (compare(item("elapsed_seconds"), "gte", lit(86400)), "deadline"),
            (truth(item("scorer_failed")), "scorer-stop"),
            (truth(item("state.checkpoint")), "checkpoint"),
            (truth(item("score")), "score"),
        ], "reconcile")
        command("reconcile", "object_loop_reconcile", identity, "loops")

        def stop(name, reason, version=None, *, identity=identity):
            command(name, "object_loop_reconcile", {
                **identity, "expected_version": version or item("version"),
                "stop_reason": lit(reason),
            }, "loops")

        stop("deadline", "object wall deadline reached")
        stop("checkpoint-stop", "checkpoint not accepted; evidence retained")
        stop("adopt-stop", "checkpoint continuation refused by limits",
             bound("checkpoint", "version"))
        command("scorer-stop", "object_score_record", {
            **identity, "expected_version": item("version"),
            "score_task_id": item("state.score_task_id"), "receipts": lit([]),
            "action": lit("stop"),
            "stop_reason": lit("scorer exhausted; retained incumbent; quality unavailable"),
        }, "reconcile")
        command("checkpoint", "object_checkpoint_read", identity, "checkpoint-choice", "checkpoint")
        decision("checkpoint-choice", [
            (compare(item("review_state"), "in",
                     lit(["rejected", "withdrawn", "changes_requested"])), "checkpoint-stop"),
            ({"type": "bool", "op": "and", "operands": [
                truth(bound("checkpoint", "approved")), truth(item("next_variants")),
            ]}, "adopt"),
        ], "loops")
        command("adopt", "object_loop_reconcile", {
            **identity, "expected_version": bound("checkpoint", "version"),
            "next_variants": item("next_variants"),
        }, "loops", rejected="adopt-stop")
        score_inputs = {key: item("score." + key) for key in ObjectScoreRecordArgs.model_fields}
        command("score", "object_score_record", score_inputs, "reconcile", rejected="score-refused")
        decision("score-refused", [
            (compare(item("score.action"), "eq", lit("continue")), "score-stop"),
        ], "failed")
        command("score-stop", "object_score_record", {
            **{key: value for key, value in score_inputs.items()
               if key not in {"review_id", "review_revision", "review_sha256"}},
            "action": lit("stop"), "next_variants": lit([]),
            "stop_reason": lit(
                "continuation refused by score or budget contract; retained verified result"
            ),
        }, "reconcile")
        for name, outcome in (("done", "completed"), ("failed", "failed")):
            steps[sid(name)] = _terminal(rule, outcome, terminal_ref)
    return {"rules": rules, "steps": steps}


def _github_issue_triage_body(source: PlaybookSource) -> dict[str, Any]:
    """Two one-command rules, one daily and one approved-review response."""
    index = ProseIndex(source, source.vault_path)
    rules: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}

    def lit(value: Any) -> dict[str, Any]:
        return {"type": "literal", "value": value}

    def event(path: str) -> dict[str, Any]:
        return {"type": "event_ref", "path": path}

    def add(rule: str, trigger: dict[str, Any], command: str,
            inputs: dict[str, Any], successes: tuple[str, ...]) -> None:
        entry = f"{rule}--command"
        done = f"{rule}--done"
        failed = f"{rule}--failed"
        rules.append({"id": rule, "name": rule, "trigger": trigger,
                      "entry_step": entry, "source": index.rule_ref(rule)})
        transitions = {outcome: done for outcome in successes}
        transitions.update({"rejected": failed, "runtime_error": failed})
        steps[entry] = {
            "type": "command", "rule": rule, "title": command,
            "source": index.step_ref(rule, 1), "command": command,
            "inputs": inputs, "transitions": transitions,
        }
        steps[done] = _terminal(rule, "completed", index.step_ref(rule, None))
        steps[failed] = _terminal(rule, "failed", index.step_ref(rule, None))

    add("investigate-nightly", {"event_type": "cron.02:00"},
        "github_issue_triage", {"project_id": lit("agent-queue")}, ("swept",))
    add("file-approved-fix", {"event_type": "review.decided",
                              "filter": {"decision": "approve"}},
        "github_issue_fix_approved",
        {"project_id": event("project_id"), "review_id": event("review_id"),
         "revision": event("revision")},
        ("created", "reused", "ignored"))
    add("close-explicit-rejection", {"event_type": "review.decided",
                                     "filter": {"decision": "reject"}},
        "github_issue_rejection",
        {"project_id": event("project_id"), "review_id": event("review_id"),
         "revision": event("revision")},
        ("closed", "ignored"))
    return {"rules": rules, "steps": steps}


def _promotion_policy_body(source):
    from src.commands.contracts import CONTRACTS

    continuous = source.frontmatter["id"] == "promotion-continuous"
    rules, steps = [], {}
    def event(path):
        return {"type": "event_ref", "path": path}
    def lit(value):
        return {"type": "literal", "value": value}
    def bound(path):
        return {"type": "binding_ref", "binding": "input", "path": "policy." + path}
    def truth(path):
        return {"type": "exists", "value": bound(path), "mode": "truthy"}
    for rule, trigger in (("advance-source", "promotion.source_settled"),
                          ("explicit-request", "promotion.request_due"),
                          ("hotfix-completed", "promotion.hotfix_completed"),
                          ("visit-intent", "promotion.intent_due"),
                          ("backmerge-delivered", "promotion.delivered")):
        ref = _source_ref_for_heading(source, "## Rule: " + rule)
        def sid(name):
            return rule + "--" + name
        def step(name, kind, **fields):
            steps[sid(name)] = {"type": kind, "rule": rule, "title": name,
                                "source": ref, **fields}
        def cmd(name, command, inputs, success, *, save=None, outcomes=None):
            contract = CONTRACTS.get(command).contract.execution
            transitions = {outcome.name: sid("notify") for outcome in contract.outcomes}
            transitions.update({outcome.name: sid(success) for outcome in contract.outcomes
                                if outcome.classification.value == "success"})
            transitions.update({"runtime_error": sid("failed")})
            transitions.update({key: sid(value) for key, value in (outcomes or {}).items()})
            step(name, "command", command=command, inputs=inputs, transitions=transitions,
                 **({"save_result_as": save} if save else {}))
        for name, outcome in (("done", "completed"), ("failed", "failed")):
            steps[sid(name)] = _terminal(rule, outcome, _source_ref_for_heading(source, "## Failure handling"))
        notify_inputs = {"project_id": event("project_id"), "to_kind": lit("user"),
                         "to_id": lit("user"), "from_id": lit(source.frontmatter["id"]),
                         "from_kind": lit("system"),
                         "body": lit("Promotion needs operator attention. Inspect aq promote status; use prepare, approve or cancel after resolving the named hold.")}
        cmd("notify", "message_send", notify_inputs, "done")
        # Refused notification must end; it never loops back into itself.
        steps[sid("notify")]["transitions"] = {key: (sid("done") if value == sid("done") else sid("failed"))
                                                for key, value in steps[sid("notify")]["transitions"].items()}
        entry = "read"
        if rule == "visit-intent":
            entry = "publish"
            cmd("publish", "integration_promotion_publish", {"batch_id": event("batch_id")}, "done",
                outcomes={"held": "notify", "unknown": "notify"})
        elif rule == "backmerge-delivered":
            entry = "backmerge"
            cmd("backmerge", "integration_backmerge_source",
                {"project_id": event("project_id"), "step_id": event("step_id")}, "done")
        elif rule == "advance-source" and not continuous:
            entry = "done"
        else:
            identity = {"project_id": event("project_id"), "step_id": event("step_id")}
            if rule == "hotfix-completed":
                identity["from_task"] = event("from_task")
            cmd("read", "integration_promotion_policy_input", identity, "choose", save="input")
            cases = [{"when": truth("backmerge_pending"), "goto": sid("notify")}]
            if continuous and rule == "advance-source":
                cases.append({"when": truth("newer_source"), "goto": sid("cancel")})
                cmd("cancel", "promote_cancel", {"project_id": event("project_id"),
                    "request_id": bound("active_request_id")}, "request")
            cases.append({"when": truth("active_request_id"), "goto": sid("done")})
            step("choose", "decision", cases=cases, default=sid("request"))
            inputs = {**identity, "source_sha": bound("source_sha")}
            if rule != "advance-source":
                inputs["notes_reviewed"] = event("notes_reviewed")
            cmd("request", "promote_request", inputs, "done")
        if rule == "advance-source" and not continuous:
            steps.pop(sid("notify"))
            steps.pop(sid("failed"))
        rules.append({"id": rule, "name": rule, "source": ref,
                      "trigger": {"event_type": trigger}, "entry_step": sid(entry)})
    return {"rules": rules, "steps": steps}


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

    rule = "continue-closed-root-repair"
    done, failed = terminals(rule)
    current, build, ci, dispatch = (
        f"{rule}--current", f"{rule}--build", f"{rule}--ci", f"{rule}--dispatch"
    )
    rules.append({"id": rule, "name": rule,
                  "trigger": {"event_type": "integration.repair_delegate_closed"},
                  "entry_step": current, "source": ref(rule)})
    steps[current] = {"type": "command", "rule": rule, "title": "current",
                      "source": ref(rule), "command": "integration_repair_close_current",
                      "save_result_as": "closed_repair",
                      "inputs": {name: event(name) for name in (
                          "operation_id", "stage", "task_id", "session_id",
                          "instance_token", "workspace_id", "fence_token")},
                      "transitions": {"current": build, "not_batch": done,
                                      "stale": failed, "runtime_error": failed}}
    steps[build] = {"type": "command", "rule": rule, "title": "build",
                    "source": ref(rule), "command": "integration_build_candidate",
                    "save_result_as": "repaired_candidate",
                    "inputs": {"batch_id": bound("closed_repair", "batch_id"),
                               "expected_revision": bound("closed_repair", "revision")},
                    "transitions": {"built": ci, "already_built": ci, "empty": done,
                                    "conflict": dispatch,
                                    **{name: failed for name in (
                                        "source_moved", "base_moved", "stale_revision",
                                        "wait", "human_required", "configuration_blocked",
                                        "runtime_error")}}}
    steps[ci] = {"type": "command", "rule": rule, "title": "ci",
                 "source": ref(rule), "command": "integration_ci_evidence",
                 "inputs": {"batch_id": bound("closed_repair", "batch_id"),
                            "revision": bound("repaired_candidate", "revision")},
                 "transitions": {"green": done, "red": done, "pending": done,
                                 **{name: failed for name in (
                                     "full_suite_required", "stale_subject",
                                     "configuration_blocked", "runtime_error")}}}
    steps[dispatch] = {"type": "command", "rule": rule, "title": "dispatch-conflict",
                       "source": ref(rule), "command": "integration_repair_dispatch",
                       "inputs": {"operation_id": event("operation_id")},
                       "transitions": {name: (
                           done if name in {"dispatched", "already_dispatched", "writer_reused"}
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
                        "operation_id": event("operation_id")},
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


#: The heading of the routing playbook's policy section (mandatory-routing
#: §6.3); its fenced ``yaml`` block is the policy every plan step carries.
ROUTING_POLICY_HEADING = "## Routing policy"


def routing_policy_block(source_text: str) -> str:
    """The text of the fenced ``yaml`` block under ``## Routing policy``, verbatim.

    Every line between the opening and the closing fence, each ending in a
    newline.  A missing section or block, or a second block in the section,
    is a refusal rather than a guess: the block *is* the executed policy.
    """
    lines = source_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == ROUTING_POLICY_HEADING)
    except StopIteration:
        raise SystemExit(f"routing source has no {ROUTING_POLICY_HEADING!r} section") from None
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines[start + 1 : end]:
        if current is None and line.strip() == "```yaml":
            current = []
        elif current is not None and line.strip() == "```":
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    if current is not None or len(blocks) != 1:
        raise SystemExit(
            f"{ROUTING_POLICY_HEADING!r} must hold exactly one closed ```yaml block"
        )
    return "".join(f"{line}\n" for line in blocks[0])


#: The classifier's risk answer, in ascending order
#: (:data:`src.routing.policy.RISK_LEVELS`).
_RISK_LEVELS = ["low", "medium", "high", "very_high"]


def _classification_schema(policy_text: str) -> dict[str, Any]:
    """The classify step's ``output_schema`` for the policy block *policy_text*.

    A policy with a top-level ``risk`` table asks the classifier for ``risk``
    and ``risk_reason`` as well; one without it keeps the six fields, so a
    policy that does not use risk compiles to the same artifact as before.
    """
    properties: dict[str, Any] = {
        "task_type": {"type": "string"},
        "intelligence_class": {"type": "string"},
        "narrow": {"type": "boolean"},
        "test_verified": {"type": "boolean"},
        "independent_verifier": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
    }
    required = [
        "task_type", "intelligence_class", "narrow",
        "test_verified", "independent_verifier", "reason",
    ]
    parsed = yaml.safe_load(policy_text)
    if isinstance(parsed, dict) and parsed.get("risk"):
        properties["risk"] = {"type": "string", "enum": list(_RISK_LEVELS)}
        properties["risk_reason"] = {"type": "string", "minLength": 1, "maxLength": 400}
        required += ["risk", "risk_reason"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _default_assignment_routing_body(source: PlaybookSource) -> dict[str, Any]:
    """The routing semantic body for ``default-assignment-routing``.

    Mandatory-routing §6.7: one rule on ``task.route_needed``.  Plan with
    ``task_route_plan`` over the policy block, classify with the LLM only
    when the plan asks for it, and write the plan with ``task_route_apply``.
    V2 binds a name once and a transition names only a step, so the three
    plans carry distinct bindings (``plan_a``, ``plan_b``, ``plan_c``) and
    each has its own apply step.  Every ``task_route_plan`` step passes the
    ``## Routing policy`` block's text, verbatim, as the scalar literal
    ``policy``: a ``LiteralValue`` holds scalars and flat containers only, so
    a string is the faithful carrier, and the reviewed prose and the executed
    policy cannot drift.  Every step carries the numbered prose line that
    authorises it.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "route-task"
    plan_first = f"{rule}--plan_first"
    classify = f"{rule}--classify"
    plan_classified = f"{rule}--plan_classified"
    plan_unclassified = f"{rule}--plan_unclassified"
    done = f"{rule}--done"
    failed = f"{rule}--failed"
    task_id = {"type": "event_ref", "path": "task_id"}
    policy = {"type": "literal", "value": routing_policy_block(source.raw)}
    router_id = str(source.frontmatter["id"])

    def bound(binding: str, path: str | None = None) -> dict[str, Any]:
        ref: dict[str, Any] = {"type": "binding_ref", "binding": binding}
        if path is not None:
            ref["path"] = path
        return ref

    def plan_step(title: str, ordinal: int, binding: str, apply_step: str,
                  classification: dict[str, Any] | None,
                  extra: dict[str, str]) -> dict[str, Any]:
        inputs: dict[str, Any] = {"task_id": task_id, "policy": policy}
        if classification is not None:
            inputs["classification"] = classification
        transitions = {
            "planned": apply_step,
            "held": done,
            "already_routed": done,
            "needs_classification": failed,
            "no_candidates": failed,
            "rejected": failed,
            "runtime_error": failed,
        }
        transitions.update(extra)
        return {
            "type": "command",
            "rule": rule,
            "title": title,
            "source": index.step_ref(rule, ordinal),
            "command": "task_route_plan",
            "inputs": inputs,
            "save_result_as": binding,
            "transitions": transitions,
        }

    def apply_step(title: str, binding: str) -> dict[str, Any]:
        return {
            "type": "command",
            "rule": rule,
            "title": title,
            "source": index.step_ref(rule, 5),
            "command": "task_route_apply",
            "inputs": {"task_id": task_id, "plan": bound(binding)},
            "transitions": {
                "routed": done,
                "stale": done,
                "rejected": failed,
                "runtime_error": failed,
            },
        }

    def status_in(path: str, values: list[str]) -> dict[str, Any]:
        return {
            "type": "comparison",
            "op": "in",
            "left": {"type": "event_ref", "path": path},
            "right": {"type": "literal", "value": values},
        }

    return {
        "rules": [
            {
                "id": rule,
                "name": rule,
                "trigger": {"event_type": "task.route_needed"},
                # Route-needed is deliberately re-emitted while a task owes a
                # route, so its event id is not a durable task identity.
                # Admit only a currently eligible hydrated row that the
                # router still owes a route, and only for a project bound to
                # this playbook, so a queued retry cannot reach the planner
                # after another run routed or finished the task.
                "guard": {
                    "type": "bool",
                    "op": "and",
                    "operands": [
                        status_in("task.status", ["DEFINED", "READY", "BLOCKED"]),
                        status_in("task.route_source", ["unrouted", "legacy"]),
                        {
                            "type": "comparison",
                            "op": "eq",
                            "left": {"type": "event_ref", "path": "router"},
                            "right": {"type": "literal", "value": router_id},
                        },
                    ],
                },
                "entry_step": plan_first,
                "source": index.rule_ref(rule),
            }
        ],
        "steps": {
            plan_first: plan_step(
                "plan_first", 1, "plan_a", f"{rule}--apply_a", None,
                {"needs_classification": classify},
            ),
            classify: {
                "type": "llm",
                "rule": rule,
                "title": "classify",
                "source": index.step_ref(rule, 2),
                "profile_id": "playbook-compiler",
                "prompt": {
                    "type": "literal",
                    "value": _section(source, "## Classifying a task", "## "),
                },
                # §6.5: the classifier sees the task and the policy's
                # vocabulary, never a profile, a provider or a load.
                "inputs": {
                    name: bound("plan_a", name)
                    for name in (
                        "title", "description", "task_type", "class_hint",
                        "questions", "allowed_kinds", "allowed_classes",
                    )
                },
                "output_schema": _classification_schema(policy["value"]),
                "budget": {
                    "max_calls": 1,
                    "max_output_tokens": 4096,
                    "max_total_tokens": 4096,
                    "timeout_seconds": 300,
                },
                "tool_use": {"enabled": False, "aq_commands": [], "plugin_tools": []},
                "save_result_as": "classification",
                # A failed classification still routes, on the policy's
                # defaults (§6.4 step 4).
                "transitions": _llm_transitions(plan_classified, plan_unclassified),
            },
            plan_classified: plan_step(
                "plan_classified", 3, "plan_b", f"{rule}--apply_b",
                bound("classification"), {},
            ),
            plan_unclassified: plan_step(
                "plan_unclassified", 4, "plan_c", f"{rule}--apply_c",
                {"type": "literal", "value": {"failed": True}}, {},
            ),
            f"{rule}--apply_a": apply_step("apply_a", "plan_a"),
            f"{rule}--apply_b": apply_step("apply_b", "plan_b"),
            f"{rule}--apply_c": apply_step("apply_c", "plan_c"),
            done: _terminal(rule, "completed", index.step_ref(rule, None)),
            failed: _terminal(rule, "failed", index.step_ref(rule, None)),
        },
    }


def _ci_main_sentinel_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``ci-main-sentinel``.

    Four command steps and two terminals, lowered from the numbered prose
    items so every step carries the line that authorises it.  See
    ``docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md`` and
    ``docs/superpowers/specs/2026-09-16-ci-sentinel-repair-coverage-design.md``.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "keep-main-green"
    observe = f"{rule}--read_baseline"
    repair = f"{rule}--ensure_repair_task"
    record = f"{rule}--record_repair"
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
                    "created": record,
                    "reused": record,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            record: {
                "type": "command",
                "rule": rule,
                "title": "record_repair",
                "source": index.step_ref(rule, 3),
                "command": "ci_repair_adopt",
                "inputs": {
                    "project_id": project,
                    "task_id": {"type": "binding_ref", "binding": "repair", "path": "task_id"},
                    "ref": bound("ref"),
                    "head_sha": bound("head_sha"),
                    "failing_tests": bound("repair_tests"),
                    "failing_checks": bound("repair_checks"),
                },
                "transitions": {
                    "adopted": done,
                    "recorded": done,
                    "unchanged": done,
                    "rejected": failed,
                    "runtime_error": failed,
                },
            },
            escalate: {
                "type": "command",
                "rule": rule,
                "title": "escalate_to_human",
                "source": index.step_ref(rule, 4),
                "command": "escalation_create",
                "inputs": {
                    "project_id": project,
                    "source_kind": {"type": "literal", "value": "core"},
                    "source_identity": bound("escalation_key"),
                    "incident_key": bound("escalation_key"),
                    "summary": bound("escalation_title"),
                    "investigation": bound("escalation_question"),
                    "decision_requested": {
                        "type": "literal",
                        "value": "Choose the next bounded repair or accept the red baseline.",
                    },
                    "choices": {
                        "type": "literal",
                        "value": ["Fix by hand", "Retarget the repair", "Accept the red baseline"],
                    },
                    "severity": {"type": "literal", "value": "high"},
                },
                "transitions": {
                    "created": done,
                    "reused": done,
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


def _supervisor_hourly_report_body(source: PlaybookSource) -> dict[str, Any]:
    index = ProseIndex(source, source.vault_path)
    rules, steps = [], {}
    for rule, event_type in (("request-window", "digest.window_ready"),
                             ("recover-window", "timer.1m")):
        call, done, failed = (f"{rule}--{suffix}" for suffix in ("request", "done", "failed"))
        rules.append({
            "id": rule, "name": rule, "trigger": {"event_type": event_type},
            "entry_step": call, "source": index.rule_ref(rule),
        })
        steps[call] = {
            "type": "command", "rule": rule, "title": "request",
            "source": index.step_ref(rule, 1), "command": "report_reconcile",
            "inputs": {}, "save_result_as": "requests",
            "transitions": {"completed": done, "rejected": failed, "runtime_error": failed},
        }
        steps[done] = _terminal(rule, "completed", index.step_ref(rule, None))
        steps[failed] = _terminal(rule, "failed", index.step_ref(rule, None))
    return {"rules": rules, "steps": steps}


def _supervisor_failure_triage_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewed strict-hold companion for every durable task failure.

    Policy remains in the playbook prose. The one command obtains an
    idempotent incident receipt; the query layer decides whether a terminal
    failure is actionable, so a status filter cannot hide service failures.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "triage-failed-task"
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
                "trigger": {"event_type": "task.failed"},
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
                "command": "task_failure_triage_notify",
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


def _morning_report_body(source: PlaybookSource) -> dict[str, Any]:
    """One optional minute reconciliation command, with no model or worker step."""
    index = ProseIndex(source, source.vault_path)
    rule = "reconcile-morning"
    tick, done, failed = (f"{rule}--{suffix}" for suffix in ("tick", "done", "failed"))
    return {
        "rules": [{
            "id": rule, "name": rule, "trigger": {"event_type": "timer.1m"},
            "entry_step": tick, "source": index.rule_ref(rule),
        }],
        "steps": {
            tick: {
                "type": "command", "rule": rule, "title": "tick",
                "source": index.step_ref(rule, 1), "command": "morning_report_tick",
                "inputs": {}, "save_result_as": "report",
                "transitions": {"completed": done, "rejected": failed, "runtime_error": failed},
            },
            done: _terminal(rule, "completed", index.step_ref(rule, None)),
            failed: _terminal(rule, "failed", index.step_ref(rule, None)),
        },
    }


def _supervisor_digest_body(source: PlaybookSource) -> dict[str, Any]:
    """One optional minute reconciliation command, with no model or worker step.

    Phase P3 of *Discord as a chat extension of the supervisor* (2026-10-03 §4).
    The supervisor writes the digest in its own session, so this policy has no
    author step of its own: it only decides when held windows become author turns,
    and the command owns the rest.
    """
    index = ProseIndex(source, source.vault_path)
    rule = "reconcile-digest"
    request, done, failed = (f"{rule}--{suffix}" for suffix in ("request", "done", "failed"))
    return {
        "rules": [{
            "id": rule, "name": rule, "trigger": {"event_type": "timer.1m"},
            "entry_step": request, "source": index.rule_ref(rule),
        }],
        "steps": {
            request: {
                "type": "command", "rule": rule, "title": "request",
                "source": index.step_ref(rule, 1), "command": "digest_request",
                "inputs": {}, "save_result_as": "windows",
                "transitions": {"completed": done, "rejected": failed, "runtime_error": failed},
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


def _provider_failover_body(source: PlaybookSource) -> dict[str, Any]:
    """The reviewer-authored deterministic graph for ``provider-failover``.

    Two rules, command steps only, no LLM (provider-failover D11).  On
    ``provider.state_changed``: one ``provider_reroute`` sweep, then the
    idempotent ``provider_availability_notify`` for the event's provider and
    generation.  On ``timer.5m``: one sweep, which is what trickles the rest
    of a dead provider's queue across (D15).  See
    ``docs/specs/provider-failover.md``.
    """
    index = ProseIndex(source, source.vault_path)
    change = "reroute-on-change"
    tick = "reroute-sweep"
    sweep_outcomes = ("rerouted", "held", "idle", "disabled")
    notify_outcomes = (
        "notified",
        "flapping",
        "already_notified",
        "flap_damped",
        "not_a_half_change",
        "disabled",
        "messages_disabled",
    )

    def event(path: str) -> dict[str, Any]:
        return {"type": "event_ref", "path": path}

    def transitions(outcomes: tuple[str, ...], ok: str, failed: str) -> dict[str, str]:
        mapped = {name: ok for name in outcomes}
        mapped["rejected"] = failed
        mapped["runtime_error"] = failed
        return mapped

    change_sweep, change_notify = f"{change}--reroute", f"{change}--notify"
    change_done, change_failed = f"{change}--done", f"{change}--failed"
    tick_sweep = f"{tick}--reroute"
    tick_done, tick_failed = f"{tick}--done", f"{tick}--failed"
    return {
        "rules": [
            {
                "id": change,
                "name": change,
                "trigger": {"event_type": "provider.state_changed"},
                "entry_step": change_sweep,
                "source": index.rule_ref(change),
            },
            {
                "id": tick,
                "name": tick,
                "trigger": {"event_type": "timer.5m"},
                "entry_step": tick_sweep,
                "source": index.rule_ref(tick),
            },
        ],
        "steps": {
            change_sweep: {
                "type": "command",
                "rule": change,
                "title": "reroute",
                "source": index.step_ref(change, 1),
                "command": "provider_reroute",
                "inputs": {},
                "save_result_as": "sweep",
                "transitions": transitions(sweep_outcomes, change_notify, change_failed),
            },
            change_notify: {
                "type": "command",
                "rule": change,
                "title": "notify",
                "source": index.step_ref(change, 2),
                "command": "provider_availability_notify",
                "inputs": {
                    "provider": event("provider"),
                    "generation": event("generation"),
                },
                "save_result_as": "notice",
                "transitions": transitions(notify_outcomes, change_done, change_failed),
            },
            change_done: _terminal(change, "completed", index.step_ref(change, None)),
            change_failed: _terminal(change, "failed", index.step_ref(change, None)),
            tick_sweep: {
                "type": "command",
                "rule": tick,
                "title": "reroute",
                "source": index.step_ref(tick, 1),
                "command": "provider_reroute",
                "inputs": {},
                "save_result_as": "sweep",
                "transitions": transitions(sweep_outcomes, tick_done, tick_failed),
            },
            tick_done: _terminal(tick, "completed", index.step_ref(tick, None)),
            tick_failed: _terminal(tick, "failed", index.step_ref(tick, None)),
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


def build(playbook_id: str, rel_path: str | None = None) -> dict[str, Any]:
    """Compile one shipped source and report what a reviewer would see.

    *rel_path* compiles a draft of the shipped playbook in place of its
    shipped source, with the same semantic body.
    """
    rel_path = rel_path or SOURCES[playbook_id]
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
    parser.add_argument(
        "--source",
        help=(
            "compile this draft source of one shipped id instead of its shipped "
            "source; needs --out and exactly one id"
        ),
    )
    parser.add_argument(
        "--out",
        help=(
            "write the bundle here instead of the fixture tree; the manifest is "
            "seeded from the fixture's on first write"
        ),
    )
    parser.add_argument("ids", nargs="*", default=None)
    args = parser.parse_args()
    if args.source and (not args.out or len(args.ids or []) != 1):
        parser.error("--source needs --out and exactly one playbook id")

    drift = 0
    for playbook_id in args.ids or list(SOURCES):
        print(f"{playbook_id}:")
        result = build(playbook_id, args.source)
        directory = Path(args.out).resolve() if args.out else FIXTURE_ROOT / playbook_id
        directory.mkdir(parents=True, exist_ok=True)
        seed_manifest = FIXTURE_ROOT / playbook_id / "manifest.md"
        if args.out and not (directory / "manifest.md").exists() and seed_manifest.exists():
            (directory / "manifest.md").write_bytes(seed_manifest.read_bytes())
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
            manifest_path = directory / "manifest.md"
            if manifest_path.exists():
                manifest = manifest_path.read_text(encoding="utf-8")
                for key, value in {
                    "artifact_sha256": result["artifact_sha256"],
                    "source_sha256": artifact.source_hash,
                    "contract_fingerprint": artifact.contract_fingerprint(),
                }.items():
                    manifest, count = re.subn(
                        rf"^{key}: .+$", f"{key}: {value}", manifest, count=1, flags=re.M
                    )
                    if count != 1:
                        raise SystemExit(f"{manifest_path}: missing {key}")
                files[manifest_path] = manifest.encode("utf-8")
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
            if args.check and path.name == "manifest.md" and existing is not None:
                # The artifact hash covers compiled_at; the deterministic artifact
                # and the manifest's recorded hash are checked independently.
                stable = lambda data: re.sub(  # noqa: E731
                    r"^artifact_sha256: .+$", "", data.decode("utf-8"), flags=re.M
                )
                if stable(existing) == stable(payload):
                    continue
            drift += 1
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
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
