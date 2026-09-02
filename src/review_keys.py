"""The dedup keys the default pipeline stamps on the review tasks it creates.

``src/prompts/default_playbooks/default-pipeline.md`` is the source of truth:
``per-task-review`` writes ``review:task:<task_id>`` and
``per-branch-final-review`` writes ``branch-review:<branch_name>``.  Two other
places need the same strings and must not drift from it:

* ``src/doctor/integration_checks.py`` looks for the ``review:task:`` row to
  decide whether a finished PR was ever reviewed;
* the session close path (``execution.py``) flags a finishing task that carries
  either key as ``review_task`` on ``task.completed`` so the review rules never
  review a review.  The older ``no_code`` flag came from the reviewer profile's
  ``read_only`` setting, which an operator can (and did) turn off; the dedup
  key is the pipeline's own mark on the row and survives any profile edit;
* the pipeline dispatch path (``Orchestrator._on_playbook_trigger``) sets the
  same flag again from the hydrated task row via :func:`flag_review_task_event`;
* ``ensure_task`` (``src/commands/task_commands.py``) refuses to create a
  ``review:task:<X>`` row when X itself carries either key, via
  :func:`reviewed_task_id` + :func:`is_pipeline_review_task`.  The three
  guards above all live on the ``task.completed`` event and the rules' ``when``
  clauses, and none of them reaches a daemon still running older code or an
  operator-edited vault copy of the pipeline whose rules lack the guards
  (``ensure_default_playbooks`` never refreshes a copy it does not recognise):
  ``Review: Review: ...`` chains ten deep reached the live queue that way
  (task solid-harbor-68).  The command that writes the row is the one place
  every version of the pipeline must pass through.
  The rules guard with ``truthy: false``, which passes on a *missing* key, so
  an emitter that never sets it — a daemon still running code older than the
  flag, container settlement, a hand-written event — used to fire the review
  anyway and ``Review: Review: Review: ...`` chains grew six deep on the live
  queue (task prime-cascade-64).  Deriving it at dispatch makes the guard hold
  for every emitter.

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
    """The task a ``review:task:<id>`` key reviews, or ``None`` for any other key.

    Inverse of :func:`review_task_dedup_key`.  ``branch-review:`` keys name a
    branch, not a task, so they yield ``None`` too.
    """
    if not dedup_key or not dedup_key.startswith(REVIEW_TASK_DEDUP_PREFIX):
        return None
    return dedup_key[len(REVIEW_TASK_DEDUP_PREFIX):] or None


def is_pipeline_review_task(dedup_key: str | None) -> bool:
    """True when *dedup_key* marks a task the pipeline itself created as a review."""
    if not dedup_key:
        return False
    return dedup_key.startswith(PIPELINE_REVIEW_DEDUP_PREFIXES)


def flag_review_task_event(event: dict, dedup_key: str | None) -> dict:
    """Set ``review_task`` on *event* when *dedup_key* marks a pipeline review.

    Mutates and returns *event*.  Only ever narrows: a flag the emitter already
    set is left alone, and a task without a review key gets no flag at all, so
    the ``truthy: false`` guards in the review rules read exactly as before for
    ordinary work.
    """
    if is_pipeline_review_task(dedup_key):
        event["review_task"] = True
    return event
