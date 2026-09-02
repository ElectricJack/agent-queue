"""How the system recognises a task whose work product is a review verdict.

A review leaves no branch and no diff, so nothing about it should be routed
back into the review stage — but a reviewer runs on an ordinary worktree slot
checked out on its own ``aq/<id>`` branch, so its row looks like any other
session task from the outside.  Left unrecognised, every finished review
spawned a review *of* the review: task solid-beacon-50 caught a seven-deep
``Review: Review: ...`` chain grown from one CI-red task.

Three independent signals say "this was a review", and the close path
(``execution.py``) ORs them into the ``review_task`` flag on
``task.completed``, which both review rules in
``src/prompts/default_playbooks/default-pipeline.md`` guard on.  Three,
because each one alone is disarmable:

* **the profile's ``read_only`` flag** — carried separately as ``no_code``
  (``git_ops._task_produces_no_code``).  An operator who hands the reviewer
  Write/Edit tools turns it off.
* **the pipeline's dedup key** — ``per-task-review`` writes
  ``review:task:<task_id>``, ``per-branch-final-review`` writes
  ``branch-review:<branch_name>``.  A project that routes reviews through its
  own pipeline keys the rows however it likes, and this reads False.
* **the reviewer role** — the ``reviewer`` / ``final-reviewer`` profile ids.
  Survives both of the above, and is what a custom pipeline running the
  shipped reviewer profiles still trips.

``src/doctor/integration_checks.py`` is the fourth consumer: it looks for the
``review:task:`` row to decide whether a finished PR was ever reviewed.  The
playbook markdown remains the source of truth for the key strings; nothing
here may drift from it.

Kept dependency-free so both the doctor and the orchestrator can import it.
"""

from __future__ import annotations

#: ``ensure_task`` dedup key prefix for one reviewer task per reviewed task.
REVIEW_TASK_DEDUP_PREFIX = "review:task:"

#: ``ensure_task`` dedup key prefix for one final-reviewer task per branch.
BRANCH_REVIEW_DEDUP_PREFIX = "branch-review:"

#: Every prefix that marks a row the pipeline created as a review.
PIPELINE_REVIEW_DEDUP_PREFIXES: tuple[str, ...] = (
    REVIEW_TASK_DEDUP_PREFIX,
    BRANCH_REVIEW_DEDUP_PREFIX,
)


def review_task_dedup_key(task_id: str) -> str:
    """The dedup key ``per-task-review`` uses for the review of *task_id*."""
    return f"{REVIEW_TASK_DEDUP_PREFIX}{task_id}"


def branch_review_dedup_key(branch_name: str) -> str:
    """The dedup key ``per-branch-final-review`` uses for *branch_name*."""
    return f"{BRANCH_REVIEW_DEDUP_PREFIX}{branch_name}"


#: Profile ids whose whole job is to produce a review verdict.  These profiles
#: ship with ``read_only: true`` (``src/profiles/defaults/{reviewer,
#: final-reviewer}/profile.md``), but the id is the signal here, not the flag —
#: that is the point of keeping it separate from ``no_code``.
REVIEW_PROFILE_IDS: frozenset[str] = frozenset({"reviewer", "final-reviewer"})


def is_pipeline_review_task(dedup_key: str | None) -> bool:
    """True when *dedup_key* marks a task the pipeline itself created as a review."""
    if not dedup_key:
        return False
    return dedup_key.startswith(PIPELINE_REVIEW_DEDUP_PREFIXES)


def is_review_role(profile_id: str | None) -> bool:
    """True when *profile_id* names a profile that only ever produces a verdict.

    Deliberately id-based: unlike ``read_only`` this cannot be edited away in
    the profile markdown, so it still holds for a project that gives its
    reviewers write tools.
    """
    return (profile_id or "") in REVIEW_PROFILE_IDS


def is_review_completion(dedup_key: str | None, profile_id: str | None) -> bool:
    """True when a task finishing with these fields produced a review verdict.

    The ``review_task`` flag the close path puts on ``task.completed``.  Either
    structural signal is enough; both are checked because a project can defeat
    either one on its own (see the module docstring).
    """
    return is_pipeline_review_task(dedup_key) or is_review_role(profile_id)
