"""Build the synthetic upper-bound artifact for the Package 7 performance corpus.

Child plan §11.2: "production-like graph sizes" is fixed as the largest enabled
artifact in the fleet plus a synthetic upper bound at **5x the default
pipeline's rule count and 10x its node count** — 25 rules / 170 nodes.
Measure 12's 300 ms graph-latency gate is asserted against this artifact, not
the shipped pipeline, so the gate cannot quietly weaken as the pipeline shrinks.

Run ``python tests/fixtures/playbooks/cutover/perf-corpus/generate.py`` to
rewrite ``synthetic-25x170.artifact.json``; the bytes are canonical, so a
regeneration with no generator change is a no-op.  The graph is deliberately
plain — chains of command steps ending in two terminals — because the gate is
about size, not about exotic step kinds.
"""

from __future__ import annotations

import pathlib
import sys

RULES = 25
NODES = 170
OUTPUT = pathlib.Path(__file__).with_name("synthetic-25x170.artifact.json")


def synthetic_payload() -> dict:
    """25 rules; 20 with five command steps and 5 with four, two terminals each."""
    rules = []
    steps: dict[str, dict] = {}
    source = {"path": "synthetic.md", "start_line": 1, "end_line": 1}
    for index in range(RULES):
        rule_id = f"rule-{index:02d}"
        chain = 5 if index < 20 else 4
        step_ids = [f"{rule_id}--step-{n}" for n in range(chain)]
        done, bad = f"{rule_id}--done", f"{rule_id}--bad"
        rules.append(
            {
                "id": rule_id,
                "name": f"Synthetic rule {index}",
                "trigger": {"event_type": f"synthetic.event-{index % 5}"},
                "entry_step": step_ids[0],
                "source": source,
            }
        )
        for position, step_id in enumerate(step_ids):
            following = step_ids[position + 1] if position + 1 < chain else done
            steps[step_id] = {
                "type": "command",
                "rule": rule_id,
                "title": f"Step {position}",
                "command": "ensure_task",
                "inputs": {},
                "save_result_as": f"result_{position}",
                "transitions": {"created": following, "reused": following, "rejected": bad},
                "source": source,
            }
        steps[done] = {
            "type": "terminal", "rule": rule_id, "title": "Done", "outcome": "completed",
            "source": source,
        }
        steps[bad] = {
            "type": "terminal", "rule": rule_id, "title": "Bad", "outcome": "failed",
            "source": source,
        }
    assert len(rules) == RULES and len(steps) == NODES, (len(rules), len(steps))
    return {
        "schema_version": 2,
        "id": "synthetic-25x170",
        "version": 1,
        "scope": {"type": "system"},
        "source_hash": "sha256:" + "7" * 64,
        "compiled_at": "2026-09-03T00:00:00Z",
        "purpose": "routine",
        "rules": rules,
        "steps": steps,
    }


def main() -> int:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[5]))
    from src.playbooks.definition import PlaybookDefinition, canonical_bytes

    definition = PlaybookDefinition.model_validate(synthetic_payload())
    OUTPUT.write_bytes(canonical_bytes(definition))
    print(f"wrote {OUTPUT} ({len(definition.rules)} rules, {len(definition.steps)} nodes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
