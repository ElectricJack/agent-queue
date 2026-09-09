"""What a digest window is allowed to see (implementation spec §8).

These cover the read half: durable facts only, "active" meaning a live
current attempt, project visibility applied in SQL, and the lookback that
picks up rows written after their window closed.
"""

from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from src.database import Database
from src.database.queries.digest_queries import DigestQueryMixin
from src.database.tables import agent_questions, task_comments, task_session_attempts, tasks
from src.digest import DigestWindow, build_digest
from src.models import Project, SessionRecord, Task, TaskCompletion
from tests.db_fixtures import lease_dsn

NOW = 2_000_000.0
HOUR = 3600.0
WINDOW = DigestWindow(since=NOW - HOUR, until=NOW)


@pytest.fixture
async def db():
    database = Database(lease_dsn("digest.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Agent Queue"))
    await database.create_project(Project(id="other", name="Other"))
    yield database
    await database.close()


async def make_task(db, task_id, *, project_id="p", status="COMPLETED", title=None):
    await db.create_task(
        Task(id=task_id, project_id=project_id, title=title or task_id, description="")
    )
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(status=status))


async def add_attempt(db, task_id, *, started_at, ended_at=None, project_id="p",
                      session_id="s1"):
    attempt_id = uuid4().hex
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id=attempt_id, session_id=session_id, task_id=task_id, project_id=project_id,
                agent_id="a", agent_name="Worker", profile_id="worker", name="worker",
                lifecycle="pool", model="claude-opus-5", harness="claude", provider="tmux",
                state="running", work_dir="/w", started_at=started_at,
                session_started_at=started_at, ended_at=ended_at,
            )
        )
    return attempt_id


async def add_session(db, session_id, *, state="running", last_activity=NOW - 30,
                      project_id="p", task_id=None):
    await db.create_session(
        SessionRecord(
            id=session_id, project_id=project_id, profile_id="worker", harness="claude",
            provider="tmux", name=f"s-{session_id}", lifecycle="pool", work_dir="/w",
            epoch="e", instance_token=session_id, started_at=NOW - 2 * HOUR,
            task_id=task_id, state=state, last_activity=last_activity,
        )
    )


async def complete(db, task_id, *, at, summary="shipped the thing", pr_url=None):
    await db.save_task_completion(
        TaskCompletion(
            id="completion-" + uuid4().hex, task_id=task_id, outcome="pass",
            summary=summary, pr_url=pr_url, completed_at=at,
        )
    )


async def note(db, task_id, body, *, at, project_id="p", kind="progress",
               author_kind="agent", author_id="worker"):
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_comments).values(
                id="comment-" + uuid4().hex, task_id=task_id, project_id=project_id,
                body=body, author_kind=author_kind, author_id=author_id, kind=kind,
                created_at=at,
            )
        )


async def collect(db, **kwargs):
    kwargs.setdefault("now", NOW)
    return await db.collect_digest_activity(kwargs.pop("window", WINDOW), **kwargs)


def keys(inputs):
    return {fact.key.split(":")[0] for fact in inputs.facts}


def test_the_mixin_is_composed_into_the_database():
    assert issubclass(Database, DigestQueryMixin)


async def test_a_completion_in_the_window_is_a_work_fact(db):
    await make_task(db, "t1", title="Ship pools")
    await complete(db, "t1", at=NOW - 120)
    inputs = await collect(db)
    assert [(f.kind, f.category, f.project_id) for f in inputs.facts] == [
        ("completed", "work", "p")
    ]
    assert inputs.facts[0].detail == "shipped the thing"
    assert inputs.project_names["p"] == "Agent Queue"


async def test_a_pull_request_is_a_separate_vcs_milestone(db):
    await make_task(db, "t1")
    await complete(db, "t1", at=NOW - 120, pr_url="https://github.com/x/y/pull/1")
    inputs = await collect(db)
    assert {(f.kind, f.category) for f in inputs.facts} == {
        ("completed", "work"),
        ("progress", "vcs"),
    }


async def test_attempt_starts_and_progress_notes_are_facts(db):
    await make_task(db, "t1", status="IN_PROGRESS")
    await add_attempt(db, "t1", started_at=NOW - 600)
    await note(db, "t1", "Rebased and green on 65 tests", at=NOW - 300)
    inputs = await collect(db)
    assert keys(inputs) == {"attempt", "comment"}


async def test_an_ordinary_comment_is_not_progress(db):
    """A question, a plan or chatter is history, not recorded work.

    Reading every comment as progress would let a human asking "any update?"
    manufacture an hourly digest claiming the task progressed — §8 forbids
    both the eligibility and the fabricated highlight.
    """
    await make_task(db, "t1", status="READY")
    await note(db, "t1", "Any update on this?", at=NOW - 300, kind="note",
               author_kind="user", author_id="local")
    await note(db, "t1", "Plan: start with the parser, then the renderer.",
               at=NOW - 250, kind="note")
    inputs = await collect(db)
    assert inputs.facts == ()
    assert inputs.active == ()
    result = build_digest(inputs)
    assert not result.send
    assert result.reason == "idle_only"


