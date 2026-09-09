"""Recent-work reads: window boundaries, attempts, and model attribution.

Covers ``Database.list_recent_task_activity`` and the ``task_recent_activity``
command that the Tasks tab's "last 24 hours" view is built on.
"""

from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from src.commands.task_commands import TaskCommandsMixin
from src.database.adapters.sqlite import SQLiteDatabaseAdapter
from src.database.tables import archived_tasks, task_session_attempts, tasks
from src.models import Project, Task, TaskCompletion

NOW = 1_000_000.0
DAY = 24 * 3600.0
SINCE = NOW - DAY


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "activity.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    await database.create_project(Project(id="other", name="Other"))
    yield database
    await database.close()


async def make_task(db, task_id, *, project_id="p", status="COMPLETED", updated_at=0.0, title=None):
    await db.create_task(
        Task(id=task_id, project_id=project_id, title=title or task_id, description="")
    )
    # Creating a task stamps "now"; every test pins its own timeline instead.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(status=status, created_at=updated_at, updated_at=updated_at)
        )


async def add_attempt(db, task_id, *, started_at, ended_at=None, model="claude-opus-5",
                      project_id="p", state="stopped", outcome=None, session_id="s"):
    attempt_id = uuid4().hex
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id=attempt_id, session_id=session_id, task_id=task_id, project_id=project_id,
                agent_id="a", agent_name="Worker", profile_id="worker", name="worker",
                lifecycle="pool", model=model, intelligence_class="standard-medium",
                llm_provider="anthropic", harness="claude", provider="anthropic", state=state,
                work_dir="/w", started_at=started_at, session_started_at=started_at,
                ended_at=ended_at, outcome=outcome,
            )
        )
    return attempt_id


async def add_completion(db, task_id, *, completed_at, outcome="pass", summary="done"):
    await db.save_task_completion(
        TaskCompletion(
            id=uuid4().hex, task_id=task_id, outcome=outcome,
            summary=summary, completed_at=completed_at,
        )
    )


async def activity(db, **kwargs):
    kwargs.setdefault("since", SINCE)
    kwargs.setdefault("until", NOW)
    return await db.list_recent_task_activity(**kwargs)


# ---------------------------------------------------------------------------
# Window boundaries
# ---------------------------------------------------------------------------


async def test_an_attempt_on_either_edge_of_the_window_is_included(db):
    await make_task(db, "start-edge", updated_at=SINCE - DAY)
    await make_task(db, "end-edge", updated_at=SINCE - DAY)
    await add_attempt(db, "start-edge", started_at=SINCE - 60, ended_at=SINCE)
    await add_attempt(db, "end-edge", started_at=NOW, ended_at=NOW)

    items, total = await activity(db)

    assert total == 2
    assert {item["task_id"] for item in items} == {"start-edge", "end-edge"}


async def test_work_that_finished_before_the_window_is_excluded(db):
    await make_task(db, "yesterday", updated_at=SINCE - DAY)
    await add_attempt(db, "yesterday", started_at=SINCE - 7200, ended_at=SINCE - 1)

    assert await activity(db) == ([], 0)


async def test_an_attempt_that_started_before_the_window_and_still_runs_is_included(db):
    """In-progress work is the whole point of the view; it must not fall out."""
    await make_task(db, "long-runner", status="IN_PROGRESS", updated_at=SINCE - 3 * DAY)
    await add_attempt(
        db, "long-runner", started_at=SINCE - 3 * DAY, ended_at=None, state="running"
    )

    items, total = await activity(db)

    assert total == 1
    assert items[0]["task_id"] == "long-runner"
    assert items[0]["status"] == "IN_PROGRESS"
    assert items[0]["attempts"][0]["ended_at"] is None


async def test_a_status_change_with_no_session_still_counts_as_activity(db):
    await make_task(db, "hand-edited", status="BLOCKED", updated_at=NOW - 60)

    items, _ = await activity(db)

    assert [item["task_id"] for item in items] == ["hand-edited"]
    assert items[0]["attempts"] == []
    assert items[0]["models"] == []
    assert items[0]["attempt_count"] == 0


async def test_a_completion_inside_the_window_counts_even_when_the_row_is_older(db):
    await make_task(db, "closed", updated_at=SINCE - DAY)
    await add_completion(db, "closed", completed_at=NOW - 120, outcome="pass")

    items, _ = await activity(db)

    assert items[0]["task_id"] == "closed"
    assert items[0]["outcome"] == "pass"
    assert items[0]["completed_at"] == NOW - 120
    assert items[0]["last_activity_at"] == NOW - 120


