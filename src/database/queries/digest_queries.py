"""Gather the durable evidence one digest window is allowed to consider.

This is the only part of the digest that touches rows.  It answers two
questions and nothing else:

* what *happened* in the window -- completions, attempt starts, comments the
  author explicitly recorded as progress (``task_comments.kind = 'progress'``)
  and PR milestones, each carrying the identity of the row it came from so a
  replay or a late arrival cannot double-count it;
* what is *executing right now* -- tasks with a live, non-stale attempt on a
  running session, excluding anything parked on a human answer.

Beside task work it reads one fleet producer: provider availability changes
between launchable and unavailable (provider-failover D19), from the
append-only ``provider_availability_transitions`` log, as ``system`` facts
keyed ``provider:<key>:<generation>``, and the re-route batches that moved
work off an unavailable provider, from ``task_reroutes``, keyed
``reroute:<batch_id>``.  An outage in a quiet hour is exactly what the digest
is for, so such a fact makes a window eligible on its own.

An ordinary comment is *not* evidence of progress.  Questions, plans, status
requests and chatter share the comments table with real milestones, so the
digest reads the durable ``kind`` marker rather than guessing from prose: an
unlabelled comment can neither make a window eligible nor appear as a
highlight.

``list_recent_task_activity`` deliberately is not reused: its definition of
"touched" includes ``updated_at`` churn, which §8 forbids as an eligibility
signal.  A layout rebuild, a heartbeat or a metrics tick all bump
``updated_at``; none of them is work.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.task_session_queries import live_attempt_predicate
from src.database.tables import (
    agent_questions,
    archived_tasks,
    digest_windows,
    doc_reviews,
    escalations,
    projects,
    provider_availability_transitions,
    sessions,
    supervisor_report_requests,
    task_comments,
    task_completion_records,
    task_reroutes,
    task_session_attempts,
    tasks,
)
from src.digest.facts import (
    KIND_COMPLETED,
    KIND_PROGRESS,
    KIND_PROVIDER,
    KIND_REROUTE,
    KIND_STARTED,
    ActiveTask,
    DigestInputs,
    DigestWindow,
    WorkFact,
)

#: Statuses that mean "this task is waiting, not working".  A task in one of
#: these produced no fact and is never active; it is counted as idle so the
#: preview can say "quiet" rather than "nothing exists".
_IDLE_STATUSES = (
    "DEFINED",
    "READY",
    "ASSIGNED",
    "IN_PROGRESS",
    "WAITING_INPUT",
    "PAUSED",
    "BLOCKED",
)

#: Question states that mean a worker is parked on a human.  Waiting for an
#: answer is not healthy execution, however recently the session spoke.
_WAITING_QUESTION_STATES = ("supervisor", "human")

#: How long a running session may be silent before its attempt stops counting
#: as live.  Matches the default session lease TTL; callers pass the
#: configured value.
DEFAULT_STALE_AFTER = 480.0

#: The one ``task_comments.kind`` value that counts as recorded progress.
PROGRESS_COMMENT_KIND = "progress"


def _first_line(text: str, limit: int = 160) -> str:
    """A summary's first sentence-ish line, never a wall of log output."""
    line = (text or "").strip().split("\n", 1)[0].strip()
    return line[:limit]


def provider_fact(row: Mapping[str, Any]) -> WorkFact | None:
    """The digest fact for one provider transition, or ``None`` when it is not news.

    Only a change of *half* is (D19): ``unauthenticated`` becoming
    ``exhausted`` is still one outage, and ``available`` becoming
    ``degraded`` never stopped a launch.  The key is the transition's
    ``(provider, generation)`` -- generations only grow, so a provider's next
    outage is a new fact however alike its wording.
    """
    from src.providers.availability import half

    from_state, to_state = str(row["from_state"]), str(row["to_state"])
    if half(from_state) == half(to_state):
        return None
    provider = str(row["provider"])
    reason = _first_line(str(row.get("reason") or ""), limit=80)
    if half(to_state) == "unavailable":
        detail = f"unavailable ({to_state})"
        if row.get("until"):
            stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(row["until"])))
            detail += f" until {stamp}"
    else:
        detail = f"launchable again ({to_state})"
    if reason:
        detail += f" — {reason}"
    return WorkFact(
        key=f"provider:{provider}:{int(row['generation'])}",
        kind=KIND_PROVIDER,
        category="system",
        project_id="",
        task_id="",
        title=f"provider {provider}",
        at=float(row["at"]),
        detail=detail,
    )