async def test_a_recorded_progress_note_alone_makes_a_window_eligible(db):
    """The other half: an explicitly labelled note still counts as work."""
    await make_task(db, "t1", status="IN_PROGRESS", title="Ship pools")
    await note(db, "t1", "Migration applied; 65 tests green", at=NOW - 300)
    inputs = await collect(db)
    assert [(f.kind, f.category) for f in inputs.facts] == [("progress", "work")]
    assert inputs.facts[0].detail == "Migration applied; 65 tests green"
    assert build_digest(inputs).send


async def test_progress_and_ordinary_comments_on_one_task_report_only_progress(db):
    await make_task(db, "t1", status="IN_PROGRESS")
    await note(db, "t1", "Blocked on a decision?", at=NOW - 400, kind="note")
    await note(db, "t1", "PR #524 pushed", at=NOW - 300)
    inputs = await collect(db)
    assert [f.detail for f in inputs.facts] == ["PR #524 pushed"]


async def test_updated_at_churn_alone_produces_no_facts(db):
    await make_task(db, "t1", status="READY")
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "t1").values(updated_at=NOW - 5)
        )
    inputs = await collect(db)
    assert inputs.facts == ()
    assert inputs.active == ()
    assert inputs.idle_tasks == 1
    assert build_digest(inputs).send is False


async def test_a_live_running_attempt_counts_as_active(db):
    await make_task(db, "t1", status="IN_PROGRESS", title="Long job")
    await add_session(db, "live", last_activity=NOW - 30, task_id="t1")
    await add_attempt(db, "t1", started_at=NOW - 5 * HOUR, session_id="live")
    inputs = await collect(db)
    assert [t.task_id for t in inputs.active] == ["t1"]
    assert inputs.facts == ()
    result = build_digest(inputs)
    assert (result.send, result.active_count) == (True, 1)


async def test_a_stale_session_is_not_active(db):
    await make_task(db, "t1", status="IN_PROGRESS")
    await add_session(db, "stale", last_activity=NOW - 3 * HOUR, task_id="t1")
    await add_attempt(db, "t1", started_at=NOW - 5 * HOUR, session_id="stale")
    inputs = await collect(db)
    assert inputs.active == ()
    assert build_digest(inputs).send is False


async def test_a_worker_waiting_on_a_human_is_not_active(db):
    await make_task(db, "t1", status="IN_PROGRESS")
    await add_session(db, "live", task_id="t1")
    await add_attempt(db, "t1", started_at=NOW - HOUR, session_id="live")
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(agent_questions).values(
                id="q1", session_id="live", session_name="s-live", instance_token="live",
                task_id="t1", project_id="p", agent_id="a", turn_id="turn", claim_epoch=1,
                question="which way?", requires_human=True, state="human",
                created_at=NOW - 600, updated_at=NOW - 600, source_ts=NOW - 600,
            )
        )
    inputs = await collect(db, window=DigestWindow(since=NOW - 0.5 * HOUR, until=NOW))
    assert inputs.active == ()


async def test_an_in_progress_container_with_no_attempt_is_idle_not_active(db):
    await make_task(db, "epic", status="IN_PROGRESS", title="Epic")
    inputs = await collect(db)
    assert (inputs.active, inputs.facts) == ((), ())
    assert inputs.idle_tasks == 1


async def test_an_ended_attempt_is_not_active(db):
    await make_task(db, "t1", status="IN_PROGRESS")
    # No ``task_id`` on the session: creating one with a task snapshots a
    # fresh live attempt, which is exactly what this test must not have.
    await add_session(db, "live")
    await add_attempt(db, "t1", started_at=NOW - 2 * HOUR, ended_at=NOW - HOUR,
                      session_id="live")
    inputs = await collect(db, window=DigestWindow(since=NOW - 0.5 * HOUR, until=NOW))
    assert inputs.active == ()


async def test_another_projects_work_is_never_collected(db):
    await make_task(db, "t1", project_id="other", title="Secret")
    await complete(db, "t1", at=NOW - 60, summary="secret work")
    inputs = await collect(db, project_ids=("p",))
    assert inputs.facts == ()
    assert "Secret" not in str(inputs.project_names)


async def test_lookback_picks_up_a_row_written_after_its_window_closed(db):
    await make_task(db, "t1")
    await complete(db, "t1", at=NOW - HOUR - 300)
    later = DigestWindow(since=NOW, until=NOW + HOUR)
    without = await collect(db, window=later, now=NOW + HOUR)
    assert without.facts == ()
    with_lookback = await collect(
        db, window=later, now=NOW + HOUR, lookback_seconds=2 * HOUR
    )
    assert len(with_lookback.facts) == 1
    # And it is reported exactly once: the next window's lookback sees it
    # again but the fact key is already spent.
    first = build_digest(with_lookback)
    again = await collect(
        db, window=DigestWindow(since=NOW + HOUR, until=NOW + 2 * HOUR),
        now=NOW + 2 * HOUR, lookback_seconds=4 * HOUR,
        reported_keys=first.reported_keys,
    )
    assert build_digest(again).send is False
