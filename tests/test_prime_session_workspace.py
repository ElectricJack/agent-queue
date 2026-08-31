"""Prime must describe the active session, not a reused workspace slot."""

import time

import pytest

from src.config import AppConfig
from src.database import Database
from src.models import AgentProfile, Project, SessionRecord, Task
from src.prime import PrimeRenderer


@pytest.fixture
async def prime_context(tmp_path):
    db = Database(str(tmp_path / "prime.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_profile(AgentProfile(id="coder", name="Coder"))
    task = Task(id="work", project_id="p", title="Work", description="", profile_id="coder")
    await db.create_task(task)
    await db.create_task(Task(id="other", project_id="p", title="Other", description=""))
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    yield db, PrimeRenderer(db, cfg), tmp_path
    await db.close()


async def _session(
    db, path, *, session_id="live", task_id="work", state="running", started_at=None
):
    await db.create_session(
        SessionRecord(
            id=session_id,
            project_id="p",
            profile_id="coder",
            harness="codex",
            provider="fake",
            name=f"s-{session_id}",
            lifecycle="task",
            task_id=task_id,
            work_dir=str(path),
            epoch="test",
            instance_token="test-only",
            state=state,
            started_at=started_at if started_at is not None else time.time(),
        )
    )


@pytest.mark.parametrize("explicit_session", [False, True])
async def test_active_session_work_dir_overrides_old_task_metadata(prime_context, explicit_session):
    db, renderer, root = prime_context
    old = root / "old-slot"
    current = root / "current-slot"
    for path, body in ((old, "WRONG old override"), (current, "Current workspace: {{work_dir}}")):
        (path / ".aq").mkdir(parents=True)
        (path / ".aq" / "PRIME.md").write_text(body)
    await db.set_task_meta("work", "work_dir", str(old))
    await _session(db, current, started_at=10)
    await _session(db, old, session_id="newer-stopped", state="stopped", started_at=20)
    doc = await renderer.render_for_task("work", session_id="live" if explicit_session else None)
    assert doc.work_dir == str(current)
    assert doc.to_markdown() == f"Current workspace: {current}"
    assert str(current) in next(s.body for s in doc.sections if s.key == "workspaces")


async def test_prime_without_work_metadata_uses_live_session_directory(prime_context):
    db, renderer, root = prime_context
    current = root / "current-slot"
    await _session(db, current)
    doc = await renderer.render_for_task("work")
    assert doc.work_dir == str(current)
    assert f"**work_dir:** {current}" in doc.to_markdown()


async def test_explicit_directory_override_still_wins(prime_context):
    db, renderer, root = prime_context
    await _session(db, root / "current-slot")
    override = root / "requested"
    doc = await renderer.render_for_task("work", work_dir=str(override))
    assert doc.work_dir == str(override)


async def test_stopped_or_foreign_session_does_not_override_saved_task_directory(prime_context):
    db, renderer, root = prime_context
    saved = root / "saved-task-dir"
    await db.set_task_meta("work", "work_dir", str(saved))
    await _session(db, root / "reused-slot", state="stopped")
    await _session(db, root / "foreign-slot", session_id="foreign", task_id="other")
    for session_id in (None, "foreign"):
        doc = await renderer.render_for_task("work", session_id=session_id)
        assert doc.work_dir == str(saved)


async def test_saved_salvage_diff_is_marked_as_historical(prime_context):
    db, renderer, _ = prime_context
    diff = "diff --git a/work.py b/work.py\n-old\n+saved work\n"
    await db.add_task_context(
        "work",
        type="worktree_salvage",
        label="Salvaged changes from old-slot",
        content=diff,
    )
    await db.add_task_context("work", type="note", label="Current request", content="Do this next")
    doc = await renderer.render_for_task("work")
    context = next(s.body for s in doc.sections if s.key == "task_context")
    assert "Historical recovery snapshot" in context
    assert "git status" in context
    assert diff.strip() in context
    assert "**Current request:**\nDo this next" in context