async def test_older_attempts_on_a_task_active_in_the_window_are_not_reported(db):
    """The window bounds the *work*, not just the task: only in-window attempts."""
    await make_task(db, "t", updated_at=SINCE - DAY)
    await add_attempt(db, "t", started_at=SINCE - 5 * DAY, ended_at=SINCE - 4 * DAY,
                      model="ancient-model")
    await add_attempt(db, "t", started_at=NOW - 3600, ended_at=NOW - 60, model="claude-opus-5")

    items, _ = await activity(db)

    assert items[0]["models"] == ["claude-opus-5"]
    assert items[0]["attempt_count"] == 1


# ---------------------------------------------------------------------------
# Model attribution
# ---------------------------------------------------------------------------


async def test_multiple_attempts_report_every_model_newest_first_without_duplicates(db):
    await make_task(db, "t", updated_at=SINCE - DAY)
    await add_attempt(db, "t", started_at=NOW - 3 * 3600, ended_at=NOW - 2 * 3600,
                      model="claude-sonnet-5", session_id="s1")
    await add_attempt(db, "t", started_at=NOW - 2 * 3600, ended_at=NOW - 3600,
                      model="claude-opus-5", session_id="s2")
    await add_attempt(db, "t", started_at=NOW - 3600, ended_at=NOW - 60,
                      model="claude-opus-5", session_id="s3")

    items, _ = await activity(db)

    assert items[0]["attempt_count"] == 3
    assert items[0]["models"] == ["claude-opus-5", "claude-sonnet-5"]
    assert items[0]["unattributed_attempts"] == 0


async def test_missing_attribution_is_counted_not_invented(db):
    await make_task(db, "t", updated_at=SINCE - DAY)
    await add_attempt(db, "t", started_at=NOW - 7200, ended_at=NOW - 3600, model=None,
                      session_id="s1")
    await add_attempt(db, "t", started_at=NOW - 3600, ended_at=NOW - 60,
                      model="claude-opus-5", session_id="s2")

    items, _ = await activity(db)

    assert items[0]["models"] == ["claude-opus-5"]
    assert items[0]["unattributed_attempts"] == 1


async def test_a_task_whose_only_attempt_reports_no_model_has_no_models(db):
    await make_task(db, "t", updated_at=SINCE - DAY)
    await add_attempt(db, "t", started_at=NOW - 3600, ended_at=NOW - 60, model=None)

    items, _ = await activity(db)

    assert items[0]["models"] == []
    assert items[0]["attempt_count"] == 1
    assert items[0]["unattributed_attempts"] == 1


# ---------------------------------------------------------------------------
# Scoping, ordering, and archived work
# ---------------------------------------------------------------------------


async def test_ordering_is_by_last_activity_and_covers_finished_and_running_work(db):
    await make_task(db, "running", status="IN_PROGRESS", updated_at=SINCE - DAY)
    await make_task(db, "finished", status="COMPLETED", updated_at=SINCE - DAY)
    await add_attempt(db, "running", started_at=NOW - 600, ended_at=None, state="running")
    await add_attempt(db, "finished", started_at=NOW - 7200, ended_at=NOW - 3600)
    await add_completion(db, "finished", completed_at=NOW - 3600)

    items, _ = await activity(db)

    assert [item["task_id"] for item in items] == ["running", "finished"]
    assert items[0]["last_activity_at"] > items[1]["last_activity_at"]


async def test_the_project_filter_drops_other_projects(db):
    await make_task(db, "mine", updated_at=NOW - 60)
    await make_task(db, "theirs", project_id="other", updated_at=NOW - 30)

    items, total = await activity(db, project_id="p")

    assert total == 1
    assert [item["task_id"] for item in items] == ["mine"]
    assert len((await activity(db))[0]) == 2


async def test_archived_tasks_keep_their_history(db):
    await make_task(db, "gone", updated_at=SINCE - DAY)
    await add_attempt(db, "gone", started_at=NOW - 3600, ended_at=NOW - 60)
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(archived_tasks).values(
                id="gone", project_id="p", title="Archived work", description="",
                status="COMPLETED", created_at=SINCE - DAY, updated_at=SINCE - DAY,
                archived_at=NOW - 30,
            )
        )
        await conn.execute(tasks.delete().where(tasks.c.id == "gone"))

    items, _ = await activity(db)

    assert items[0]["task_id"] == "gone"
    assert items[0]["archived"] is True
    assert items[0]["title"] == "Archived work"
    assert items[0]["models"] == ["claude-opus-5"]


