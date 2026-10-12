"""Archived task operations."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import and_, delete, exists, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    agents,
    archived_tasks,
    projects,
    sessions,
    task_comments,
    task_completion_records,
    task_metadata,
    task_subtasks,
    tasks,
)
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: The statuses ``archive_task`` treats as "this task will not run again".
TERMINAL_STATUSES = (
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.BLOCKED.value,
)

#: Metadata key holding a development repair's source manifest, written by
#: :meth:`src.integration.development.DevelopmentPrimitives.ensure_repair`.
DEVELOPMENT_REPAIR_SOURCES_KEY = "development_repair_sources"


#: ``task_metadata`` key holding why the sweep last refused to archive a root.
#: ``{"code", "detail", "at"}``.  Written by the sweep itself, from the refusal
#: it actually hit, so no reader has to re-derive the archive path's rules.
ARCHIVE_REFUSAL_KEY = "archive_refusal"

#: Longest one-line detail kept in a log line or a refusal record.
MAX_REFUSAL_DETAIL = 200


def _one_line(text: str, limit: int = MAX_REFUSAL_DETAIL) -> str:
    """First line of *text*, truncated — a refusal detail is never multi-line.

    A driver message carries the failing SQL and its parameters on later
    lines; at WARNING, once an hour, per root, that is a wall of text for one
    fact.
    """
    first = (text or "").strip().splitlines()
    head = first[0].strip() if first else ""
    return head if len(head) <= limit else head[: limit - 1] + "…"


def _constraint_name(exc: BaseException) -> str | None:
    """The constraint asyncpg reports, wherever SQLAlchemy has buried it.

    SQLAlchemy wraps the driver error twice: ``IntegrityError.orig`` is the
    asyncpg *adapter*'s exception, and only its ``__cause__`` is the real
    ``asyncpg.exceptions.ForeignKeyViolationError`` that carries
    ``constraint_name``.  Reading ``exc.orig.constraint_name`` therefore
    always returned ``None``, which is why the log line fell back to the full
    message.  Walk the whole wrapping chain instead.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if name:
            return str(name)
        for link in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(link, BaseException):
                pending.append(link)
    return None


def _failure_signature(exc: BaseException) -> str:
    """``IntegrityError(fk_…)`` — the exception class plus, where the driver
    gives one, the constraint that refused.  One line always: specific enough
    to name the offending table in a bug report, short enough to log."""
    name = type(exc).__name__
    constraint = _constraint_name(exc)
    return f"{name}({constraint})" if constraint else _one_line(f"{name}: {exc}")


@dataclass(frozen=True)
class ArchiveBlockedRoots:
    """Roots the sweep could not archive: the true count, and a capped page.

    ``total`` is every still-eligible root carrying a refusal record, not
    ``len(roots)`` — the list is capped so a surface can render it, and a
    count that saturated at the cap would understate the backlog it exists to
    report.
    """

    total: int
    roots: list[dict]


