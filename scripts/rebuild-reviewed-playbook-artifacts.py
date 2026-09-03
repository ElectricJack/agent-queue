#!/usr/bin/env python3
"""Rebuild the reviewed V2 artifact fixtures for the shipped playbooks.

**Nothing in CI, the daemon, or the release check runs this.**  It is the
recording aid for the human procedure in Package 6's child plan §5.3 (T-8):
a person runs it, reads the semantic diff, resolves every compiler question,
and only then checks the output in beside a hand-written ``review.md``.  The
fixtures are the approved recording; the suites validate them and never
regenerate them (child plan §5.3, "Determinism note").

Two of the four shipped playbooks compile deterministically here, because
their V1 predecessors carried machine graphs that
``src/playbooks/pipeline_lowering.py`` lowers without an LLM:

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
V1 machine graph at all.  Their bodies live in
``tests/fixtures/playbooks/v2/<id>/semantic-body.json`` as the reviewed compiler
proposal, exactly as a compiler agent would have emitted it; this script loads
and re-validates them rather than inventing them.

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

from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import canonical_bytes
from src.playbooks.pipeline_lowering import lower_assignment, lower_pipeline
from src.playbooks.proposal import propose
from src.playbooks.validation import (
    RegisteredEventLookup,
    RegistryContractLookup,
    VaultProfileLookup,
)

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2"
FROZEN_V1 = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v1"

SHIPPED = {
    "default-pipeline": "src/prompts/default_playbooks/default-pipeline.md",
    "default-assignment-routing": "src/prompts/default_playbooks/default-assignment-routing.md",
    "memory-consolidation": "src/prompts/default_playbooks/memory-consolidation.md",
    "coding-reflection": "src/prompts/default_agent_type_playbooks/claude-opus/reflection.md",
}

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


def shipped_profiles() -> dict[str, Any]:
    """Every profile under ``src/profiles/defaults/``, as capability-policy input."""
    from types import SimpleNamespace

    from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

    profiles: dict[str, Any] = {}
    for profile_md in sorted((REPO_ROOT / "src" / "profiles" / "defaults").glob("*/profile.md")):
        parsed = parse_profile(profile_md.read_text(encoding="utf-8"))
        if not parsed.is_valid:
            raise SystemExit(f"{profile_md}: {parsed.errors}")
        fields = parsed_profile_to_agent_profile(parsed)
        profiles[fields["id"]] = SimpleNamespace(**fields)
    return profiles


def profile_lookup() -> VaultProfileLookup:
    """Resolve profiles from ``src/profiles/defaults/``, the shipped set.

    Production resolves profiles from the database (``_v2_lookups``); a fixture
    must not depend on one operator's install, so the reviewed artifact is held
    to the profiles this repository ships.
    """
    return VaultProfileLookup(shipped_profiles())


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
    return {}


def build(playbook_id: str) -> dict[str, Any]:
    """Compile one shipped source and report what a reviewer would see."""
    rel_path = SHIPPED[playbook_id]
    source = _load(rel_path)
    body = semantic_body(playbook_id, source)
    if not body:
        # No V1 machine graph exists to lower, and this script does not run a
        # compiler agent.  Record the question rather than inventing a body.
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
        profiles=profile_lookup(),
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
    for playbook_id in args.ids or list(SHIPPED):
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
