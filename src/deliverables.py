"""Deterministic close-time checks for plan-derived task deliverables."""

from __future__ import annotations

from pathlib import Path
from typing import Any


_TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".ts", ".tsx", ".js"}
_SKIP_PARTS = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}


def parse_unmet_reasons(raw: Any, deliverables: list[dict[str, str]]) -> tuple[dict[str, str], str | None]:
    """Parse repeatable ``ID: reason`` exemptions without silently ignoring typos."""
    if raw is None:
        return {}, None
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list):
        return {}, "deliverable_unmet must be a repeatable 'id: reason' value"
    known = {item["id"] for item in deliverables}
    reasons: dict[str, str] = {}
    for value in values:
        item_id, separator, reason = str(value).partition(":")
        item_id, reason = item_id.strip(), reason.strip()
        if not separator or not item_id or not reason:
            return {}, "each deliverable_unmet entry must be 'id: reason'"
        if item_id not in known:
            return {}, f"deliverable_unmet names unknown deliverable '{item_id}'"
        if item_id in reasons:
            return {}, f"duplicate deliverable_unmet reason for '{item_id}'"
        reasons[item_id] = reason
    return reasons, None


def evaluate_deliverables(
    deliverables: list[dict[str, str]], *, root: Path, tests: list[str]
) -> list[dict[str, str | bool]]:
    """Evaluate declared items using only local, reproducible evidence."""
    return [
        {**item, "met": _is_met(item, root=root, tests=tests), "reason": ""}
        for item in deliverables
    ]


def _is_met(item: dict[str, str], *, root: Path, tests: list[str]) -> bool:
    kind, target = item["kind"], item["target"]
    if kind in {"file", "test"}:
        candidate = (root / target).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return False
        if not candidate.is_file():
            return False
        return kind != "test" or any(target in command for command in tests)
    return _repo_text_contains(root, target)


def _repo_text_contains(root: Path, target: str) -> bool:
    for path in root.rglob("*"):
        if not path.is_file() or _SKIP_PARTS.intersection(path.parts) or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            if target in path.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False