async def test_limit_caps_the_rows_but_not_the_total(db):
    for index in range(5):
        await make_task(db, f"t{index}", updated_at=NOW - index)

    items, total = await activity(db, limit=2)

    assert total == 5
    assert [item["task_id"] for item in items] == ["t0", "t1"]


# ---------------------------------------------------------------------------
# The command surface
# ---------------------------------------------------------------------------


class Handler(TaskCommandsMixin):
    def __init__(self, db, project_id=None):
        self.db = db
        self._active_project_id = project_id


async def test_command_defaults_to_a_labeled_24_hour_window_and_totals_by_model(db, monkeypatch):
    monkeypatch.setattr("src.commands.task_commands.time.time", lambda: NOW)
    await make_task(db, "t", updated_at=SINCE - DAY)
    await make_task(db, "u", updated_at=SINCE - DAY)
    await add_attempt(db, "t", started_at=NOW - 7200, ended_at=NOW - 3600,
                      model="claude-opus-5", session_id="s1")
    await add_attempt(db, "t", started_at=NOW - 3600, ended_at=NOW - 60, model=None,
                      session_id="s2")
    await add_attempt(db, "u", started_at=NOW - 1800, ended_at=NOW - 30,
                      model="claude-opus-5", session_id="s3")

    result = await Handler(db)._cmd_task_recent_activity({})

    assert result["success"] is True
    assert result["hours"] == 24.0
    assert (result["since"], result["until"]) == (SINCE, NOW)
    assert result["total"] == 2
    assert result["truncated"] is False
    assert result["by_model"] == [
        {"model": "claude-opus-5", "tasks": 2, "attempts": 2},
        {"model": None, "tasks": 1, "attempts": 1},
    ]


async def test_command_honours_hours_and_falls_back_to_the_active_project(db, monkeypatch):
    monkeypatch.setattr("src.commands.task_commands.time.time", lambda: NOW)
    await make_task(db, "recent", updated_at=NOW - 1800)
    await make_task(db, "older", updated_at=NOW - 3 * 3600)
    await make_task(db, "elsewhere", project_id="other", updated_at=NOW - 60)

    result = await Handler(db, project_id="p")._cmd_task_recent_activity({"hours": 1})

    assert result["project_id"] == "p"
    assert [item["task_id"] for item in result["items"]] == ["recent"]


async def test_command_rejects_a_non_positive_window_and_caps_a_huge_one(db, monkeypatch):
    monkeypatch.setattr("src.commands.task_commands.time.time", lambda: NOW)

    assert await Handler(db)._cmd_task_recent_activity({"hours": 0}) == {
        "success": False, "error": "hours must be greater than 0"
    }
    assert (await Handler(db)._cmd_task_recent_activity({"hours": "soon"}))["error"] == (
        "hours must be a number"
    )
    capped = await Handler(db)._cmd_task_recent_activity({"hours": 10_000})
    assert capped["hours"] == 24.0 * 31


async def test_command_flags_truncation(db, monkeypatch):
    monkeypatch.setattr("src.commands.task_commands.time.time", lambda: NOW)
    for index in range(3):
        await make_task(db, f"t{index}", updated_at=NOW - index)

    result = await Handler(db)._cmd_task_recent_activity({"limit": 2})

    assert result["truncated"] is True
    assert result["total"] == 3
    assert len(result["items"]) == 2


async def test_command_result_validates_against_the_typed_api_response(db, monkeypatch):
    """The codegen route serializes through this model; a shape drift 500s it."""
    from src.api.models.task import TaskRecentActivityResponse

    monkeypatch.setattr("src.commands.task_commands.time.time", lambda: NOW)
    await make_task(db, "t", status="IN_PROGRESS", updated_at=NOW - 100)
    await add_attempt(db, "t", started_at=NOW - 3600, ended_at=None, state="running")
    await add_completion(db, "t", completed_at=NOW - 90)

    result = await Handler(db)._cmd_task_recent_activity({})
    model = TaskRecentActivityResponse.model_validate(result)

    assert model.items[0].task_id == "t"
    assert model.items[0].attempts[0].model == "claude-opus-5"
    assert model.items[0].outcome == "pass"
    assert model.by_model[0].model == "claude-opus-5"
