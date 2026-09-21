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
keyed ``provider:<key>:<generation>``.  An outage in a quiet hour is exactly
what the digest is for, so such a fact makes a window eligible on its own.

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

import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from src.database.queries.task_session_queries import live_attempt_predicate
from src.database.tables import (
    agent_questions,
    archived_tasks,
    projects,
    provider_availability_transitions,
    sessions,
    task_comments,
    task_completion_records,
    task_session_attempts,
    tasks,
)
from src.digest.facts import (
    KIND_COMPLETED,
    KIND_PROGRESS,
    KIND_PROVIDER,
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
                        select(
                            table.c.id, table.c.project_id, table.c.title, table.c.status
                        ).where(table.c.id.in_(missing))
                    )
                    for row in rows.all():
                        titles.setdefault(
                            row.id, (row.project_id or "", row.title or "", row.status or "")
                        )

            completions = (
                await conn.execute(
                    select(task_completion_records).where(
                        task_completion_records.c.completed_at >= since,
                        task_completion_records.c.completed_at < until,
                    )
                )
            ).mappings().all()
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
                await conn.execute(
                    select(task_comments).where(
                        task_comments.c.kind == PROGRESS_COMMENT_KIND,
                        task_comments.c.created_at >= since,
                        task_comments.c.created_at < until,
                    )
                )
            ).mappings().all()
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
                    await conn.execute(
                        select(provider_availability_transitions).where(
                            provider_availability_transitions.c.at >= since,
                            provider_availability_transitions.c.at < until,
                        )
                    )
                ).mappings().all()
                for row in transitions:
                    fact = provider_fact(row)
                    if fact is not None:
                        facts.append(fact)

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
