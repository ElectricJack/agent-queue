#!/usr/bin/env python3
"""Check that every documentation file carries a disposition, and that the
pages which need a historical banner have one.

Run from the repository root:

    python3 docs/history/check_dispositions.py          # verdict, exit 1 on failure
    python3 docs/history/check_dispositions.py --list   # also print every row

The ledger is `docs/history/disposition-ledger.md`.  Scope is derived, not
listed: a path is in scope when the overhaul's coverage manifest assigns it to
the `legacy` shard, or categorises it as `documentation` in any shard.  Prose
that ships beside the code it documents (`src/`, `tests/`, `packages/`,
`dashboard/`, `vault/`) is out of scope — changing it changes agent or build
behaviour, so it belongs to the ticket that owns that code.

This is the `legacy` shard's own check.  It is the disposition half of the pair;
the coverage half is
`python3 docs/plans/documentation-overhaul/refresh_inventory.py --check`, which
fails when a tracked path matches no ownership rule.  Run both after adding or
removing a documentation file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs" / "history" / "disposition-ledger.md"
MANIFEST_DIR = ROOT / "docs" / "plans" / "documentation-overhaul"

DISPOSITIONS = {"current", "update", "redirect", "archive", "historical"}

# Prose that ships with the code; documented by the ticket owning that code.
CODE_ADJACENT = ("src/", "tests/", "packages/", "dashboard/", "vault/")

# A row in the ledger's per-file tables.
ROW = re.compile(r"^\| \[`(?P<path>[^`]+)`\]\([^)]*\) \| `(?P<disp>[a-z]+)` \|")

# Dispositions whose Markdown pages must carry a banner, and the marker to
# look for.  A binary or JSON file cannot carry one.
BANNER_REQUIRED = {
    "historical": ("<!-- aq:historical -->",),
    "archive": ("<!-- aq:historical -->",),
    "redirect": ("<!-- aq:redirect -->",),
}

# Historical trees whose file bodies are audit evidence: they are indexed by a
# directory README rather than bannered file by file.
EVIDENCE_TREES = (
    "docs/reports/",
    "docs/reviews/",
    "docs/gates/",
    "docs/superpowers/reports/",
    "reports/",
    ".superpowers/sdd/",
    "docs/specs/.obsidian/",
)


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def in_scope() -> set[str]:
    sys.path.insert(0, str(MANIFEST_DIR))
    import refresh_inventory as ri

    paths: set[str] = set()
    for path in tracked():
        if path.startswith(CODE_ADJACENT):
            continue
        hit = ri.classify(path)
        if hit is None:
            continue
        shard, _component, category, _note = hit
        if shard == "legacy" or category == "documentation":
            paths.add(path)
    return paths


def ledger_rows() -> tuple[dict[str, str], list[str]]:
    rows: dict[str, str] = {}
    problems: list[str] = []
    if not LEDGER.exists():
        return rows, [f"missing ledger: {LEDGER.relative_to(ROOT)}"]
    for lineno, line in enumerate(LEDGER.read_text(encoding="utf-8").splitlines(), 1):
        m = ROW.match(line)
        if not m:
            continue
        path, disp = m.group("path"), m.group("disp")
        if disp not in DISPOSITIONS:
            problems.append(f"{LEDGER.name}:{lineno}: unknown disposition {disp!r} for {path}")
        if path in rows:
            problems.append(f"{LEDGER.name}:{lineno}: duplicate row for {path}")
        rows[path] = disp
    return rows, problems


def banner_problems(rows: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for path, disp in sorted(rows.items()):
        markers = BANNER_REQUIRED.get(disp)
        if not markers or not path.endswith(".md"):
            continue
        if any(path.startswith(t) and path != t + "README.md" for t in EVIDENCE_TREES):
            continue  # indexed by the tree's own README; bodies left unedited
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        if not any(marker in text for marker in markers):
            problems.append(f"{path}: disposition `{disp}` but no {markers[0]} banner")
    return problems


def main() -> int:
    scope = in_scope()
    rows, problems = ledger_rows()

    for path in sorted(scope - set(rows)):
        problems.append(f"{path}: in scope but has no disposition row")
    for path in sorted(set(rows) - scope):
        if not (ROOT / path).exists():
            problems.append(f"{path}: ledger row for a path that no longer exists")
        else:
            problems.append(f"{path}: ledger row for a path outside the ledger's scope")
    problems.extend(banner_problems(rows))

    if "--list" in sys.argv:
        for path, disp in sorted(rows.items()):
            print(f"{disp:11} {path}")

    counts = {d: sum(1 for v in rows.values() if v == d) for d in sorted(DISPOSITIONS)}
    summary = ", ".join(f"{d}={n}" for d, n in counts.items())
    if problems:
        print(f"FAIL — {len(problems)} problem(s); {len(rows)} row(s): {summary}")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"ok — {len(rows)} documentation file(s) have a disposition: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
