"""Dry-run the risk-aware routing draft against the shipped policy.

Run from the repository root::

    PYTHONPATH=. python docs/superpowers/specs/2026-10-09-risk-aware-routing/dry-run.py

Each worked example goes through :func:`src.routing.planner.plan_route` twice
per policy, the way the playbook drives it: first with no classification (to
show which questions the plan asks), then with the classifier answer recorded
below.  The fleet is one derived rung per class on ``claude`` and ``codex``,
the three local OpenCode rungs and the three free hosted OpenCode Zen rungs,
all idle, so the result shows the policy and nothing else.  Prints a Markdown
table; ``dry-run.md`` next to this file is its recorded output.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from src.routing.planner import ProfileFacts, ProviderFacts, Snapshot, TaskFacts, plan_route
from src.routing.policy import parse_policy

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SHIPPED = ROOT / "src/prompts/reviewed_playbooks/default-assignment-routing/source.md"
DRAFT = HERE / "default-assignment-routing.md"


def _policy(path: Path):
    text = path.read_text(encoding="utf-8")
    section = text.split("## Routing policy", 1)[1]
    block = re.search(r"```yaml\n(.*?)```", section, re.DOTALL).group(1)
    return parse_policy(block)


def _fleet(classes: frozenset[str], *, local_busy: bool = False) -> Snapshot:
    def rung(class_id, harness, *, slots=2, local=False):
        return ProfileFacts(
            id=f"{class_id}-{harness}", harness=harness, provider=harness, lifecycle="pool",
            default_class=class_id, classes=classes, slots=slots, local=local,
        )

    profiles = [rung(c, h) for c in sorted(classes) for h in ("claude", "codex")]
    profiles += [rung(c, "opencode", slots=1, local=True)
                 for c in ("standard-high", "fast-low", "fast-off")]
    zen = ("opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat")
    profiles += [rung("standard-high", h, slots=1) for h in zen]
    providers = {h: ProviderFacts() for h in ("claude", "codex", "opencode", *zen)}
    busy = {p.id: p.slots for p in profiles if p.local} if local_busy else {}
    return Snapshot(profiles=tuple(profiles), providers=providers, busy=busy)


def _answer(kind, class_id, *, narrow, tested, risk, why, verifier=False):
    return {
        "task_type": kind, "intelligence_class": class_id, "narrow": narrow,
        "test_verified": tested, "independent_verifier": verifier,
        "reason": why, "risk": risk, "risk_reason": why,
    }


FR45 = {"task_type": "feature", "class_hint": "standard-high", "priority": 230}

#: (label, TaskFacts fields, classifier answer); a label ending in
#: :data:`BUSY` runs with every local OpenCode slot taken.
BUSY = " (local OpenCode busy)"
EXAMPLES = [
    ("fleet-ridge-45.1 recovery resumes on the task's own branch", FR45,
     _answer("feature", "standard-high", narrow=False, tested=True, risk="very_high",
             why="owner recovery and git branch handling across five modules")),
    ("fleet-ridge-45.2 delete branches on land", FR45,
     _answer("feature", "standard-high", narrow=True, tested=True, risk="very_high",
             why="deletes branches on origin; one settle path, ancestry-proven, tested")),
    ("fleet-ridge-45.3 delete branches on abandon + audit table", FR45,
     _answer("feature", "standard-high", narrow=False, tested=True, risk="very_high",
             why="deletes unmerged branches and adds an Alembic table")),
    ("fleet-ridge-45.4 hand-landing merges, never rebases", FR45,
     _answer("feature", "standard-high", narrow=True, tested=True, risk="high",
             why="changes how the hand-land tool writes git history")),
    ("fleet-ridge-45.4 docs-only slice (guide text)",
     {**FR45, "task_type": "docs", "priority": 100},
     _answer("docs", "standard-high", narrow=True, tested=True, risk="low",
             why="guide text only; the docs build checks links")),
    ("fleet-ridge-45.4 docs-only slice at the epic's priority",
     {**FR45, "task_type": "docs"},
     _answer("docs", "standard-high", narrow=True, tested=True, risk="low",
             why="guide text only; the docs build checks links")),
    ("fleet-ridge-45.5 provenance refs off refs/heads", FR45,
     _answer("feature", "standard-high", narrow=True, tested=True, risk="high",
             why="ref migration with a dry-run mode; copies before deleting")),
    ("fleet-ridge-45.6 daily backstop branch sweep", FR45,
     _answer("feature", "standard-high", narrow=False, tested=True, risk="very_high",
             why="unattended daily deletion of branches across projects")),
    ("fleet-ridge-45.7 one-time backlog branch cleanup",
     {**FR45, "task_type": "chore", "class_hint": "fast-high"},
     _answer("chore", "fast-high", narrow=False, tested=False, risk="very_high",
             why="destructive on origin: deletes ~765 branches")),
    ("quilt-trader: order-size rounding fix, narrow + tested",
     {"task_type": "bugfix"},
     _answer("bugfix", "standard-high", narrow=True, tested=True, risk="very_high",
             why="the live trader's order path; one function with unit tests")),
    ("quilt-trader: new position-sizing strategy",
     {"task_type": "feature"},
     _answer("feature", "deep-low", narrow=False, tested=False, risk="very_high",
             why="new money-moving logic across the strategy and execution modules")),
    ("docs: fix a guide's broken links",
     {"task_type": "docs"},
     _answer("docs", "fast-high", narrow=True, tested=True, risk="low",
             why="docs only; the link check verifies it")),
    ("UI: narrow, test-verified dashboard tweak",
     {"task_type": "feature"},
     _answer("feature", "standard-high", narrow=True, tested=True, risk="low",
             why="one component, covered by its component test")),
    ("UI: the same tweak" + BUSY,
     {"task_type": "feature"},
     _answer("feature", "standard-high", narrow=True, tested=True, risk="low",
             why="one component, covered by its component test")),
    ("same tweak in shared code (medium risk)",
     {"task_type": "feature"},
     _answer("feature", "standard-high", narrow=True, tested=True, risk="medium",
             why="shared store used by every page; tested")),
    ("trivial chore: bump a pinned version",
     {"task_type": "chore"},
     _answer("chore", "fast-high", narrow=True, tested=True, risk="low",
             why="one line, CI verifies")),
    ("trivial chore" + BUSY,
     {"task_type": "chore"},
     _answer("chore", "fast-high", narrow=True, tested=True, risk="low",
             why="one line, CI verifies")),
    ("same chore at priority 200",
     {"task_type": "chore", "priority": 200},
     _answer("chore", "fast-high", narrow=True, tested=True, risk="low",
             why="one line, CI verifies")),
]


def _route(task, policy, digest, snapshot, answer) -> tuple[str, str]:
    first = plan_route(task, policy, snapshot, policy_sha256=digest)
    asked = ", ".join(first.value.get("questions", [])) if (
        first.outcome == "needs_classification") else "—"
    result = first
    if first.outcome == "needs_classification":
        result = plan_route(task, policy, snapshot, policy_sha256=digest,
                            classification=answer)
    if result.outcome != "planned":
        return asked, f"{result.outcome}: {result.value.get('reason', '')}"
    value = result.value
    route = f"`{value['profile_id']}`"
    if value.get("lane"):
        route += f" (lane {value['lane']})"
    raised = value.get("class_raised_for_risk")
    if raised:
        route += f" — raised from {raised['from']}"
    return asked, route


def main() -> int:
    shipped, shipped_digest = _policy(SHIPPED)
    draft, draft_digest = _policy(DRAFT)
    classes = frozenset(draft.class_order)
    print(f"shipped policy {shipped_digest}  \ndraft policy {draft_digest}\n")
    print("| Example | Prio | Risk | Shipped route | Draft asks | Draft route |")
    print("|---|---|---|---|---|---|")
    for index, (label, fields, answer) in enumerate(EXAMPLES, 1):
        # 100 is the column default (``tasks.priority``).
        fields = {"priority": 100, **fields}
        task = TaskFacts(task_id=f"ex-{index}", title=label, description=label, **fields)
        snapshot = _fleet(classes, local_busy=label.endswith(BUSY))
        _, before = _route(task, shipped, shipped_digest, snapshot, answer)
        asked, after = _route(task, draft, draft_digest, snapshot, answer)
        print(f"| {label} | {fields['priority']} | {answer['risk']} | {before} | {asked} "
              f"| {after} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