class ArchiveQueryMixin:
    """Query mixin for archived task operations.  Expects ``self._engine``."""

    #: Git delivery truth for the archive guard, registered by the daemon
    #: (:meth:`set_delivery_observer`).  Without one, development work that
    #: needs proof is unverified and every archive of it is refused.
    _delivery_observer = None

    #: Git-first sibling prerequisite evidence (a truth-bearing observer),
    #: registered by the daemon when ``integration.git_first`` is active.
    _prerequisite_observer = None

    def set_prerequisite_observer(self, observer) -> None:
        """Register the observer whose Git view proves sibling prerequisites."""
        self._prerequisite_observer = observer

    def set_delivery_observer(self, observer) -> None:
        """Register the :class:`~src.integration.delivery_observer.DeliveryObserver`."""
        self._delivery_observer = observer

    async def observe_removal_delivery(self, root_ids):
        """Git evidence for the subtrees of *root_ids*, taken before any transaction.

        Only the development work that needs proof is fetched and evaluated,
        once per repository however many roots a sweep archives.  A target
        that keeps moving yields no view: the guard then holds that work as
        unknown rather than trusting a stale answer.
        """
        from src.integration.delivery_observer import delivery_sensitive_ids

        observer = self._delivery_observer
        if observer is None:
            return None
        sensitive: set[str] = set()
        async with self._engine.connect() as conn:
            for root_id in dict.fromkeys(root_ids):
                sensitive |= await delivery_sensitive_ids(
                    conn, await self.subtree_ids(root_id, conn=conn)
                )
        if not sensitive:
            return None
        view = await observer.observe(sensitive)
        return view if await view.fresh() else None

    async def archive_task(
        self,
        task_id: str,
        *,
        hold_undelivered: bool = False,
        abandon_undelivered: bool = False,
        abandon_reason: str | None = None,
        abandoned_by: str = "operator",
        delivery=None,
        obsolete_integration_delegate: bool = False,
        archive_reason: str | None = None,
        disposition: str | None = None,
    ) -> bool:
        """Archive *task_id* and its whole subtree atomically (spec §7).

        Every archive protects undelivered development work. ``hold_undelivered``
        remains accepted for old sweep callers but is now redundant. A human or
        elevated supervisor can intentionally bypass only that delivery rule
        with ``abandon_undelivered`` and a durable reason.

        Delivery is proven in git before the transaction (*delivery*, or the
        registered observer's view of the subtree) and each identity is
        rechecked inside it, so a task reopened or re-completed meanwhile is
        held rather than archived on an old answer.

        Refuses live sessions; DEFINED/READY/PAUSED tasks require ``archive_reason``.
        Deepest first, root last, so the subtree moves together. *disposition*
        (``obsolete`` or ``retire``) is the operator's recorded decision for the
        abandoned work; it is written on the task and on each branch retirement.
        """
        if abandon_undelivered and not (abandon_reason or "").strip():
            raise ValueError("abandon_undelivered requires a reason")
        if delivery is None:
            delivery = await self.observe_removal_delivery([task_id])
        async with self.immediate() as conn:
            outcome = await self._archive_task_on(
                task_id, conn=conn, abandon_undelivered=abandon_undelivered,
                abandon_reason=abandon_reason, abandoned_by=abandoned_by,
                delivery=delivery, obsolete_integration_delegate=obsolete_integration_delegate,
                archive_reason=archive_reason, disposition=disposition,
            )
        if outcome is None:
            return False
        flipped, ready, settle_result = outcome
        await self.log_blocked_flips(flipped | settle_result.flipped)
        await self._notify_settled(settle_result.settled)
        await self._notify_ready(ready + list(settle_result.ready))
        return True

    async def undelivered_holders(self, task_id, *, delivery=None, conn=None):
        """The subtree's completed work that has not reached the default branch.

        Returns ``(default_branch, holders)``: the rows the archive guard refuses
        with ``integration_undelivered``, each ``{"task_id", "holder", "detail"}``.
        """
        if delivery is None and conn is None:
            delivery = await self.observe_removal_delivery([task_id])
        if conn is None:
            async with self._engine.connect() as read:
                return await self.undelivered_holders(task_id, delivery=delivery, conn=read)
        from src.integration.removal_guard import undelivered_removal_holders

        row = (await conn.execute(
            select(projects.c.id, projects.c.hierarchical_integration_mode)
            .select_from(projects.join(tasks, tasks.c.project_id == projects.c.id))
            .where(tasks.c.id == task_id)
        )).first()
        if row is None:
            return "default branch", []
        project_id, mode = row
        return await undelivered_removal_holders(
            conn, root_id=task_id, ids=await self.subtree_ids(task_id, conn=conn),
            project_id=project_id, mode=mode, delivery=delivery,
        )

    async def _archive_task_on(
        self, task_id, *, conn, abandon_undelivered=False, abandon_reason=None,
        abandoned_by="operator", delivery=None, obsolete_integration_delegate=False,
        archive_reason=None, defer_projection=False, disposition=None,
    ):
        from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES, HierarchyError

        terminal = set(TERMINAL_STATUSES)
        if archive_reason and archive_reason.strip():
            terminal.update({"DEFINED", "READY", "PAUSED"})
        # Admission still protects live and undelivered work. An admitted
        # archive now queues audited branch retirement before removing rows.
        await self.guard_integration_mutation(
            task_id,
            "archive",
            conn=conn,
            retire_pending=True,
            branch_policy="keep",
            abandon_undelivered=abandon_undelivered,
            delivery=delivery,
            obsolete_integration_delegate=obsolete_integration_delegate,
        )
        if obsolete_integration_delegate:
            from src.integration.delegate_release import assert_obsolete_delegate_on

            proof = await assert_obsolete_delegate_on(self, conn, task_id)
            project_id = (await conn.execute(select(tasks.c.project_id).where(
                tasks.c.id == task_id,
            ))).scalar_one()
            snapshot = json.dumps(proof, sort_keys=True)
            # Comments are capped at 16k characters; retain the complete
            # snapshot even when a legacy dossier or metadata is larger.
            chunks = [snapshot[i:i + 15000] for i in range(0, len(snapshot), 15000)]
            for index, chunk in enumerate(chunks, start=1):
                await conn.execute(insert(task_comments).values(
                    id="comment-" + uuid.uuid4().hex, task_id=task_id, project_id=project_id,
                    author_kind="agent", author_id="integration-reconciliation", kind="note",
                    body="Obsolete integration delegate archived; audited branch retirement "
                         f"queued and stage history preserved. Retirement proof "
                         f"{index}/{len(chunks)}: " + chunk,
                    created_at=time.time(),
                ))
        ids = await self.subtree_ids(task_id, conn=conn)
        if not ids:
            return None
        # Read once: the integration guards below scope their reads to this
        # project, and the layout mark needs it before the rows leave
        # ``tasks``.  A task never changes project.
        project_id = (
            await conn.execute(select(tasks.c.project_id).where(tasks.c.id == task_id))
        ).scalar_one_or_none()
        retirement_reason = "task archived"
        if abandon_undelivered:
            _branch, abandoned = await self.undelivered_holders(
                task_id, delivery=delivery, conn=conn,
            )
            named_holders = ", ".join(
                f"{row['task_id']} ({row['holder']})" for row in abandoned
            ) or "none pending at archive time"
            decided = ""
            if disposition:
                retirement_reason = f"task archived ({disposition}): {abandon_reason.strip()}"
                decided = (
                    f"Disposition: {disposition}; its branches are retired after a backup "
                    "bundle.\n"
                )
            await conn.execute(
                pg_insert(task_comments).values(
                    id="comment-" + uuid.uuid4().hex,
                    task_id=task_id,
                    project_id=project_id,
                    body=(
                        "Delivery abandoned before archive by "
                        f"{abandoned_by}: {abandon_reason.strip()}\n"
                        f"{decided}"
                        f"Affected delivery holders: {named_holders}\n"
                        f"Affected subtree: {', '.join(sorted(ids))}"
                    ),
                    author_kind="supervisor",
                    author_id=abandoned_by,
                    kind="note",
                    created_at=time.time(),
                )
            )
        if archive_reason:
            await conn.execute(insert(task_comments).values(
                id="comment-" + uuid.uuid4().hex, task_id=task_id, project_id=project_id,
                author_kind="supervisor" if abandoned_by.startswith("supervisor") else "user",
                author_id=abandoned_by, kind="note",
                body=f"Subtree archived by {abandoned_by}: {archive_reason.strip()}",
                created_at=time.time(),
            ))
        # Follow the existing sessions-before-tasks lock order. A task
        # can be terminal while its worker is still draining.
        live = await self.live_descendant_sessions(task_id, conn=conn)
        if live:
            raise HierarchyError("live_descendants", ", ".join(sorted(t for _, t in live)))
        task_rows = select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids))
        task_rows = task_rows.order_by(tasks.c.id).with_for_update()
        rows = (await conn.execute(task_rows)).fetchall()
        live_ids = (
            (
                await conn.execute(
                    select(sessions.c.task_id).where(
                        sessions.c.task_id.in_(ids),
                        sessions.c.state.in_(LIVE_SESSION_STATES),
                    )
                )
            )
            .scalars()
            .all()
        )
        if live_ids:
            raise HierarchyError("live_descendants", ", ".join(sorted(set(live_ids))))
        if any(r[0] == task_id and r[1] not in terminal for r in rows):
            raise HierarchyError("non_terminal_root", task_id)
        open_ids = [r[0] for r in rows if r[1] not in terminal and r[0] != task_id]
        if open_ids:
            raise HierarchyError("open_descendants", ", ".join(sorted(open_ids)))
        parent = (
            await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
        ).scalar()
        affected = await self._collect_affected(set(ids), conn)
        affected -= set(ids)
        if parent:
            affected.add(parent)
        # Archiving moves the rows out of ``tasks``, so mark while the
        # project id is still readable there; ``_archive_one`` reuses
        # ``_delete_one``, which drops the layout rows (an FK holder on
        # ``tasks``) inside this same transaction.  The surviving PARENTS
        # are marked too: with the archived rows gone the driver cannot
        # find the former container from a stored row, so without this
        # the container never re-flows and its ancestors' aggregates go
        # stale.
        if project_id is not None:
            await self.mark_layout_dirty(
                project_id,
                [*ids, *await self._layout_parent_ids(ids, conn=conn)],
                "task.archived",
                conn=conn,
            )
        for tid in reversed(ids):
            task = await self._get_task_conn(tid, conn=conn)
            if task is not None:
                await self._archive_one(task, conn=conn, retirement_reason=retirement_reason)
        if defer_projection:
            # The change-set caller retains the old dependents/parent and
            # performs their projection and stale-claim release as one pass.
            from src.database.queries.task_queries import TransitionResult
            return set(), [], TransitionResult()
        flipped = await self.recompute_blocked(affected, conn=conn) if affected else set()
        # Archiving a blocker unblocks its dependents exactly as
        # completing it would, so the same ``task.ready`` audit row and
        # listener wake-up are owed (I2) — without them a waiting
        # ``task_claim`` long-poll sleeps through claimable work.
        ready = [
            (tid, "unblocked")
            for tid in await self._note_frontier_entry(conn, flipped, reason="unblocked")
        ]
        # Archiving empties a container exactly as deleting a child does, and
        # strands its parent the same way, so the same proved release of a
        # stopped holder's claim runs here (sharp-ridge-57).
        if parent:
            stale = await self.release_stale_container_claim(parent, conn=conn)
            flipped |= stale.flipped
            ready.extend(stale.ready)
        settle_result = await self.settle_containers({parent} if parent else set(), conn=conn)
        return flipped, ready, settle_result

    async def _development_integration_hold(self, ids, project_id, *, conn) -> str | None:
        """Say why development delivery still owns one of *ids*, or ``None``.

        Development mode keeps its claim on a source task in two places, and
        neither is a foreign key onto ``tasks``: an unfinished
        publisher operation manifest, and the source manifest an open
        repair task carries in its metadata.  Archiving a task named by either
        leaves the publisher holding an id that no longer resolves — the batch
        can no longer explain what it is publishing, and the repair can no
        longer show where its work came from.  Both are reported as
        ``integration_owned``, naming the batch or repair that holds the task
        so an operator can see what to settle first.

        A repair that has reached a terminal status is not open: it will not
        consult its sources again, and if the batch that spawned it is still
        unresolved the manifest check above already holds those sources.
        """
        from src.integration.development import OPEN_OPERATION_STATES, operation_rows_on

        wanted = set(ids)
        # Outstanding legacy journal rows were retained as ``legacy-operation:``
        # events by the development_deliveries retirement revision.
        batches = await operation_rows_on(conn, [project_id], states=OPEN_OPERATION_STATES)
        for row in batches:
            named = self._named_task_ids(row["manifest"], wanted)
            if named:
                return (
                    f"development batch {row['id']} ({row['state']}) lists {', '.join(named)}"
                )
        # The join to ``tasks`` is what bounds this read: only a repair still
        # in the queue, and still able to run, can hold a source.
        repair_task = tasks.alias("development_repair_task")
        repairs = (
            await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.value)
                .select_from(
                    task_metadata.join(
                        repair_task, repair_task.c.id == task_metadata.c.task_id
                    )
                )
                .where(task_metadata.c.key == DEVELOPMENT_REPAIR_SOURCES_KEY)
                .where(repair_task.c.project_id == project_id)
                .where(repair_task.c.status.notin_(TERMINAL_STATUSES))
                .order_by(task_metadata.c.task_id)
            )
        ).all()
        for repair_id, raw in repairs:
            try:
                manifest = json.loads(raw)
            except (TypeError, ValueError):
                continue
            named = self._named_task_ids(manifest, wanted)
            if named:
                return (
                    f"open development repair {repair_id} lists "
                    f"{', '.join(named)} as a source"
                )
        return None

    @staticmethod
    def _named_task_ids(manifest, wanted: set[str]) -> list[str]:
        """Return the members of *manifest* that name a task in *wanted*.

        A manifest is written by the publisher, but it is stored as free JSON
        and old rows predate later shape changes, so read it defensively.
        """
        if not isinstance(manifest, list):
            return []
        return sorted(
            {
                member["task_id"]
                for member in manifest
                if isinstance(member, dict) and member.get("task_id") in wanted
            }
        )

    async def _archive_one(self, task, *, conn, retirement_reason="task archived") -> None:
        """Move a single task row from ``tasks`` into ``archived_tasks``."""
        task_id = task.id
        # Serialize archive with accepted description/comment writes, then
        # snapshot the current task rather than the earlier unlocked read.
        locked = await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(
                updated_at=tasks.c.updated_at,
            )
        )
        if locked.rowcount != 1:
            return
        task = await self._get_task_conn(task_id, conn=conn)
        now = time.time()
        from src.integration.branch_retirement import request_task_retirement_on

        await request_task_retirement_on(
            conn, task_id, request_id=f"archive:{task_id}:{now}", reason=retirement_reason, now=now,
        )
        # Insert into archive (skip if already archived).
        # on_conflict_do_nothing requires dialect-specific insert.
        _insert = pg_insert
        await conn.execute(
            _insert(archived_tasks)
            .on_conflict_do_nothing()
            .values(
                id=task.id,
                project_id=task.project_id,
                parent_task_id=task.parent_task_id,
                repo_id=task.repo_id,
                title=task.title,
                description=task.description,
                priority=task.priority,
                status=task.status.value,
                verification_type=task.verification_type.value,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                assigned_agent_id=task.assigned_agent_id,
                branch_name=task.branch_name,
                resume_after=task.resume_after,
                integration_mode=task.integration_mode,
                pr_url=task.pr_url,
                plan_source=task.plan_source,
                is_plan_subtask=int(task.is_plan_subtask),
                task_type=task.task_type.value if task.task_type else None,
                profile_id=task.profile_id,
                intelligence_class=task.intelligence_class,
                preferred_workspace_id=task.preferred_workspace_id,
                attachments=json.dumps(task.attachments) if task.attachments else "[]",
                skip_verification=int(task.skip_verification),
                workflow_id=task.workflow_id,
                affinity_agent_id=task.affinity_agent_id,
                affinity_reason=task.affinity_reason,
                workspace_mode=(task.workspace_mode.value if task.workspace_mode else None),
                # Carry the blocked-state projection across so archiving
                # really is lossless (work-graph §2.2).
                is_blocked=int(task.is_blocked),
                dedup_key=task.dedup_key,
                created_by_kind=task.created_by_kind,
                created_by_id=task.created_by_id,
                provider_intent=task.provider_intent or "class_only",
                rerouted_from=task.rerouted_from,
                route=task.route,
                created_at=0.0,
                updated_at=0.0,
                archived_at=now,
            )
        )

        # Legacy allocation could reuse an archived ID in another project.
        # Never drop the active identity or overwrite that archive's timestamps.
        archived_project_id = (
            await conn.execute(
                select(archived_tasks.c.project_id).where(archived_tasks.c.id == task_id)
            )
        ).scalar_one()
        if archived_project_id != task.project_id:
            from src.database.queries.hierarchy_queries import HierarchyError

            raise HierarchyError("archive_identity_conflict", task_id)

        # Copy original timestamps
        result = await conn.execute(
            select(tasks.c.created_at, tasks.c.updated_at, tasks.c.legacy_completion_id)
            .where(tasks.c.id == task_id)
        )
        row = result.fetchone()
        if row:
            await conn.execute(
                update(archived_tasks)
                .where(archived_tasks.c.id == task_id)
                .values(created_at=row[0], updated_at=row[1], legacy_completion_id=row[2])
            )

        # Use the same FK cleanup as permanent deletion, but keep comments
        # and subtasks attached to the archived task identity and preserve
        # session history.
        await conn.execute(
            update(agents).where(agents.c.current_task_id == task_id).values(current_task_id=None)
        )
        await self._delete_one(
            task_id,
            conn=conn,
            preserve_comments=True,
            preserve_completion=True,
            preserve_subtasks=True,
            gate_resolution="last waiter task archived",
        )

    async def archive_completed_tasks(
        self,
        project_id: str | None = None,
    ) -> list[str]:
        """Archive all COMPLETED tasks. Returns list of archived task IDs.

        A COMPLETED task can still have a non-terminal *descendant* (a
        grandchild left open under a completed child).  ``archive_task``
        refuses those with ``hierarchy.open_descendants``; like
        ``archive_old_terminal_tasks``, the bulk path skips them rather than
        aborting the whole sweep — and reports only what it actually archived.

        This is the explicit bulk command, not the hourly sweep, so it writes
        no ``archive_refusal`` record: the report is about what the *automatic*
        pass keeps failing to do.  Anything added here that does record must go
        through ``_note_archive_refusal`` / ``_forget_archive_refusal``, never
        the raw helpers — a failed bookkeeping write may not end the loop.
        """
        from src.database.queries.hierarchy_queries import HierarchyError

        stmt = select(tasks.c.id).where(tasks.c.status == TaskStatus.COMPLETED.value)
        if project_id:
            stmt = stmt.where(tasks.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            task_ids = [r[0] for r in result.fetchall()]

        archived: list[str] = []
        delivery = await self.observe_removal_delivery(task_ids)
        for tid in task_ids:
            try:
                await self.archive_task(tid, hold_undelivered=True, delivery=delivery)
                archived.append(tid)
            except HierarchyError as exc:
                logger.debug("archive_completed_tasks: skipping %s, %s", tid, exc.code)
            except Exception as exc:  # noqa: BLE001 — one bad root may not stop the rest
                logger.warning(
                    "archive_completed_tasks: skipping %s, %s", tid, _failure_signature(exc)
                )

        return archived

    async def archive_old_terminal_tasks(
        self,
        statuses: list[str],
        older_than_seconds: float,
    ) -> list[str]:
        """Archive terminal tasks older than the threshold. Returns archived IDs.

        Selects only subtree roots of terminal subtrees — terminal, older
        than cutoff, ``parent_task_id IS NULL``, and with no non-terminal
        direct child. Children are archived by the root's own subtree
        archive, not selected individually. Open grandchildren are caught
        by ``archive_task``'s own subtree check, which raises; those roots
        are logged and skipped.

        **One bad root never stops the sweep.**  Before 2026-09-19 only
        ``HierarchyError`` was caught here, so the first root an
        ``IntegrityError`` escaped from aborted the whole hourly pass — on
        the operator's install that happened 225 times in a row and nothing
        was ever archived.  Every exception is now per root: recorded, logged,
        and the loop continues.

        **What it skipped is recorded where a reader can find it.**  Each
        skipped root gets the refusal the sweep *actually* hit — code, a
        one-line detail and a timestamp — in ``task_metadata`` under
        ``archive_refusal``.  Nothing else derives those conditions a second
        time: ``sealed`` and ``delivery_target_fixed`` come from deep inside
        ``guard_integration_mutation`` and a report that re-implemented the
        rules would keep missing them.  The record is written only when the
        *code* changes, so an unarchivable root costs no write an hour, and it
        never touches ``tasks.updated_at`` — that column is the eligibility
        cutoff, and moving it would make a blocked root flicker in and out of
        the sweep's own candidate set.
        """
        from src.database.queries.hierarchy_queries import HierarchyError

        if not statuses:
            return []

        cutoff = time.time() - older_than_seconds
        async with self._engine.begin() as conn:
            result = await conn.execute(self._eligible_archive_roots_stmt(statuses, cutoff))
            task_ids = [r[0] for r in result.fetchall()]

        archived: list[str] = []
        # A whole sweep failing the same way is one fact, not N log lines.
        unexpected: Counter[str] = Counter()
        unexpected_ids: dict[str, list[str]] = {}
        # One git observation for the whole sweep; each archive rechecks the
        # identities it covers under its own locks.
        delivery = await self.observe_removal_delivery(task_ids)
        for tid in task_ids:
            try:
                await self.archive_task(tid, hold_undelivered=True, delivery=delivery)
            except HierarchyError as exc:
                await self._note_archive_refusal(tid, exc.code, _one_line(exc.detail or exc.code))
                logger.debug("archive_old_terminal_tasks: skipping %s, %s", tid, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001 — one bad root may not stop the rest
                signature = _failure_signature(exc)
                await self._note_archive_refusal(tid, "unexpected", signature)
                unexpected[signature] += 1
                unexpected_ids.setdefault(signature, []).append(tid)
                continue
            archived.append(tid)
            # The archive took the task's metadata with it; clear explicitly
            # so the record's lifetime does not depend on that coincidence.
            await self._forget_archive_refusal(tid)

        for signature, count in unexpected.most_common():
            shown = unexpected_ids[signature][:5]
            logger.warning(
                "archive_old_terminal_tasks: skipped %d root(s) on %s: %s%s",
                count,
                signature,
                ", ".join(shown),
                "..." if count > len(shown) else "",
            )
        # A root can become ineligible (reopened, edited, or gain an open
        # descendant) between sweeps. Its old refusal is no longer a fact the
        # operator should see, so remove it rather than merely filtering it
        # from the report forever.
        await self._clear_ineligible_archive_refusals(statuses, cutoff)
        return archived

    async def _clear_ineligible_archive_refusals(self, statuses: list[str], cutoff: float) -> None:
        eligible = self._eligible_archive_roots_stmt(statuses, cutoff).subquery()
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.key == ARCHIVE_REFUSAL_KEY,
                    ~exists(
                        select(literal(1)).where(eligible.c.id == task_metadata.c.task_id)
                    ),
                )
            )

    async def _note_archive_refusal(self, task_id: str, code: str, detail: str) -> None:
        """:meth:`_record_archive_refusal`, but it can never stop the sweep.

        Bookkeeping about a skipped root is strictly less important than
        archiving the roots that are fine.  An exception escaping here would
        abandon every remaining root in the hourly pass — the very failure
        mode this sweep exists to have removed, arriving through the code that
        reports it.  One WARNING and carry on; the root's archive outcome was
        decided above and is unaffected.
        """
        try:
            await self._record_archive_refusal(task_id, code, detail)
        except Exception as exc:  # noqa: BLE001 — reporting may not break the sweep
            logger.warning(
                "archive_old_terminal_tasks: could not record why %s was skipped, %s",
                task_id,
                _failure_signature(exc),
            )

    async def _forget_archive_refusal(self, task_id: str) -> None:
        """:meth:`_clear_archive_refusal`, but it can never stop the sweep.

        The task has already been archived when this runs; a failed tidy-up
        leaves a stale record that the eligibility filter in
        :meth:`list_archive_blocked_roots` hides anyway.
        """
        try:
            await self._clear_archive_refusal(task_id)
        except Exception as exc:  # noqa: BLE001 — reporting may not break the sweep
            logger.warning(
                "archive_old_terminal_tasks: could not clear the refusal record for %s, %s",
                task_id,
                _failure_signature(exc),
            )

    async def _record_archive_refusal(self, task_id: str, code: str, detail: str) -> None:
        """Remember why the sweep last refused *task_id*, if that has changed.

        Writes only ``task_metadata`` — never ``tasks`` — so the eligibility
        cutoff (``tasks.updated_at``) is untouched. Both the code and detail
        are compared: a narrower post-upgrade refusal must replace a stale
        blanket ``integration_owned`` record while an unchanged refusal costs
        no hourly write.
        """
        async with self._engine.begin() as conn:
            stored = (
                await conn.execute(
                    select(task_metadata.c.value).where(
                        and_(
                            task_metadata.c.task_id == task_id,
                            task_metadata.c.key == ARCHIVE_REFUSAL_KEY,
                        )
                    )
                )
            ).scalar_one_or_none()
            if stored is not None:
                try:
                    existing = json.loads(stored)
                    if existing.get("code") == code and existing.get("detail") == detail:
                        return
                except (TypeError, ValueError):
                    pass
            await self._upsert_meta(
                task_id,
                ARCHIVE_REFUSAL_KEY,
                {"code": code, "detail": detail, "at": time.time()},
                conn=conn,
            )

    async def _clear_archive_refusal(self, task_id: str) -> None:
        """Drop *task_id*'s refusal record — it is no longer refused."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_metadata).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == ARCHIVE_REFUSAL_KEY,
                    )
                )
            )

    @staticmethod
    def _eligible_archive_roots_stmt(statuses: list[str], cutoff: float):
        """Terminal subtree roots older than *cutoff* — the sweep's candidates."""
        child = tasks.alias("child")
        return select(tasks.c.id).where(
            and_(
                tasks.c.status.in_(statuses),
                # Reusable project triage keeps one identity and its run history.
                ~and_(
                    func.coalesce(tasks.c.profile_id, "") == "triage",
                    func.coalesce(tasks.c.dedup_key, "") == "triage-open",
                ),
                tasks.c.updated_at <= cutoff,
                tasks.c.parent_task_id.is_(None),
                ~exists(
                    select(literal(1)).where(
                        and_(
                            child.c.parent_task_id == tasks.c.id,
                            child.c.status.notin_(
                                (
                                    TaskStatus.COMPLETED.value,
                                    TaskStatus.FAILED.value,
                                    TaskStatus.BLOCKED.value,
                                )
                            ),
                        )
                    )
                ),
            )
        )

    async def list_archive_blocked_roots(
        self,
        statuses: list[str],
        older_than_seconds: float,
        limit: int = 50,
    ) -> ArchiveBlockedRoots:
        """Eligible roots the sweep could not archive, and why.  Read-only.

        Reports what ``archive_old_terminal_tasks`` *recorded* — it does not
        re-derive a single refusal condition.  That matters: ``sealed`` and
        ``delivery_target_fixed`` are raised several layers down inside
        ``guard_integration_mutation``, and the earlier version of this
        method, which reimplemented the checks it knew about, silently
        omitted both.  Now every code the archive path can raise shows up,
        including ``unexpected``, with no second copy of the rules to drift.

        Restricted to roots that are *still* eligible, so a record left on a
        task that has since been reopened or edited is never reported.
        ``total`` is the true count; ``roots`` is capped at *limit*.
        """
        if not statuses:
            return ArchiveBlockedRoots(total=0, roots=[])
        cutoff = time.time() - older_than_seconds
        eligible = self._eligible_archive_roots_stmt(statuses, cutoff).subquery()
        recorded = (
            select(task_metadata.c.task_id, task_metadata.c.value)
            .select_from(
                task_metadata.join(eligible, task_metadata.c.task_id == eligible.c.id)
            )
            .where(task_metadata.c.key == ARCHIVE_REFUSAL_KEY)
        )
        async with self._engine.begin() as conn:
            total = (
                await conn.execute(select(func.count()).select_from(recorded.subquery()))
            ).scalar_one()
            if not total:
                return ArchiveBlockedRoots(total=0, roots=[])
            rows = (
                await conn.execute(recorded.order_by(task_metadata.c.task_id).limit(limit))
            ).fetchall()
        roots = []
        for task_id, value in rows:
            try:
                record = json.loads(value)
            except (TypeError, ValueError):
                record = {}
            roots.append(
                {
                    "task_id": task_id,
                    "reason": record.get("code") or "unexpected",
                    "detail": record.get("detail") or "",
                    "since": record.get("at") or 0.0,
                }
            )
        return ArchiveBlockedRoots(total=int(total), roots=roots)

    async def list_archived_tasks(
        self,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return archived tasks as dicts, newest archived first."""
        stmt = select(archived_tasks)
        if project_id:
            stmt = stmt.where(archived_tasks.c.project_id == project_id)
        stmt = stmt.order_by(archived_tasks.c.archived_at.desc()).limit(limit)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_archived_task(r) for r in result.mappings().fetchall()]

    async def get_archived_task(self, task_id: str) -> dict | None:
        """Return a single archived task as a dict, or *None* if not found."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(archived_tasks).where(archived_tasks.c.id == task_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_archived_task(row)

    async def list_archived_tasks_by_dedup_prefix(
        self, project_id: str, prefix: str
    ) -> list[dict]:
        """Archived tasks with a matching dedup key, oldest created first."""
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = (
            select(archived_tasks)
            .where(
                archived_tasks.c.project_id == project_id,
                archived_tasks.c.dedup_key.like(pattern, escape="\\"),
            )
            .order_by(archived_tasks.c.created_at.asc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [self._row_to_archived_task(row) for row in rows]

    async def delete_archived_task(self, task_id: str) -> bool:
        """Permanently delete an archived task. Returns *True* if deleted."""
        from src.database.queries.hierarchy_queries import HierarchyError
        from src.database.queries.task_references import (
            describe_integration_references,
            find_integration_task_references,
        )

        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(archived_tasks.c.project_id).where(archived_tasks.c.id == task_id)
            )
            archived_project_id = result.scalar_one_or_none()
            if archived_project_id is None:
                return False
            found = await find_integration_task_references(conn, [task_id])
            if found:
                raise HierarchyError(
                    "integration_history_retained",
                    f"delete is refused: {len(found)} integration audit record(s) name {task_id} "
                    f"({describe_integration_references(found)}) and audit history is append-only. "
                    "Archive the task instead; its history stays readable by id.",
                    {"references": found},
                )
            await conn.execute(
                delete(task_completion_records).where(
                    task_completion_records.c.task_id == task_id,
                    ~exists(select(tasks.c.id).where(tasks.c.id == task_id)),
                )
            )
            await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == task_id))
            # Restoration may recreate the active identity before removing
            # its archive snapshot. Only permanent deletion removes history.
            await conn.execute(
                delete(task_comments).where(
                    task_comments.c.task_id == task_id,
                    task_comments.c.project_id == archived_project_id,
                    ~exists(
                        select(tasks.c.id).where(
                            tasks.c.id == task_id,
                            tasks.c.project_id == archived_project_id,
                        )
                    ),
                )
            )
            await conn.execute(
                delete(task_subtasks).where(
                    task_subtasks.c.task_id == task_id,
                    task_subtasks.c.project_id == archived_project_id,
                    ~exists(
                        select(tasks.c.id).where(
                            tasks.c.id == task_id,
                            tasks.c.project_id == archived_project_id,
                        )
                    ),
                )
            )
        return True

    async def count_archived_tasks(
        self,
        project_id: str | None = None,
    ) -> int:
        """Return the total count of archived tasks."""
        stmt = select(func.count()).select_from(archived_tasks)
        if project_id:
            stmt = stmt.where(archived_tasks.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return row[0] if row else 0

    @staticmethod
    def _row_to_archived_task(row) -> dict:
        """Convert a database row from ``archived_tasks`` to a plain dict."""
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "parent_task_id": row["parent_task_id"],
            "repo_id": row["repo_id"],
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": row["status"],
            "verification_type": row["verification_type"],
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "assigned_agent_id": row["assigned_agent_id"],
            "branch_name": row["branch_name"],
            "resume_after": row["resume_after"],
            "integration_mode": row["integration_mode"],
            "pr_url": row["pr_url"],
            "plan_source": row.get("plan_source"),
            "is_plan_subtask": bool(row.get("is_plan_subtask", 0)),
            "task_type": row.get("task_type"),
            "profile_id": row.get("profile_id"),
            "intelligence_class": row.get("intelligence_class"),
            "workflow_id": row.get("workflow_id"),
            "affinity_agent_id": row.get("affinity_agent_id"),
            "affinity_reason": row.get("affinity_reason"),
            "workspace_mode": row.get("workspace_mode"),
            "is_blocked": bool(row.get("is_blocked", 0)),
            "dedup_key": row.get("dedup_key"),
            "created_by_kind": row.get("created_by_kind"),
            "created_by_id": row.get("created_by_id"),
            "provider_intent": row.get("provider_intent") or "class_only",
            "rerouted_from": row.get("rerouted_from"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }
