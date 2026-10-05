"""Open the pull request a completed child cannot get from its container.

A completed child of a managed container is delivered by *collection*: the
parent-episode Subject merges its head into the container's branch, the
container verifies, and only a **root** opens a pull request
(:meth:`~src.integration.root_pull_requests.EpicPullRequestService.open_for_epic`
refuses any task with a ``parent_task_id``).  When no collector will ever carry
it, the child's code sits on a pushed branch with no delivery and the train can
never seat it -- bright-rapids-84.1/.2/.3/.4/.7/.9 reached COMPLETED with
``pr_url`` NULL and were only rescued by a supervisor opening pull requests by
hand.  That was luck: the only fallback was the agent's own ``aq git create-pr``,
so the siblings whose worker happened to run it (.5, .6) got pull requests and
the rest did not.

:func:`open_stranded_child_pull_request` is the close-time action.  It proves
the child's finished leaf head from its own checkpoint and origin, proves the
branch is published at exactly that head and is ahead of the default branch,
then opens one pull request to the default branch and records its URL on the
task.  It runs only once :func:`delivery_refusal_on` says no collector exists
or can appear, so the squash-per-root invariant is untouched for a healthy
train.

:func:`stranded_child_statement` is the ``stranded_child`` line of
``aq doctor --check stall.sweep``.  It shares the same predicate, so it names
the same reasons, and it covers what the close-time leg cannot: a GitHub
failure, and every completion that ran before this leg existed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update

from src.database.tables import (
    archived_tasks,
    integration_promotion_intents,
    integration_repair_operations,
    integration_subjects,
    projects,
    repos,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)
from src.integration.child_delivery import (
    _SETTLED_INTENT_STATES,
    MANAGED_MODES,
    _child_refusal,
    _parent_refusal,
    _snapshot_on,
)
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: How long a completed task may carry a pushed branch with no pull request and
#: no delivery before ``stall.sweep`` says so.
STRANDED_CHILD_AFTER_SECONDS = 15 * 60

#: A Subject past this phase walks its episode no more.
_DONE_PHASE = "done"

#: A collection operation in one of these states is still accepting children.
_LIVE_OPERATION_STATES = ("active", "escalated")

#: Container states from which no collection will ever run.  Anything else that
#: is not PAUSED is a container that has not suspended itself yet, and it may
#: still collect -- so its children are not stranded.
_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
)


def render_stranded_child_body(
    *,
    task_id: str,
    title: str,
    parent_id: str,
    parent_title: str,
    reason: str,
) -> str:
    """Describe the child and name the container delivery that cannot carry it."""
    lines = [
        title,
        "",
        (
            f"Its container `{parent_id}` cannot deliver this branch ({reason}), so this "
            "completed child opens its own pull request to the default branch."
        ),
        "",
        f"- Task: `{task_id}`",
        f"- Container: `{parent_id}` {parent_title}",
        "",
        f"AQ-Task: {task_id}",
    ]
    return "\n".join(lines)


async def _subject_phase_on(conn, episode_id: str | None) -> str | None:
    """The phase of the parent-episode Subject walking *episode_id*, if any."""
    if episode_id is None:
        return None
    return (
        await conn.execute(
            select(integration_subjects.c.phase).where(
                integration_subjects.c.parent_episode_id == episode_id,
            )
        )
    ).scalar_one_or_none()


async def _delivery_receipt_on(conn, snapshot: dict[str, Any]) -> str | None:
    """The receipt already carrying this child's head, to any target."""
    head = snapshot["head_sha"]
    if head is None:
        return None
    return (
        await conn.execute(
            select(task_delivery_receipts.c.id)
            .where(
                task_delivery_receipts.c.source_task_id == snapshot["task_id"],
                task_delivery_receipts.c.repository_id == snapshot["repository_id"],
                task_delivery_receipts.c.reviewed_head_sha == head,
            )
            .order_by(task_delivery_receipts.c.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _promotion_in_flight_on(conn, task_id: str, head: str | None) -> str | None:
    """An unfinished promotion of this head, which the collection owns."""
    if head is None:
        return None
    return (
        await conn.execute(
            select(integration_promotion_intents.c.id)
            .where(
                integration_promotion_intents.c.source_task_id == task_id,
                integration_promotion_intents.c.source_head == head,
                integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _collecting_on(conn, snapshot: dict[str, Any]) -> bool:
    """Whether a collector will still carry this child's head into its parent.

    Mirrors the conditions :mod:`src.integration.child_delivery` assembles on
    (``_parent_refusal``) and adds the one it does not depend on: a live
    parent-episode Subject.  An operation with no Subject walking it delivers
    nothing, which is the shape the legacy engine's removal left behind.
    """
    if _parent_refusal(snapshot) is not None:
        return False
    phase = await _subject_phase_on(conn, snapshot["parent_checkpoint"]["episode_id"])
    return phase is not None and phase != _DONE_PHASE


async def delivery_refusal_on(conn, snapshot: dict[str, Any]) -> str | None:
    """Why no collector will ever deliver this completed child, or ``None``.

    ``None`` means a collector exists or may still appear -- the healthy train,
    where the container is suspended and holding an operation a Subject walks,
    or is not suspended yet and may suspend itself.

    The three reasons it does return:

    * ``no_parent_collection`` -- the container is terminal or archived, or
      holds no collection episode, so there is no branch it could assemble this
      child into.
    * ``paused_epic`` -- the container is PAUSED holding an episode whose live
      operation is gone, or whose Subject is done.
    * ``no_consumer`` -- the episode has a live operation but no Subject is
      walking it.  ``ParentSubjectRuntime`` builds its reconciler loop only when
      the parent runtime is active, and ``ensure_parent_subject_on`` returns
      nothing when the pinned playbook artifact is missing or declares no
      ``parent_episode`` table; after the legacy engine's removal
      (``refactor(integration): remove legacy engine and recovery controls``,
      2026-10-04) that is the ordinary state of a collection nothing drives.
    """
    parent = snapshot["parent"]
    parent_checkpoint = snapshot["parent_checkpoint"]
    if parent is None:
        return "no_parent_collection: the container task is gone"
    if parent["status"] in _TERMINAL_STATUSES:
        return (
            f"no_parent_collection: the container is {parent['status']}, "
            "so no collection will run"
        )
    if parent["status"] != TaskStatus.PAUSED.value:
        # Children can be filed on a container that has not suspended itself
        # yet.  It may still collect this child, so nothing is stranded.
        return None
    if parent_checkpoint is None or parent_checkpoint["episode_id"] is None:
        return "no_parent_collection: the container is not holding a collection episode"
    episode_id = parent_checkpoint["episode_id"]
    if snapshot["operation"] is None:
        return (
            f"paused_epic: the container's collection episode {episode_id} "
            "has no live operation"
        )
    if parent_checkpoint["state"] != "awaiting_children":
        return (
            f"paused_epic: the container's checkpoint is {parent_checkpoint['state']}, "
            "not awaiting_children"
        )
    phase = await _subject_phase_on(conn, episode_id)
    if phase is None:
        return f"no_consumer: no Subject is walking the container's episode {episode_id}"
    if phase == _DONE_PHASE:
        return f"paused_epic: the container's episode {episode_id} Subject is done"
    return None


async def _diagnose_snapshot_on(conn, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The child's snapshot and its diagnosis, from one consistent read."""
    snapshot = await _snapshot_on(conn, task_id)
    if snapshot is None:
        return None, {"outcome": "unknown_task", "task_id": task_id}
    base = {
        "task_id": task_id,
        "project_id": snapshot["project_id"],
        "parent_task_id": snapshot["parent_task_id"],
        "branch": snapshot["branch"],
        "head_sha": snapshot["head_sha"],
        "base_sha": snapshot["base_sha"],
    }
    if snapshot["pr_url"] and snapshot["pr_url"].strip():
        return snapshot, {**base, "outcome": "already_open", "pr_url": snapshot["pr_url"]}
    refusal = _child_refusal(snapshot)
    if refusal is not None:
        # A child with no checkpoint predates its container's collection and has
        # no recorded head to propose: a stall, not a pull request.
        outcome, reason = refusal
        return snapshot, {**base, "outcome": outcome, "reason": reason}
    if await _delivery_receipt_on(conn, snapshot) is not None:
        return snapshot, {
            **base,
            "outcome": "delivered",
            "reason": "a receipt already carries its head",
        }
    intent = await _promotion_in_flight_on(conn, task_id, snapshot["head_sha"])
    if intent is not None:
        return snapshot, {
            **base,
            "outcome": "not_stranded",
            "reason": f"promotion {intent} of this head is in flight",
        }
    if await _collecting_on(conn, snapshot):
        return snapshot, {
            **base,
            "outcome": "not_stranded",
            "reason": "its container still has a live collector for this child",
        }
    reason = await delivery_refusal_on(conn, snapshot)
    if reason is None:
        return snapshot, {
            **base,
            "outcome": "not_stranded",
            "reason": "its container may still collect",
        }
    return snapshot, {**base, "outcome": "stranded", "reason": reason}


async def diagnose_on(conn, task_id: str) -> dict[str, Any]:
    """Read-only: whether *task_id* is a completed child nothing will deliver.

    The outcome vocabulary matches ``open_for_epic``'s so one caller can log it
    and the sweep can print it unchanged: ``stranded`` is the only outcome that
    means "open it a pull request".
    """
    _, diagnosis = await _diagnose_snapshot_on(conn, task_id)
    return diagnosis


class StrandedChildPullRequestService:
    """Open one pull request to the default branch for a stranded child."""

    def __init__(self, db: Any, *, git_manager: Any, clock=time.time) -> None:
        self._db = db
        self._git = git_manager
        self._clock = clock

    async def open_for_child(self, task_id: str) -> dict[str, Any]:
        async with self._db.immediate() as conn:
            snapshot, diagnosis = await _diagnose_snapshot_on(conn, task_id)
            if diagnosis["outcome"] != "stranded":
                return {
                    "outcome": diagnosis["outcome"],
                    "task_id": task_id,
                    "reason": diagnosis.get("reason"),
                    "pr_url": diagnosis.get("pr_url"),
                }
            head, branch = snapshot["head_sha"], snapshot["branch"]
            container = tasks.alias("stranded_child_container")
            repository = (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == snapshot["integration_repository_id"],
                        repos.c.project_id == snapshot["project_id"],
                        repos.c.id == snapshot["repository_id"],
                    )
                )
            ).mappings().one_or_none()
            if repository is None or not repository["url"]:
                return {
                    "outcome": "repository_missing",
                    "task_id": task_id,
                    "reason": diagnosis["reason"],
                }
            title, parent_title = (
                await conn.execute(
                    select(tasks.c.title, container.c.title)
                    .select_from(
                        tasks.join(
                            container,
                            container.c.id == snapshot["parent_task_id"],
                            isouter=True,
                        )
                    )
                    .where(tasks.c.id == task_id)
                )
            ).one()
            body = render_stranded_child_body(
                task_id=task_id,
                title=title,
                parent_id=snapshot["parent_task_id"],
                parent_title=parent_title or "(archived)",
                reason=diagnosis["reason"],
            )

        binding = await self._git.bind_github_repository(repository["url"])
        remote_head = await self._git.aremote_branch_head(repository=binding, branch=branch)
        if remote_head is None:
            return {
                "outcome": "branch_not_published",
                "task_id": task_id,
                "reason": diagnosis["reason"],
                "branch": branch,
            }
        if remote_head != head:
            # An unreviewed head must never be proposed: the recorded
            # checkpoint and the published tip have to be the same commit.
            return {
                "outcome": "branch_moved",
                "task_id": task_id,
                "reason": diagnosis["reason"],
                "branch": branch,
                "head_sha": head,
                "remote_head_sha": remote_head,
            }
        ahead = await self._git.acommits_ahead_of_base(
            repository=binding, base=repository["default_branch"], head_sha=head
        )
        if ahead == 0:
            return {
                "outcome": "already_on_default",
                "task_id": task_id,
                "reason": diagnosis["reason"],
                "head_sha": head,
            }
        pr_url = await self._git.acreate_pr(
            repository["source_path"] or repository["checkout_base_path"],
            branch=branch,
            title=title,
            body=body,
            base=repository["default_branch"],
            project_id=snapshot["project_id"],
            repository=binding,
        )
        async with self._db.immediate() as conn:
            await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == task_id,
                    or_(tasks.c.pr_url.is_(None), func.trim(tasks.c.pr_url) == ""),
                )
                .values(pr_url=pr_url)
            )
            stored = (
                await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == task_id))
            ).scalar_one()
        if stored != pr_url:
            return {"outcome": "already_open", "task_id": task_id, "pr_url": stored}
        return {
            "outcome": "opened",
            "task_id": task_id,
            "pr_url": pr_url,
            "branch": branch,
            "head_sha": head,
            "reason": diagnosis["reason"],
        }


async def open_stranded_child_pull_request(
    db: Any, git_manager: Any, task_id: str
) -> dict[str, Any]:
    """Open *task_id*'s own pull request when nothing else will deliver it."""
    return await StrandedChildPullRequestService(db, git_manager=git_manager).open_for_child(
        task_id
    )


def stranded_child_statement(*, completed_before: float, limit: int = 100):
    """COMPLETED tasks with a pushed branch, no pull request and no delivery.

    The literal stall: a completed task whose own branch was pushed, which has
    no ``pr_url``, has no delivery receipt of any kind, and has been that way
    for *completed_before* seconds.

    Three conditions narrow it to real strands, because a line that reports
    every in-flight train is a line nobody reads:

    * the task is a **child** (``parent_task_id`` set).  A root's missing pull
      request is ``unadmitted_parent`` or ``unmaterialized_train_pr``, and both
      of those require ``pr_url IS NOT NULL`` -- so both miss exactly this
      shape, which is why it needed its own line.
    * its head moved past its own origin base and the checkpoint is untouched,
      so the row is not a no-code child or a reworked one still in flight.
    * its container has **no live collector**: it is archived, terminal, or
      PAUSED with a missing episode, missing live operation, a checkpoint that
      is not ``awaiting_children``, or no Subject walking the episode.  A
      container that has not suspended itself yet is excluded -- it may still
      collect.

    Rows carry ``parent_status``, ``parent_state``, ``operation_state`` and
    ``subject_phase`` so the finding can name which part of the path is missing.
    """
    child = tasks
    parent = tasks.alias("stranded_parent")
    archived_parent = archived_tasks.alias("stranded_archived_parent")
    grandchild = tasks.alias("stranded_grandchild")
    checkpoint = task_integration_checkpoints.alias("stranded_checkpoint")
    parent_checkpoint = task_integration_checkpoints.alias("stranded_parent_checkpoint")
    origin = task_branch_origins
    operation = integration_repair_operations.alias("stranded_operation")
    subject = integration_subjects.alias("stranded_subject")
    # "No delivery" means no receipt carries *this* head, matching
    # ``_delivery_receipt_on``: a child reworked onto a newer head after an
    # older delivery is undelivered again.
    delivered = exists(
        select(task_delivery_receipts.c.id).where(
            task_delivery_receipts.c.source_task_id == child.c.id,
            task_delivery_receipts.c.repository_id == child.c.repo_id,
            task_delivery_receipts.c.reviewed_head_sha == checkpoint.c.checkpoint_sha,
        )
    )
    promoting = exists(
        select(integration_promotion_intents.c.id).where(
            integration_promotion_intents.c.source_task_id == child.c.id,
            integration_promotion_intents.c.source_head == checkpoint.c.checkpoint_sha,
            integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
        )
    )
    nested = exists(select(grandchild.c.id).where(grandchild.c.parent_task_id == child.c.id))
    # No collector will carry this child: the container is gone or terminal, or
    # it is PAUSED and one of the conditions ``delivery_refusal_on`` requires
    # is missing.  Three OR'd branches rather than one negated AND so a NULL
    # ``parent.status`` (an archived container) still selects.
    dead_parent = or_(
        archived_parent.c.id.is_not(None),
        parent.c.status.in_(_TERMINAL_STATUSES),
        and_(
            parent.c.status == TaskStatus.PAUSED.value,
            or_(
                parent_checkpoint.c.task_id.is_(None),
                parent_checkpoint.c.episode_id.is_(None),
                parent_checkpoint.c.state != "awaiting_children",
                operation.c.id.is_(None),
                subject.c.phase.is_(None),
                subject.c.phase == _DONE_PHASE,
            ),
        ),
    )
    return (
        select(
            child.c.id.label("task_id"),
            child.c.project_id,
            child.c.parent_task_id,
            child.c.branch_name.label("branch"),
            child.c.updated_at,
            checkpoint.c.checkpoint_sha.label("head_sha"),
            origin.c.base_sha,
            parent.c.status.label("parent_status"),
            parent_checkpoint.c.state.label("parent_state"),
            operation.c.state.label("operation_state"),
            subject.c.phase.label("subject_phase"),
        )
        .select_from(
            child.join(projects, projects.c.id == child.c.project_id)
            .join(
                checkpoint,
                and_(
                    checkpoint.c.task_id == child.c.id,
                    checkpoint.c.repository_id == child.c.repo_id,
                    checkpoint.c.branch == child.c.branch_name,
                ),
            )
            .join(
                origin,
                and_(
                    origin.c.task_id == child.c.id,
                    origin.c.repository_id == child.c.repo_id,
                    origin.c.retired_at.is_(None),
                ),
            )
            .outerjoin(parent, parent.c.id == child.c.parent_task_id)
            .outerjoin(archived_parent, archived_parent.c.id == child.c.parent_task_id)
            .outerjoin(parent_checkpoint, parent_checkpoint.c.task_id == child.c.parent_task_id)
            .outerjoin(
                operation,
                and_(
                    operation.c.parent_task_id == child.c.parent_task_id,
                    operation.c.episode_id == parent_checkpoint.c.episode_id,
                    operation.c.state.in_(_LIVE_OPERATION_STATES),
                ),
            )
            .outerjoin(subject, subject.c.parent_episode_id == parent_checkpoint.c.episode_id)
        )
        .where(
            projects.c.hierarchical_integration_mode.in_(MANAGED_MODES),
            projects.c.integration_repository_id == child.c.repo_id,
            child.c.parent_task_id.is_not(None),
            child.c.status == TaskStatus.COMPLETED.value,
            or_(child.c.pr_url.is_(None), func.trim(child.c.pr_url) == ""),
            child.c.branch_name.is_not(None),
            checkpoint.c.checkpoint_sha.is_not(None),
            checkpoint.c.checkpoint_sha != origin.c.base_sha,
            child.c.updated_at < completed_before,
            checkpoint.c.updated_at < completed_before,
            ~nested,
            ~delivered,
            ~promoting,
            dead_parent,
        )
        .order_by(child.c.updated_at, child.c.id)
        .limit(limit)
    )