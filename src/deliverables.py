"""Deterministic close-time checks for plan-derived task deliverables.

Targets come in two shapes.  A *path-like* target (``src/models.py``,
``tests/test_x.py``) is checked against the worktree.  A *command-like*
target — anything containing whitespace, such as ``aq test tests/a.py
tests/b.py`` or ``ruff check <changed files>`` — is checked against the
repeatable ``--test`` / ``--command`` values recorded on the close, because a
shell command is never a file under the worktree and is not text the repo
is expected to contain.

A ``review`` item is different again: its evidence is a document review the
task submitted (``aq review submit``), so the caller passes the task's
reviews in.  Research and design tasks carry one implicitly (see
:func:`effective_deliverables`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".ts", ".tsx", ".js"}
_SKIP_PARTS = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}
_PLACEHOLDER = re.compile(r"<[^<>]*>")

#: A ``review`` item's target: the kind of document review the task must
#: submit (``doc_reviews.kind``), or ``any``.
REVIEW_TARGETS = ("spec", "plan", "other", "any")

#: Task types whose product is a document for a human decision: a research
#: report or a design proposal.
DOCUMENT_TASK_TYPES = frozenset({"research", "design"})


def effective_deliverables(
    deliverables: list[dict[str, str]] | None,
    *,
    task_type: Any,
    dispatched_reviewer: bool = False,
) -> list[dict[str, str]]:
    """The task's declared deliverables plus its implicit document review.

    A research or design task hands back a document, and a document is for a
    human decision: it goes to Reviews through ``aq review submit``, not only
    onto a branch where nobody sees it (vivid-delta: research task
    crisp-orbit-37 committed its proposal and never reached the Reviews tab).
    Such a task that declares no ``review`` item gets ``{"id": "review",
    "kind": "review", "target": "any"}``; a task with nothing to submit waives
    it at close with ``--deliverable-unmet 'review: <reason>'``.

    The rule reads the type the task has *now*, not at creation, because the
    assignment router writes the type of an untyped task after it is filed.
    A dispatched adversarial reviewer is research-typed but answers with
    comments on someone else's revision, so it is exempt.
    """
    items = [dict(item) for item in deliverables or []]
    kind = getattr(task_type, "value", task_type)
    if (
        dispatched_reviewer
        or kind not in DOCUMENT_TASK_TYPES
        or any(item["kind"] == "review" for item in items)
    ):
        return items
    taken = {item["id"] for item in items}
    item_id, suffix = "review", 2
    while item_id in taken:
        item_id, suffix = f"review-{suffix}", suffix + 1
    return [*items, {"id": item_id, "kind": "review", "target": "any"}]


async def resolve_task_deliverables(db: Any, task: Any) -> list[dict[str, str]]:
    """:func:`effective_deliverables` with stored review and experiment context."""
    task_type = getattr(task, "task_type", None)
    task_type = getattr(task_type, "value", task_type)
    if task_type in DOCUMENT_TASK_TYPES:
        from src.object_loop.reviews import experiment_context

        experiment = await experiment_context(db, task.id)
        if experiment and not experiment["final_result"]:
            # Internal experiments hand back artifacts/score packets. Preserve
            # explicitly requested documents without inventing a human gate.
            return [dict(item) for item in getattr(task, "deliverables", None) or []]
    dispatched = (
        task_type in DOCUMENT_TASK_TYPES
        and await db.get_review_dispatch_for_task(task.id) is not None
    )
    return effective_deliverables(
        getattr(task, "deliverables", None), task_type=task_type, dispatched_reviewer=dispatched
    )


def review_submit_guidance(task_id: str, items: list[dict[str, Any]]) -> str:
    """How to meet (or visibly waive) the ``review`` items among *items*."""
    reviews = [item for item in items if item["kind"] == "review"]
    if not reviews:
        return ""
    kinds = sorted({str(item["target"]) for item in reviews} - {"any"})
    kind = "|".join(kinds) if kinds else "spec|plan|other"
    waiver = " ".join(f"--deliverable-unmet '{item['id']}: <reason>'" for item in reviews)
    return (
        "A review item is met by a document review this task submitted: write the "
        "document in your checkout, do not commit it, and run "
        f"`aq review submit --task-id {task_id} --file <draft.md> --kind {kind} "
        '--title "<title>"`; put the review id in your close summary. '
        f"Only when the task produced no document, close with {waiver}."
    )


def parse_unmet_reasons(
    raw: Any, deliverables: list[dict[str, str]]
) -> tuple[dict[str, str], str | None]:
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
    deliverables: list[dict[str, str]],
    *,
    root: Path,
    tests: list[str],
    commands: list[str] | None = None,
    reviews: list[dict[str, Any]] | None = None,
) -> list[dict[str, str | bool]]:
    """Evaluate declared items using only local, reproducible evidence.

    ``tests`` and ``commands`` are the repeatable ``--test`` / ``--command``
    values recorded on the close.  A ``test`` item is only ever satisfied by
    ``tests``; a ``command`` item accepts either list, since a test command is
    also a command that was run.  ``reviews`` are the document reviews the
    task submitted (``kind`` and ``state`` each), the evidence for a
    ``review`` item.
    """
    commands = commands or []
    reviews = reviews or []
    return [
        {
            **item,
            "met": _is_met(item, root=root, tests=tests, commands=commands, reviews=reviews),
            "reason": "",
        }
        for item in deliverables
    ]


def _is_met(
    item: dict[str, str],
    *,
    root: Path,
    tests: list[str],
    commands: list[str],
    reviews: list[dict[str, Any]],
) -> bool:
    kind, target = item["kind"], item["target"].strip()
    if kind == "review":
        # A withdrawn review is no longer awaiting anyone's decision.
        return any(
            review.get("state") != "withdrawn" and target in ("any", review.get("kind"))
            for review in reviews
        )
    command_like = _is_command_like(target)
    if kind == "test":
        if command_like:
            return _test_command_met(target, root=root, tests=tests)
        return _file_exists(root, target) and any(target in command for command in tests)
    if kind == "file":
        return _file_exists(root, target)
    if kind == "command":
        if _command_matches(target, [*commands, *tests]):
            return True
        return not command_like and _repo_text_contains(root, target)
    return _repo_text_contains(root, target)


def _is_command_like(target: str) -> bool:
    return any(ch.isspace() for ch in target)


def _file_exists(root: Path, target: str) -> bool:
    candidate = (root / target).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return candidate.is_file()


def _test_command_met(target: str, *, root: Path, tests: list[str]) -> bool:
    """A test suite declared as its command line.

    Every path the command names must exist under the worktree.  Then the
    item is met when a recorded ``--test`` value matches the declared
    command, or — so a worker who ran the suites one at a time is not
    refused — when each of those paths is named by some recorded ``--test``.
    """
    paths = [token for token in target.split() if _looks_like_path(token)]
    if not all(_path_exists(root, path) for path in paths):
        return False
    if _command_matches(target, tests):
        return True
    return bool(paths) and all(any(path in command for command in tests) for path in paths)


def _path_exists(root: Path, token: str) -> bool:
    """``tests/test_x.py::test_a`` and ``tests/`` both count as existing paths."""
    candidate = (root / token.split("::", 1)[0]).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return candidate.exists()


def _looks_like_path(token: str) -> bool:
    if token.startswith("-") or _PLACEHOLDER.fullmatch(token):
        return False
    return "/" in token or "." in token


def _command_matches(target: str, recorded: list[str]) -> bool:
    """Whitespace-insensitive match; ``<placeholder>`` segments match anything."""
    pattern = _command_pattern(target)
    return any(pattern.search(_squash(command)) for command in recorded)


def _command_pattern(target: str) -> re.Pattern[str]:
    pattern = re.escape(_squash(target)).replace(r"\ ", r"\s+")
    pattern = _PLACEHOLDER.sub(".+", pattern)
    return re.compile(rf"(?:^|\s){pattern}(?:\s|$)")


def _squash(text: str) -> str:
    return " ".join(text.split())


def _repo_text_contains(root: Path, target: str) -> bool:
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or _SKIP_PARTS.intersection(path.parts)
            or path.suffix not in _TEXT_SUFFIXES
        ):
            continue
        try:
            if target in path.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False