def reroute_facts(rows: Sequence[Mapping[str, Any]]) -> list[WorkFact]:
    """One digest fact per re-route batch among *rows* (provider-failover D19).

    Keyed ``reroute:<batch_id>`` -- a batch is one outage, so its trickle
    top-ups in later windows are the same fact and are not reported twice.
    """
    batches: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        batches.setdefault(str(row["batch_id"]), []).append(row)
    facts: list[WorkFact] = []
    for batch, moved in sorted(batches.items()):
        provider = str(moved[0].get("from_provider") or "")
        targets = sorted({str(row.get("to_profile_id") or "") for row in moved} - {""})
        projects = {str(row.get("project_id") or "") for row in moved}
        detail = f"{len(moved)} task(s) moved to " + (", ".join(targets) or "another provider")
        facts.append(
            WorkFact(
                key=f"reroute:{batch}",
                kind=KIND_REROUTE,
                category="system",
                project_id=projects.pop() if len(projects) == 1 else "",
                task_id="",
                title=f"provider {provider} re-route",
                at=max(float(row["at"]) for row in moved),
                detail=detail,
            )
        )
    return facts


class DigestQueryMixin:
    async def collect_digest_activity(
        self,
        window: DigestWindow,
        *,
        now: float,
        project_ids: tuple[str, ...] | None = None,
        lookback_seconds: float = 0.0,
        stale_after: float = DEFAULT_STALE_AFTER,
        open_escalations: int = 0,
        reported_keys: frozenset[str] = frozenset(),
        reported_highlights: frozenset[str] = frozenset(),
        provider_facts: bool = True,
    ) -> DigestInputs:
        """Build the inputs for one window.

        ``lookback_seconds`` extends only the *query's* lower bound, not the
        window's label: rows written after their window closed are picked up
        by the next evaluation and deduplicated by fact key, which is how §8's
        "include late arrivals in the next eligible window" is satisfied
        without rescanning history forever.

        ``provider_facts`` is ``provider_failover.notify.digest``: whether
        provider half changes are reported at all.  They are fleet facts, so
        ``project_ids`` does not narrow them.
        """
        since = window.since - max(0.0, lookback_seconds)
        until = window.until
        wanted = set(project_ids) if project_ids else None

        facts: list[WorkFact] = []
        active: list[ActiveTask] = []

        async with self._engine.connect() as conn:
            titles: dict[str, tuple[str, str, str]] = {}

            async def load_titles(task_ids: set[str]) -> None:
                missing = task_ids - set(titles)
                if not missing:
                    return
                for table in (tasks, archived_tasks):
                    rows = await conn.execute(
                        select(table.c.id, table.c.project_id, table.c.title, table.c.status).where(
                            table.c.id.in_(missing)
                        )
                    )
                    for row in rows.all():
                        titles.setdefault(
                            row.id, (row.project_id or "", row.title or "", row.status or "")
                        )

            completions = (
                (
                    await conn.execute(
                        select(task_completion_records).where(
                            task_completion_records.c.completed_at >= since,
                            task_completion_records.c.completed_at < until,
                        )
                    )
                )
                .mappings()
                .all()
            )
            await load_titles({row["task_id"] for row in completions})
            for row in completions:
                project_id, title, _status = titles.get(row["task_id"], ("", "", ""))
                if not project_id or (wanted is not None and project_id not in wanted):
                    continue
                detail = _first_line(row["summary"]) or title
                facts.append(
                    WorkFact(
                        key=f"completion:{row['id']}",
                        kind=KIND_COMPLETED,
                        category="work",
                        project_id=project_id,
                        task_id=row["task_id"],
                        title=title,
                        at=float(row["completed_at"]),
                        detail=detail,
                    )
                )
                if row["pr_url"]:
                    facts.append(
                        WorkFact(
                            key=f"pr:{row['id']}",
                            kind=KIND_PROGRESS,
                            category="vcs",
                            project_id=project_id,
                            task_id=row["task_id"],
                            title=title,
                            at=float(row["completed_at"]),
                            detail=f"pull request ready — {title}",
                        )
                    )

            starts = (
                await conn.execute(
                    select(
                        task_session_attempts.c.id,
                        task_session_attempts.c.task_id,
                        task_session_attempts.c.project_id,
                        task_session_attempts.c.started_at,
                    ).where(
                        task_session_attempts.c.started_at >= since,
                        task_session_attempts.c.started_at < until,
                    )
                )
            ).all()
            await load_titles({row.task_id for row in starts})
            for row in starts:
                project_id = row.project_id or titles.get(row.task_id, ("", "", ""))[0]
                if not project_id or (wanted is not None and project_id not in wanted):
                    continue
                title = titles.get(row.task_id, ("", "", ""))[1]
                facts.append(
                    WorkFact(
                        key=f"attempt:{row.id}",
                        kind=KIND_STARTED,
                        category="work",
                        project_id=project_id,
                        task_id=row.task_id,
                        title=title,
                        at=float(row.started_at),
                        detail=title,
                    )
                )

            # Only comments the author explicitly labelled ``progress``.  An
            # ordinary comment is not progress: a question, a plan, a status
            # request or chatter all live in this table, and reading them as
            # work would both wake an idle window and put fabricated
            # "progress" in the digest, which §8 forbids twice over.
            notes = (
                (
                    await conn.execute(
                        select(task_comments).where(
                            task_comments.c.kind == PROGRESS_COMMENT_KIND,
                            task_comments.c.created_at >= since,
                            task_comments.c.created_at < until,
                        )
                    )
                )
                .mappings()
                .all()
            )
            await load_titles({row["task_id"] for row in notes})
            for row in notes:
                project_id = row["project_id"] or titles.get(row["task_id"], ("", "", ""))[0]
                if not project_id or (wanted is not None and project_id not in wanted):
                    continue
                title = titles.get(row["task_id"], ("", "", ""))[1]
                facts.append(
                    WorkFact(
                        key=f"comment:{row['id']}",
                        kind=KIND_PROGRESS,
                        category="work",
                        project_id=project_id,
                        task_id=row["task_id"],
                        title=title,
                        at=float(row["created_at"]),
                        detail=_first_line(row["body"]),
                    )
                )

            if provider_facts:
                transitions = (
                    (
                        await conn.execute(
                            select(provider_availability_transitions).where(
                                provider_availability_transitions.c.at >= since,
                                provider_availability_transitions.c.at < until,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in transitions:
                    fact = provider_fact(row)
                    if fact is not None:
                        facts.append(fact)
                moves = (
                    (
                        await conn.execute(
                            select(task_reroutes).where(
                                task_reroutes.c.at >= since,
                                task_reroutes.c.at < until,
                                task_reroutes.c.reason_code == "provider_unavailable",
                                task_reroutes.c.batch_id.is_not(None),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                facts.extend(reroute_facts(moves))

            waiting = {
                row.task_id
                for row in (
                    await conn.execute(
                        select(agent_questions.c.task_id).where(
                            agent_questions.c.state.in_(_WAITING_QUESTION_STATES)
                        )
                    )
                ).all()
            }

            live = (
                await conn.execute(
                    select(
                        task_session_attempts.c.task_id,
                        task_session_attempts.c.project_id,
                        task_session_attempts.c.started_at,
                    )
                    .join(sessions, sessions.c.id == task_session_attempts.c.session_id)
                    .where(
                        live_attempt_predicate(now, stale_after),
                        task_session_attempts.c.started_at <= until,
                    )
                )
            ).all()
            await load_titles({row.task_id for row in live})
            seen_active: set[str] = set()
            for row in live:
                if row.task_id in waiting or row.task_id in seen_active:
                    continue
                project_id, title, status = titles.get(row.task_id, ("", "", ""))
                project_id = row.project_id or project_id
                if not project_id or (wanted is not None and project_id not in wanted):
                    continue
                if status != "IN_PROGRESS":
                    continue
                seen_active.add(row.task_id)
                active.append(
                    ActiveTask(
                        task_id=row.task_id,
                        project_id=project_id,
                        title=title,
                        started_at=float(row.started_at),
                    )
                )

            idle_query = select(tasks.c.id, tasks.c.project_id).where(
                tasks.c.status.in_(_IDLE_STATUSES)
            )
            if wanted is not None:
                idle_query = idle_query.where(tasks.c.project_id.in_(tuple(wanted)))
            touched = {fact.task_id for fact in facts if fact.task_id} | seen_active
            idle_tasks = sum(
                1 for row in (await conn.execute(idle_query)).all() if row.id not in touched
            )

            # Highlights name their project in prose, so the digest needs
            # display names rather than ids.
            shown_projects = {fact.project_id for fact in facts if fact.project_id} | {
                task.project_id for task in active
            }
            names: dict[str, str] = {}
            if shown_projects:
                project_rows = await conn.execute(
                    select(projects.c.id, projects.c.name).where(
                        projects.c.id.in_(tuple(shown_projects))
                    )
                )
                for row in project_rows.all():
                    names[row.id] = row.name or row.id

        return DigestInputs(
            window=window,
            facts=tuple(sorted(facts, key=lambda f: (f.at, f.key))),
            active=tuple(active),
            open_escalations=open_escalations,
            reported_keys=reported_keys,
            reported_highlights=reported_highlights,
            idle_tasks=idle_tasks,
            project_names=names,
        )


# --------------------------------------------------------------------------
# §4.2 the frozen brief, and the author request that holds one window
# --------------------------------------------------------------------------
#
# ``collect_digest_activity`` above answers a different question: what may make
# a window *eligible*, and what may a highlight say.  The read below answers the
# four questions a person asks when they read the channel -- what landed, what is
# stuck, what needs me, how much is running -- with exact counts beside lists that
# are capped, because the author needs the shape of a window, not every row of it.

#: Escalation states that are still a human's to answer.
_NEEDS_HUMAN_ESCALATION_STATES = ("needs_human", "stale")

#: Review states waiting on Jack's decision.  A review nobody has looked at is
#: exactly what the needs-you inbox exists for.
_NEEDS_YOU_REVIEW_STATES = ("in_review", "changes_requested")

#: How many rows one bounded read may return.  Counts come from separate
#: aggregates, so a capped page never silently becomes the count.
READ_CAP = 50

#: §4.2's request kind.  Shares the durable author-request lifecycle with the
#: morning and hourly reports; only the frozen brief and the fallback differ.
KIND_DIGEST = "digest"

#: States in which this window is still the author's to write.  ``reserved`` is
#: in the set on purpose: the wake message is a nudge that a lost event or a
#: slow playbook tick can delay, not a lock.  Requiring it would let a missing
#: minute decide that a window never gets the sentence its facts were frozen
#: for.  What makes a window closed is a submission, a fallback claim or a
#: cancellation -- all of which are recorded before this CAS runs.
_DIGEST_OPEN = ("reserved", "requested")


def _capped(statement, *order_by: Any):
    """``statement`` capped by a deterministic order, so a page cannot reorder."""
    for column in order_by:
        statement = statement.order_by(column)
    return statement.limit(READ_CAP)


class DigestSupervisorQueriesMixin:
    """The §4.2 facts read and the author request that holds one window open."""

    async def collect_digest_supervisor_facts(
        self,
        window: DigestWindow,
        *,
        now: float,
        project_ids: tuple[str, ...] | None = None,
        open_escalations: int = 0,
        active_tasks: int = 0,
        stalled_after: float = 30 * 60.0,
    ) -> dict[str, Any]:
        """Landed, stuck, needs-you and session counts for one window.

        Every project-scoped read applies ``project_ids`` before any row is
        fetched, so a narrowed destination cannot learn another project's work
        through the brief.
        """
        since, until = window.since, window.until
        wanted = tuple(project_ids) if project_ids else None
        landed: list[dict[str, Any]] = []
        stuck: list[dict[str, Any]] = []
        needs_escalations: list[dict[str, Any]] = []
        needs_reviews: list[dict[str, Any]] = []

        async with self._engine.connect() as conn:
            titles: dict[str, Any] = {}

            async def load_titles(task_ids: set[str]) -> None:
                missing = task_ids - set(titles)
                if not missing:
                    return
                for table in (tasks, archived_tasks):
                    rows = await conn.execute(
                        select(
                            table.c.id,
                            table.c.project_id,
                            table.c.title,
                            table.c.status,
                            table.c.updated_at,
                        ).where(table.c.id.in_(missing))
                    )
                    for row in rows.all():
                        titles.setdefault(row.id, row)

            completions = _capped(
                select(
                    task_completion_records.c.task_id,
                    task_completion_records.c.summary,
                    task_completion_records.c.completed_at,
                    task_completion_records.c.pr_url,
                ).where(
                    task_completion_records.c.completed_at >= since,
                    task_completion_records.c.completed_at < until,
                ),
                task_completion_records.c.completed_at.desc(),
                task_completion_records.c.task_id.desc(),
            )
            rows = (await conn.execute(completions)).all()
            await load_titles({row.task_id for row in rows})
            for row in rows:
                task = titles.get(row.task_id)
                if task is None:
                    continue
                if wanted is not None and (task.project_id or "") not in wanted:
                    continue
                landed.append(
                    {
                        "task_id": row.task_id,
                        "project_id": task.project_id or "",
                        "title": task.title or "",
                        "summary": _first_line(row.summary, 200),
                        "pull_request": bool(row.pr_url),
                        "at": float(row.completed_at),
                    }
                )

            # §4.2's stuck rules, each a durable row rather than a judgement: an
            # attempt that failed twice in the window, and a task that has been
            # BLOCKED longer than the stall threshold.
            failed = (
                await conn.execute(
                    _capped(
                        select(
                            task_session_attempts.c.task_id,
                            task_session_attempts.c.ended_at,
                        ).where(
                            task_session_attempts.c.ended_at >= since,
                            task_session_attempts.c.ended_at < until,
                            task_session_attempts.c.outcome.in_(("fail", "failed")),
                        ),
                        task_session_attempts.c.ended_at.desc(),
                    )
                )
            ).all()
            failures: dict[str, int] = {}
            for row in failed:
                failures[row.task_id] = failures.get(row.task_id, 0) + 1
            blocked_query = _capped(
                select(
                    tasks.c.id,
                    tasks.c.project_id,
                    tasks.c.title,
                    tasks.c.updated_at,
                ).where(
                    tasks.c.status == "BLOCKED",
                    tasks.c.updated_at <= now - stalled_after,
                ),
                tasks.c.updated_at.asc(),
            )
            if wanted is not None:
                blocked_query = blocked_query.where(tasks.c.project_id.in_(wanted))
            blocked_rows = (await conn.execute(blocked_query)).all()
            blocked_ids = {row.id for row in blocked_rows}
            await load_titles(blocked_ids)
            # A task whose attempts failed twice is stuck for the stronger
            # reason, even when it is no longer BLOCKED.
            twice = sorted(task_id for task_id, count in failures.items() if count >= 2)
            for row in blocked_rows:
                task = titles.get(row.id)
                if task is None or (wanted is not None and (row.project_id or "") not in wanted):
                    continue
                stuck.append(
                    {
                        "task_id": row.id,
                        "project_id": row.project_id or "",
                        "title": task.title or "",
                        "reason": "blocked",
                        "attempts_failed": failures.get(row.id, 0),
                        "blocked_minutes": int(max(0.0, now - float(row.updated_at)) // 60),
                    }
                )
            if wanted is not None:
                repeated = _capped(
                    select(
                        tasks.c.id,
                        tasks.c.project_id,
                        tasks.c.title,
                        tasks.c.updated_at,
                    ).where(
                        tasks.c.id.in_(twice) if twice else tasks.c.id.is_(None),
                        tasks.c.project_id.in_(wanted),
                    ),
                    tasks.c.updated_at.asc(),
                )
            else:
                repeated = _capped(
                    select(
                        tasks.c.id,
                        tasks.c.project_id,
                        tasks.c.title,
                        tasks.c.updated_at,
                    ).where(tasks.c.id.in_(twice) if twice else tasks.c.id.is_(None)),
                    tasks.c.updated_at.asc(),
                )
            seen_stuck = {fact["task_id"] for fact in stuck}
            for row in (await conn.execute(repeated)).all():
                if row.id in seen_stuck:
                    continue
                stuck.append(
                    {
                        "task_id": row.id,
                        "project_id": row.project_id or "",
                        "title": row.title or "",
                        "reason": "failed_twice",
                        "attempts_failed": failures.get(row.id, 0),
                        "blocked_minutes": 0,
                    }
                )

            escalation_query = _capped(
                select(
                    escalations.c.id,
                    escalations.c.project_id,
                    escalations.c.task_id,
                    escalations.c.summary,
                    escalations.c.severity,
                    escalations.c.state,
                    escalations.c.created_at,
                    escalations.c.updated_at,
                ).where(
                    escalations.c.state.in_(_NEEDS_HUMAN_ESCALATION_STATES),
                    # Delivery incidents belong to the supervisor's internal
                    # inbox, and answered escalations await supervisor action.
                    # Neither asks Jack to do anything in the digest.
                    escalations.c.source_kind != "supervisor_delivery",
                ),
                escalations.c.updated_at.desc(),
            )
            if wanted is not None:
                escalation_query = escalation_query.where(escalations.c.project_id.in_(wanted))
            for row in (await conn.execute(escalation_query)).all():
                needs_escalations.append(
                    {
                        "id": row.id,
                        "project_id": row.project_id,
                        "task_id": row.task_id,
                        "summary": _first_line(row.summary, 200),
                        "severity": row.severity,
                        "state": row.state,
                        "age_minutes": int(max(0.0, now - float(row.created_at)) // 60),
                    }
                )

            review_query = _capped(
                select(
                    doc_reviews.c.id,
                    doc_reviews.c.project_id,
                    doc_reviews.c.kind,
                    doc_reviews.c.state,
                    doc_reviews.c.title,
                    doc_reviews.c.updated_at,
                ).where(doc_reviews.c.state.in_(_NEEDS_YOU_REVIEW_STATES)),
                doc_reviews.c.updated_at.desc(),
            )
            if wanted is not None:
                review_query = review_query.where(doc_reviews.c.project_id.in_(wanted))
            for row in (await conn.execute(review_query)).all():
                needs_reviews.append(
                    {
                        "id": row.id,
                        "project_id": row.project_id,
                        "kind": row.kind,
                        "title": _first_line(row.title or "", 120),
                        "state": row.state,
                        "age_minutes": int(max(0.0, now - float(row.updated_at)) // 60),
                    }
                )

            working = int(
                await conn.scalar(
                    select(func.count())
                    .select_from(sessions)
                    .where(sessions.c.state.in_(("starting", "running", "draining")))
                )
                or 0
            )
            total = int(await conn.scalar(select(func.count()).select_from(sessions)) or 0)

        return {
            "landed": landed,
            "stuck": stuck,
            "escalations": needs_escalations,
            "reviews": needs_reviews,
            "sessions_working": working,
            "sessions_total": total,
            "active_tasks": int(active_tasks),
            "open_escalations": int(open_escalations),
        }

    async def find_digest_request(
        self, *, destination: str, generation: int, window_start: float
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """One window and its author request, named the way the author reads it.

        ``aq digest facts --since`` and ``aq digest post --window`` name a window
        by its start -- that is what the wake message carries -- so both resolve
        through the same generation-bounded identity the schedule itself uses.
        """
        async with self._engine.connect() as conn:
            window = (
                (
                    await conn.execute(
                        select(digest_windows).where(
                            digest_windows.c.destination == destination,
                            digest_windows.c.config_generation == generation,
                            digest_windows.c.window_start == window_start,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if window is None:
                return None
            request = (
                (
                    await conn.execute(
                        select(supervisor_report_requests).where(
                            supervisor_report_requests.c.kind == KIND_DIGEST,
                            supervisor_report_requests.c.owner_ref == window.id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(window), (dict(request) if request else {})

    async def latest_digest_window(
        self, *, destination: str, generation: int
    ) -> dict[str, Any] | None:
        """The newest window of this generation, for a caller that names none."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(digest_windows)
                        .where(
                            digest_windows.c.destination == destination,
                            digest_windows.c.config_generation == generation,
                        )
                        .order_by(digest_windows.c.window_end.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    async def reserve_digest_request_in_transaction(
        self, conn: Any, *, candidate: Mapping[str, Any]
    ) -> str | None:
        """One author request per digest window, inside the evaluation transaction.

        ``(kind, owner_ref)`` is unique, so the row is the window's dedup as well
        as its wake: a re-evaluated window, a replayed event and a second daemon
        all find it already there.
        """
        now = float(candidate["now"])
        request_id = f"report-digest-{candidate['window_id']}"
        inserted = (
            await conn.execute(
                pg_insert(supervisor_report_requests)
                .values(
                    id=request_id,
                    kind=KIND_DIGEST,
                    owner_ref=candidate["window_id"],
                    destination=candidate["destination"],
                    visibility=dict(candidate.get("visibility") or {}),
                    brief=dict(candidate["brief"]),
                    brief_hash=str(candidate["brief_hash"]),
                    fallback_text=candidate["fallback_text"],
                    author_session_id=candidate["author_session_id"],
                    state="reserved",
                    deadline=float(candidate["deadline"]),
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["kind", "owner_ref"])
                .returning(supervisor_report_requests.c.id)
            )
        ).scalar_one_or_none()
        return str(inserted) if inserted else None

    async def list_reserved_digest_requests(
        self, *, now: float, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Reserved digest windows still waiting for their author wake.

        Recovery for a lost event or a restart: the request is durable, so the
        minute reconciliation finds it again.  A window the delivery pump has
        already leased, or one whose deadline has passed, is not woken -- §4.2's
        fallback owns those.
        """
        if not 1 <= limit <= 20:
            raise ValueError("digest reconciliation is bounded to 20 rows")
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(supervisor_report_requests)
                        .join(
                            digest_windows,
                            digest_windows.c.id == supervisor_report_requests.c.owner_ref,
                        )
                        .where(
                            supervisor_report_requests.c.kind == KIND_DIGEST,
                            supervisor_report_requests.c.state == "reserved",
                            supervisor_report_requests.c.deadline > now,
                            digest_windows.c.send_status == "pending",
                            digest_windows.c.lease_owner.is_(None),
                        )
                        .order_by(supervisor_report_requests.c.created_at)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def cancel_digest_requests(self, *, now: float) -> int:
        """Flag off: release every held window to its deterministic fallback."""
        async with self.immediate() as conn:
            rows = (
                await conn.execute(
                    select(
                        supervisor_report_requests.c.id, supervisor_report_requests.c.owner_ref
                    ).where(
                        supervisor_report_requests.c.kind == KIND_DIGEST,
                        supervisor_report_requests.c.state.in_(("reserved", "requested")),
                    )
                )
            ).all()
            if not rows:
                return 0
            await conn.execute(
                update(supervisor_report_requests)
                .where(
                    supervisor_report_requests.c.id.in_([row.id for row in rows]),
                    supervisor_report_requests.c.kind == KIND_DIGEST,
                )
                .values(
                    state="cancelled",
                    skip_reason="feature_off",
                    version=supervisor_report_requests.c.version + 1,
                    updated_at=now,
                )
            )
            # The held deadline was the author's. With the flag off the window is
            # owed now, so the deterministic digest posts it on this pass rather
            # than sitting silent until a cadence that no longer exists.
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id.in_([row.owner_ref for row in rows]))
                .values(due_at=now, updated_at=now)
            )
        return len(rows)

    async def submit_digest_post(
        self,
        request_id: str,
        *,
        text: str,
        now: float,
    ) -> dict[str, Any] | None:
        """CAS the author's body onto its window and make it due immediately.

        The lock order is the delivery pump's own (request, then window), so a
        post racing the fallback deadline produces a winner rather than a
        deadlock.  Every reason the window might already be spoken for -- not
        requested, expired, leased, or a destination we never reserved -- is
        checked here: after the pump claims a window there is one post per window
        and no later edit.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(supervisor_report_requests)
                        .where(supervisor_report_requests.c.id == request_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["kind"] != KIND_DIGEST or row["state"] not in _DIGEST_OPEN:
                return None
            if float(row["deadline"]) <= now:
                return None
            window = (
                (
                    await conn.execute(
                        select(digest_windows)
                        .where(digest_windows.c.id == row["owner_ref"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                window is None
                or window["send_status"] != "pending"
                or window["lease_owner"] is not None
                or window["destination"] != row["destination"]
            ):
                return None
            payload = dict(window["payload"] or {})
            payload["text"] = text
            payload["author_request_id"] = request_id
            payload["authored"] = True
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id == row["owner_ref"])
                .values(payload=payload, output_hash=digest, due_at=now, updated_at=now)
            )
            changed = (
                (
                    await conn.execute(
                        update(supervisor_report_requests)
                        .where(supervisor_report_requests.c.id == request_id)
                        .values(
                            state="submitted",
                            version=row["version"] + 1,
                            submitted_text=text,
                            submitted_hash=digest,
                            submitted_at=now,
                            updated_at=now,
                        )
                        .returning(supervisor_report_requests)
                    )
                )
                .mappings()
                .one()
            )
        return dict(changed)
