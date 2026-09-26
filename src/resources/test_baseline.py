"""Recorded test-baseline snapshots, independent of the managed result store.

Consumes exact failure ids from the canonical result parser; it does not parse
pytest output, run tests, fetch URLs, or write a baseline. A snapshot is captured
at job start and remains immutable when the note subsequently changes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BaselineStatus = Literal["matched", "missing", "invalid", "stale"]


def normalize_node_id(node_id: str) -> str:
    """Only trim exterior whitespace; parameter values and their spaces are identity."""
    return node_id.strip()


@dataclass(frozen=True)
class BaselineSnapshot:
    baseline_ref: str | None
    content_hash: str | None
    captured_at: float
    source_commit: str | None
    failure_ids: frozenset[str]
    status: BaselineStatus

    def annotate(self, failure_ids: list[str], *, parse_complete: bool) -> dict:
        """Membership is evidence, never an outcome or a claim of causation."""
        raw = list(dict.fromkeys(normalize_node_id(n) for n in failure_ids))
        available = self.status == "matched"
        return {
            "failure_ids": raw,
            "baseline_ref": self.baseline_ref,
            "baseline_content_hash": self.content_hash,
            "baseline_captured_at": self.captured_at,
            "baseline_source_commit": self.source_commit,
            "baseline_status": self.status,
            "known_failures": [n for n in raw if n in self.failure_ids] if available else [],
            "not_in_baseline": [n for n in raw if n not in self.failure_ids] if available else [],
            "comparison_complete": available and parse_complete,
            "comparison_note": (
                "Exact membership only; not in baseline does not establish causation."
                if available and parse_complete
                else "Comparison incomplete: only observed failure ids are classified."
                if available
                else "Comparison unavailable."
            ),
        }


def read_baseline(
    path: Path, *, captured_at: float, comparison_commit: str | None
) -> BaselineSnapshot:
    try:
        # Bound the authorized local note read; linked URLs are never followed.
        with path.open("rb") as stream:
            content = stream.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return BaselineSnapshot(str(path), None, captured_at, None, frozenset(), "missing")
    except OSError:
        return BaselineSnapshot(str(path), None, captured_at, None, frozenset(), "invalid")
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    invalid = BaselineSnapshot(str(path), digest, captured_at, None, frozenset(), "invalid")
    if len(content) > 1024 * 1024:
        return invalid
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return invalid
    commit = re.search(r"main SHA\s+`?([a-f0-9]{40})`?", text)
    section = re.search(
        r"^## Known failures[^\n]*\n(.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL
    )
    if not commit or not section:
        return invalid
    node_ids: set[str] = set()
    for line in section[1].splitlines():
        if not line.startswith("- "):
            continue
        match = re.fullmatch(r"- `([^`]+)`\s*", line)
        if not match or "::" not in match[1]:
            return invalid  # Never claim a partially parsed baseline is valid.
        node_ids.add(normalize_node_id(match[1]))
    if not node_ids and not re.search(r"\b(no known failures|none)\b", section[1], re.I):
        return invalid
    source = commit[1]
    status: BaselineStatus = "matched" if source == comparison_commit else "stale"
    return BaselineSnapshot(str(path), digest, captured_at, source, frozenset(node_ids), status)


def capture_latest_baseline(
    notes_dir: Path, *, captured_at: float, comparison_commit: str | None
) -> BaselineSnapshot:
    """Latest valid recorded project baseline, at the caller's job-start instant.

    An invalid newer file cannot replace the latest valid recorded evidence.
    Relevance is explicit: a different/unknown source commit is stale, without
    inventing an age threshold or manufacturing fresh evidence by running tests.
    """
    candidates = sorted(notes_dir.glob("full-suite-baseline-????-??-??.md"), reverse=True)
    last_invalid = None
    for path in candidates:
        snapshot = read_baseline(path, captured_at=captured_at, comparison_commit=comparison_commit)
        if snapshot.status in ("matched", "stale"):
            return snapshot
        last_invalid = last_invalid or snapshot
    return last_invalid or BaselineSnapshot(None, None, captured_at, None, frozenset(), "missing")
