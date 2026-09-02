"""How a task the default pipeline created as a review is recognised.

Two marks, both written by ``src/prompts/default_playbooks/default-pipeline.md``
itself: the ``ensure_task`` dedup key, and the ``profile_id`` its review nodes
pin.

``per-task-review`` writes ``review:task:<task_id>`` under ``profile_id:
reviewer``; ``per-branch-final-review`` writes ``branch-review:<branch_name>``
under ``profile_id: final-reviewer``.  The markdown is the source of truth and
the constants here must not drift from it.  Two other places read them:

* ``src/doctor/integration_checks.py`` looks for the ``review:task:`` row to
  decide whether a finished PR was ever reviewed;
* the ``task.completed`` emitters (``execution.py`` on the session close path,
  ``monitoring.py`` on container settlement) flag a finishing task that carries
  either mark as ``review_task`` so the review rules never review a review.
  The older ``no_code`` flag came from the reviewer profile's ``read_only``
  setting, which an operator can (and did) turn off; these marks are the
  pipeline's own and survive any profile edit.

The dedup key alone is not enough: ``ensure_task`` is only one way a review row
is born.  A review created by hand, by a formula, or by a project's own flow
carries no key but still runs under ``reviewer`` / ``final-reviewer``, and
without the profile half of the mark its completion queued a review of itself
(task crisp-summit-88).  ``PIPELINE_REVIEW_PROFILE_IDS`` is therefore also the
home of the profile-id set that ``orchestrator/git_ops.py`` and
``commands/task_commands.py`` had each grown by hand.

Kept dependency-free so the doctor, the orchestrator and the command handler
can all import it.
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

#: The profiles the pipeline's review nodes pin.  A task running under one of
#: them is a review whatever created it and whatever dedup key it carries.
PIPELINE_REVIEW_PROFILE_IDS: frozenset[str] = frozenset({"reviewer", "final-reviewer"})


def review_task_dedup_key(task_id: str) -> str:
    """The dedup key ``per-task-review`` uses for the review of *task_id*."""
    return f"{REVIEW_TASK_DEDUP_PREFIX}{task_id}"


def branch_review_dedup_key(branch_name: str) -> str:
    """The dedup key ``per-branch-final-review`` uses for *branch_name*."""
    return f"{BRANCH_REVIEW_DEDUP_PREFIX}{branch_name}"


def is_pipeline_review_task(dedup_key: str | None, profile_id: str | None = None) -> bool:
    """True when either mark says this task is a review, not reviewable work.

    Args:
        dedup_key: the task's ``ensure_task`` key, if it has one.
        profile_id: the task's executing profile, if known.  Optional so the
            older key-only call sites keep working unchanged.
    """
    if dedup_key and dedup_key.startswith(PIPELINE_REVIEW_DEDUP_PREFIXES):
        return True
    return bool(profile_id) and profile_id in PIPELINE_REVIEW_PROFILE_IDS
