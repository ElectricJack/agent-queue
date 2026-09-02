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
  The flag is derived at the emit choke point rather than at the session close
  path, so container settlement and any future emitter carry it too;
* ``_cmd_ensure_task`` refuses to create a ``review:task:<X>`` row when ``X``
  itself carries one of these keys.  The playbook guards above live in a
  user-editable vault file, so this is the structural floor that bounds review
  nesting at depth 1 regardless of what a pipeline asks for.

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


def reviewed_task_id(dedup_key: str | None) -> str | None:
    """The id of the task a ``review:task:<id>`` key reviews, else ``None``.

    ``branch-review:`` keys name a branch, not a task, so they return
    ``None``.  ``_cmd_ensure_task`` uses this to recognise a review of a
    review without knowing which pipeline asked for one.
    """
    if not dedup_key or not dedup_key.startswith(REVIEW_TASK_DEDUP_PREFIX):
        return None
    return dedup_key[len(REVIEW_TASK_DEDUP_PREFIX) :] or None


def is_pipeline_review_task(dedup_key: str | None) -> bool:
    """True when *dedup_key* marks a task the pipeline itself created as a review."""
    if not dedup_key:
        return False
    return dedup_key.startswith(PIPELINE_REVIEW_DEDUP_PREFIXES)
