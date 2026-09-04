#!/usr/bin/env python3
"""Rebuild the reviewed V2 artifact fixtures for the shipped playbooks.

**Nothing in CI, the daemon, or the release check runs this.**  It is the
recording aid for the human procedure in Package 6's child plan §5.3 (T-8):
a person runs it, reads the semantic diff, resolves every compiler question,
and only then checks the output in beside a hand-written ``review.md``.  The
fixtures are the approved recording; the suites validate them and never
regenerate them (child plan §5.3, "Determinism note").

Two of the four shipped playbooks are mechanically lowered from V1 sources
here, without an LLM:

* ``default-pipeline`` — lowered from the **frozen** V1 graph at
  ``tests/fixtures/playbooks/v1/default-pipeline.md``, which is what makes the
  reviewed artifact behaviourally identical to its V1 predecessor by
  construction rather than by assertion.  Its source references are then
  remapped onto the rewritten prose, because a reference into a file that no
  longer contains a graph would point a reviewer at nothing.
* ``default-assignment-routing`` — lowered from its own live source, which has
  no graph to remove; ``lower_assignment`` derives the single AI node from the
  frontmatter and prose.

The two LLM playbooks (``memory-consolidation``, ``coding-reflection``) have no
V1 machine graph at all. Their deterministic, reviewer-authored semantic bodies
preserve the prose as the LLM prompt and add only the typed V2 envelope:
triggers, profiles, budgets, tool ceilings, output schemas, and terminal
transitions.

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
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.playbooks.authoring import PlaybookSource  # noqa: E402
from src.playbooks.definition import canonical_bytes  # noqa: E402
from src.playbooks.migration import shipped_profile_lookup  # noqa: E402
from src.playbooks.pipeline_lowering import lower_assignment, lower_pipeline  # noqa: E402
from src.playbooks.proposal import propose  # noqa: E402
from src.playbooks.validation import (  # noqa: E402
    RegisteredEventLookup,
    RegistryContractLookup,
    VaultProfileLookup,
)
from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2"
FROZEN_V1 = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v1"

SHIPPED = {
    "default-pipeline": "src/prompts/default_playbooks/default-pipeline.md",
    "default-assignment-routing": "src/prompts/default_playbooks/default-assignment-routing.md",
    "memory-consolidation": "src/prompts/default_playbooks/memory-consolidation.md",
    "coding-reflection": "src/prompts/default_agent_type_playbooks/claude-opus/reflection.md",
}

# Project playbooks stay in their live V1 vault path until an operator closes
# V1 admission.  Their reviewed V2 candidates are staged beside the other
# fixture evidence rather than copied over the running source.
STAGED = {
    "pr-merge-sweep": "tests/fixtures/playbooks/v2/pr-merge-sweep/source.md",
}
SOURCES = {**SHIPPED, **STAGED}

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


def profile_lookup(playbook_id: str | None = None) -> Any:
    """Resolve profiles from ``src/profiles/defaults/``, the shipped set.

    Production resolves profiles from the database (``_v2_lookups``); a fixture
    must not depend on one operator's install, so the reviewed artifact is held
    to the profiles this repository ships.  The construction lives in
    ``src.playbooks.migration`` so the release check that later *holds* the
    fixture to those profiles resolves them the same way this build did.
    """
    if playbook_id != "pr-merge-sweep":
        return shipped_profile_lookup()
    profile_path = FIXTURE_ROOT / "pr-merge-sweep" / "pr-merger-profile.md"
    parsed = parse_profile(profile_path.read_text(encoding="utf-8"))
    if not parsed.is_valid:
        raise ValueError(f"staged profile {profile_path} does not parse: {parsed.errors}")
    fields = parsed_profile_to_agent_profile(parsed)
    return VaultProfileLookup({fields["id"]: SimpleNamespace(**fields)})


def _load(rel_path: str) -> PlaybookSource:
    path = REPO_ROOT / rel_path
    loaded = PlaybookSource.load(path, vault_root=path.parent)
    if not isinstance(loaded, PlaybookSource):
        raise SystemExit(f"{rel_path}: {loaded.errors}")
    return loaded


def semantic_body(playbook_id: str, source: PlaybookSource) -> dict[str, Any]:
    if playbook_id == "default-pipeline":
        frozen = FROZEN_V1 / "default-pipeline.md"
        loaded = PlaybookSource.load(frozen, vault_root=frozen.parent)
        assert isinstance(loaded, PlaybookSource)
        body, diagnostics = lower_pipeline(loaded, contracts=RegistryContractLookup())
        if diagnostics:
            raise SystemExit(f"frozen V1 graph no longer lowers cleanly: {diagnostics}")
        return _remap_pipeline_refs(json.loads(json.dumps(body)), ProseIndex(source, source.vault_path))
    if playbook_id == "default-assignment-routing":
        body, diagnostics = lower_assignment(source)
        if diagnostics:
            raise SystemExit(f"{playbook_id}: {diagnostics}")
        return json.loads(json.dumps(body))
    if playbook_id == "pr-merge-sweep":
        frozen = FIXTURE_ROOT / "pr-merge-sweep" / "legacy-v1.md"
        loaded = PlaybookSource.load(frozen, vault_root=frozen.parent)
        assert isinstance(loaded, PlaybookSource)
        body, diagnostics = lower_pipeline(loaded, contracts=RegistryContractLookup())
        if diagnostics:
            raise SystemExit(f"frozen V1 graph no longer lowers cleanly: {diagnostics}")
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
    if playbook_id == "coding-reflection":
        return _coding_reflection_body(source)
    return {}


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


def _coding_reflection_body(source: PlaybookSource) -> dict[str, Any]:
    source_ref = _source_ref_for_heading(source, "# Coding Agent Reflection")
    terminal_ref = _source_ref_for_heading(source, "## Skip conditions")
    steps: dict[str, Any] = {}
    rules: list[dict[str, Any]] = []
    for suffix, event_type in (("completed", "task.completed"), ("failed", "task.failed")):
        rule = f"reflect-{suffix}"
        run = f"{rule}--run"
        done = f"{rule}--done"
        failed = f"{rule}--failed"
        rules.append(
            {
                "id": rule,
                "name": f"Reflect on {suffix} coding tasks",
                "trigger": {"event_type": event_type},
                "entry_step": run,
                "source": source_ref,
            }
        )
        steps[run] = {
            "type": "llm",
            "rule": rule,
            "title": "Extract and save reusable coding insights",
            "source": source_ref,
            "profile_id": "worker-deep-high-claude",
            "prompt": {"type": "literal", "value": source.body.strip()},
            "inputs": {
                "task_id": {"type": "event_ref", "path": "task_id"},
                "project_id": {"type": "event_ref", "path": "project_id"},
                "title": {"type": "event_ref", "path": "title"},
                "task_outcome": {"type": "literal", "value": suffix},
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "insights_saved": {"type": "integer", "minimum": 0},
                    "skipped": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                "required": ["insights_saved", "skipped", "summary"],
                "additionalProperties": False,
            },
            "budget": {
                "max_calls": 20,
                "max_output_tokens": 4096,
                "max_total_tokens": 32768,
                "timeout_seconds": 600,
            },
            "tool_use": {
                "enabled": True,
                "aq_commands": ["get_task"],
                "plugin_tools": ["git_diff", "memory_search", "memory_save"],
            },
            "transitions": _llm_transitions(done, failed),
        }
        steps[done] = _terminal(rule, "completed", terminal_ref)
        steps[failed] = _terminal(rule, "failed", terminal_ref)
    return {"rules": rules, "steps": steps}


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
                        "prose playbook with no V1 machine graph: a semantic body "
                        "requires a compiler-agent run a human reviews"
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
