"""The dedup keys the default pipeline stamps on the review tasks it creates.

``src/prompts/default_playbooks/default-pipeline.md`` is the source of truth:
``per-task-review`` writes ``review:task:<task_id>`` and
``per-branch-final-review`` writes ``branch-review:<branch_name>``.  Two other
places need the same strings and must not drift from it:

* ``src/doctor/integration_checks.py`` looks for the ``review:task:`` row to
  decide whether a finished PR was ever reviewed;
* ``Orchestrator._emit_task_event`` flags a finishing task that carries either
  key as ``review_task`` on ``task.completed`` so the review rules never review
  a review.  The older ``no_code`` flag came from the reviewer profile's
  ``read_only`` setting, which an operator can (and did) turn off; the dedup
  key is the pipeline's own mark on the row and survives any profile edit.
  The flag is stamped in the shared emitter rather than at the session close
  path so container settlement — the other emitter of ``task.completed`` —
  carries it too.

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


def is_pipeline_review_task(dedup_key: str | None) -> bool:
    """True when *dedup_key* marks a task the pipeline itself created as a review.

    Anything that is not a non-empty string is "not a review": the caller is
    ``Orchestrator._emit_task_event``, which reads the key off whatever task
    object it was handed, and answering *maybe* there would make the review
    rules stand down on ordinary work.
    """
    if not isinstance(dedup_key, str) or not dedup_key:
        return False
    return dedup_key.startswith(PIPELINE_REVIEW_DEDUP_PREFIXES)
