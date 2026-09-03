"""Which reviewed fixtures may enter an activation set.

Package 6 §3.4: a fixture whose ``review.md`` says ``decision: rejected`` is a
recorded negative — no activation may reference it.  The rule lives here rather
than inline in a test so that both the artifact suite and the release check ask
the same question, and so the answer depends on the *decision*, never on whether
a directory happens to contain artifact bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def review_frontmatter(directory: Path) -> dict | None:
    """The parsed ``review.md`` frontmatter, or ``None`` when there is none."""
    review = directory / "review.md"
    if not review.is_file():
        return None
    match = _FRONTMATTER.match(review.read_text(encoding="utf-8"))
    if match is None:
        return None
    parsed = yaml.safe_load(match.group(1))
    return parsed if isinstance(parsed, dict) else None


def activatable_fixture_ids(fixture_root: Path) -> tuple[str, ...]:
    """Fixture directory names a human approved for activation.

    A directory qualifies only when its ``review.md`` records
    ``decision: approved`` **and** it carries the artifact that decision
    approved.  Everything else — rejected, undecided, unreadable, or approved
    with no artifact — is excluded, because failing closed is the only safe
    direction for a file that claims a human's approval.
    """
    approved: list[str] = []
    for directory in sorted(p for p in fixture_root.iterdir() if p.is_dir()):
        review = review_frontmatter(directory)
        if review is None or review.get("decision") != "approved":
            continue
        if not (directory / "artifact.json").is_file():
            continue
        approved.append(directory.name)
    return tuple(approved)
