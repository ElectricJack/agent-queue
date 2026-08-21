"""SessionLens — adapter over the session runtime for the messaging engine.

Covers the SessionManagerProto surface Task 2's MessageDeliveryEngine
consumes, using the recipient vocabulary in the spec
(``MESSAGE_TO_KINDS = {session, task, profile, user}``). The lens itself
speaks only ``"task"`` (task id) and ``"session"`` (session name).
Fake session provider + in-memory sqlite DB — no tmux, no real agent, no
LLM.
"""

from __future__ import annotations

import json
import time

import pytest

from src.messages import Activity, SessionLens, SessionManagerProto
from src.models import AgentProfile, Project, SessionRecord, Task
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.provider import SessionHandle
from src.sessions.spec import SessionSpecBuilder


def _supervisor_address(project_id: str) -> str:
    """Messaging *address* for the supervisor (spec §5): ``supervisor-<pid>``.

    This is what appears in ``messages.to_id``; it is NOT the runtime
    session name. The lens translates it to
    :func:`~src.sessions.spec.named_session_name`
    (``n-supervisor--<pid>``) at every runtime-facing edge so the
    reconciler can adopt the session after a daemon restart.
    """
    return f"supervisor-{project_id}"


# Backwards-compat alias used by legacy call sites in this file.
_supervisor_name = _supervisor_address


def _supervisor_runtime_name(project_id: str) -> str:
    """Runtime session name for a supervisor (``n-supervisor--<pid>``)."""
    from src.sessions.spec import named_session_name

    return named_session_name("supervisor", project_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id="proj1", name="Proj1"))
    yield database
    await database.close()


@pytest.fixture
def config():
    class _Sessions:
        # Match how the reconciler picks a provider: read
        # ``config.sessions.provider``. Tests register the fake under this
        # name so the lens resolves it via the same code path production
        # uses — no test-shaped fallback in the implementation.
        provider = "fake"

    class _Cfg:
        vault_root = "/tmp/vault"
        mcp_server = None
        sessions = _Sessions()
    return _Cfg()


@pytest.fixture
def providers(config):
    return SessionProviderRegistry({FakeProvider.name: FakeProvider}, config=config)


@pytest.fixture
def harness_registry():
    reg = HarnessRegistry()
    reg.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    return reg


@pytest.fixture
def spec_builder(config, harness_registry):
    return SessionSpecBuilder(config, harness_registry)


@pytest.fixture
def supervisor_profile():
    return AgentProfile(id="supervisor", name="Supervisor", harness="claude", lifecycle="named")


@pytest.fixture
def profiles_loader(supervisor_profile):
    async def _load(profile_id: str):
        if profile_id == "supervisor":
            return supervisor_profile
        return None

    return _load


TEST_EPOCH = "epoch-cafeb0ba"


@pytest.fixture
def lens(db, providers, spec_builder, harness_registry, config, profiles_loader):
    return SessionLens(
        db=db,
        providers=providers,
        spec_builder=spec_builder,
        harness_registry=harness_registry,
        config=config,
        profiles_loader=profiles_loader,
        epoch=TEST_EPOCH,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_session_lens_is_a_sessionmanagerproto(lens):
    assert isinstance(lens, SessionManagerProto)


def test_activity_type_literal():
    # If this stops importing/being a Literal set the delivery engine
    # will silently accept the wrong strings.
    assert set(getattr(Activity, "__args__", ())) == {"idle", "busy", "sleeping", "absent"}


# ---------------------------------------------------------------------------
# activity()
# ---------------------------------------------------------------------------


class TestActivity:
    async def test_task_with_no_session_row_is_absent(self, lens):
        got = await lens.activity(kind="task", target_id="task-missing", project_id="proj1")
        assert got == "absent"

    async def test_session_plain_name_with_no_row_is_absent(self, lens):
        # A plain (non-supervisor) session name has no wake-on-demand path;
        # the messenger must not spawn one.
        got = await lens.activity(
            kind="session", target_id="s-task-missing", project_id="proj1"
        )
        assert got == "absent"

    async def test_session_supervisor_name_with_no_row_is_sleeping(self, lens):
        got = await lens.activity(
            kind="session", target_id="supervisor-proj1", project_id="proj1"
        )
        assert got == "sleeping"

    async def test_running_task_with_recent_activity_is_busy(self, db, providers, lens):
        # Set up a live session row + a live fake provider entry with
        # activity within the busy window.
        row, handle = await _seed_running_task(db, providers)
        s = providers.create("fake").sessions[handle.name]
        s.activity = time.time()  # fresh
        got = await lens.activity(kind="task", target_id=row.task_id, project_id="proj1")
        assert got == "busy"

    async def test_running_task_with_stale_activity_is_idle(self, db, providers, lens):
        row, handle = await _seed_running_task(db, providers)
        s = providers.create("fake").sessions[handle.name]
        s.activity = time.time() - 300  # well past the 30s busy window
        got = await lens.activity(kind="task", target_id=row.task_id, project_id="proj1")
        assert got == "idle"

    async def test_session_kind_resolves_supervisor_by_address(self, db, providers, lens):
        # The messaging *address* is ``supervisor-<pid>``; the row is
        # named ``n-supervisor--<pid>``. Lens must translate and still
        # find the row + handle.
        row, handle = await _seed_running_supervisor(db, providers, project_id="proj1")
        assert row.name == _supervisor_runtime_name("proj1")
        s = providers.create("fake").sessions[handle.name]
        s.activity = time.time()
        got = await lens.activity(
            kind="session", target_id=_supervisor_address("proj1"), project_id="proj1"
        )
        assert got == "busy"

    async def test_session_kind_resolves_task_session_by_name(
        self, db, providers, lens
    ):
        # A ``to_kind="session"`` with a task session's name should resolve
        # the same row that ``kind="task"`` would.
        row, handle = await _seed_running_task(db, providers)
        s = providers.create("fake").sessions[handle.name]
        s.activity = time.time() - 300
        got = await lens.activity(
            kind="session", target_id=row.name, project_id="proj1"
        )
        assert got == "idle"


# ---------------------------------------------------------------------------
# ensure_started()
# ---------------------------------------------------------------------------


class TestEnsureStarted:
    async def test_refuses_task_kind(self, lens):
        assert await lens.ensure_started(
            kind="task", target_id="t1", project_id="proj1"
        ) is False

    async def test_refuses_plain_session_name(self, lens):
        # Only ``supervisor-<pid>`` names are wake-on-demand.
        assert await lens.ensure_started(
            kind="session", target_id="s-task-1", project_id="proj1"
        ) is False

    async def test_supervisor_already_running_returns_true(self, db, providers, lens):
        # ensure_started is addressed with the messaging address
        # (``supervisor-<pid>``); the row it finds is
        # ``n-supervisor--<pid>``.
        await _seed_running_supervisor(db, providers, project_id="proj1")
        assert await lens.ensure_started(
            kind="session",
            target_id=_supervisor_address("proj1"),
            project_id="proj1",
        ) is True

    async def test_supervisor_cold_start_spawns_via_provider(self, providers, lens):
        # The messaging *address* is ``supervisor-proj1``; the runtime
        # session the provider is asked to start is named
        # ``n-supervisor--proj1`` so the session reconciler can adopt it
        # after a daemon restart (adoption only scans ``s-``/``n-``
        # prefixes — see src/sessions/reconciler.py adopt_on_start).
        address = _supervisor_address("proj1")
        runtime_name = _supervisor_runtime_name("proj1")
        assert await lens.ensure_started(
            kind="session", target_id=address, project_id="proj1"
        ) is True
        fake = providers.create("fake")
        assert len(fake.starts) == 1
        assert fake.starts[0].session_name == runtime_name
        # session_id passed to the harness must be a dashed UUID (5 hex groups),
        # never bare hex — `claude --session-id` rejects bare hex.
        argv = fake.starts[0].command
        i = argv.index("--session-id")
        sid = argv[i + 1]
        assert sid.count("-") == 4, f"session_id must be dashed uuid, got {sid!r}"

        # The persisted sessions row must mirror execution.py canon:
        # row id == spec/harness session id (not a fabricated hex),
        # session_key == same (the harness resume key),
        # epoch propagated from the daemon,
        # profile_id resolved from the loaded profile (not hardcoded),
        # project_id non-empty.
        row = await lens._db.get_session_by_name(runtime_name)
        assert row is not None, "cold-start must insert a sessions row"
        assert row.id == sid, "row id must equal the spec/harness session id"
        assert row.session_key == sid, "session_key is the harness resume key"
        assert row.epoch == TEST_EPOCH
        assert row.project_id == "proj1"
        assert row.profile_id == "supervisor"
        assert row.state == "running"

    async def test_refuses_when_project_id_missing(self, providers, lens):
        # A bare ``supervisor-`` address (empty suffix) with no project_id
        # fallback means we can't identify the project. Refuse before we
        # start anything, so we never insert a phantom ``project_id=""`` row.
        ok = await lens.ensure_started(
            kind="session", target_id="supervisor-", project_id=None
        )
        assert ok is False
        assert providers.create("fake").starts == []

    async def test_supervisor_cold_start_derives_project_from_name(
        self, providers, lens
    ):
        # ``project_id=None`` but the address carries the project id —
        # the lens should derive it rather than skip on empty work_dir.
        address = _supervisor_address("proj1")
        runtime_name = _supervisor_runtime_name("proj1")
        assert await lens.ensure_started(
            kind="session", target_id=address, project_id=None
        ) is True
        starts = providers.create("fake").starts
        assert len(starts) == 1
        assert starts[0].session_name == runtime_name

    async def test_returns_false_when_configured_provider_missing(
        self, db, providers, spec_builder, harness_registry, profiles_loader
    ):
        # Production must not silently fall back to ``fake`` when the
        # operator has configured a different provider that isn't loaded:
        # that would mask a misconfigured host. Lens should log and return
        # False so the delivery engine leaves the supervisor asleep.
        class _MissingSessions:
            provider = "subprocess-not-loaded"

        class _Cfg:
            vault_root = "/tmp/vault"
            mcp_server = None
            sessions = _MissingSessions()

        lens = SessionLens(
            db=db,
            providers=providers,  # only "fake" registered
            spec_builder=spec_builder,
            harness_registry=harness_registry,
            config=_Cfg(),
            profiles_loader=profiles_loader,
        )
        name = _supervisor_name("proj1")
        ok = await lens.ensure_started(
            kind="session", target_id=name, project_id="proj1"
        )
        assert ok is False
        # No start was recorded on the fake provider.
        assert providers.create("fake").starts == []

    async def test_returns_false_when_work_dir_would_be_empty(
        self, db, providers, spec_builder, harness_registry, profiles_loader
    ):
        # No vault_root AND no project_id derivable → _supervisor_work_dir
        # returns "". Passing that into the harness would produce a
        # confusing downstream failure; instead skip cleanly.
        class _Sessions:
            provider = "fake"

        class _Cfg:
            vault_root = ""
            mcp_server = None
            sessions = _Sessions()

        lens = SessionLens(
            db=db,
            providers=providers,
            spec_builder=spec_builder,
            harness_registry=harness_registry,
            config=_Cfg(),
            profiles_loader=profiles_loader,
            epoch=TEST_EPOCH,
        )
        # Valid project id (so the earlier "missing project" refusal does
        # not preempt this test) but no ``vault_root`` — the work_dir
        # helper still can't compose a meaningful directory.
        ok = await lens.ensure_started(
            kind="session", target_id="supervisor-proj1", project_id="proj1"
        )
        assert ok is False
        assert providers.create("fake").starts == []


# ---------------------------------------------------------------------------
# nudge()
# ---------------------------------------------------------------------------


class TestNudge:
    async def test_nudge_delivers_and_records(self, db, providers, lens):
        row, handle = await _seed_running_task(db, providers)
        ok = await lens.nudge(
            kind="task", target_id=row.task_id, project_id="proj1", text="hello"
        )
        assert ok is True
        fake = providers.create("fake")
        assert fake.sent_nudges == [(handle.name, "hello")]

    async def test_nudge_returns_false_on_notsubmitted(self, db, providers, lens):
        row, handle = await _seed_running_task(db, providers)
        providers.create("fake").swallow_next_nudge(handle.name)
        ok = await lens.nudge(
            kind="task", target_id=row.task_id, project_id="proj1", text="hi"
        )
        assert ok is False

    async def test_nudge_returns_false_when_no_session(self, lens):
        ok = await lens.nudge(
            kind="task", target_id="never-existed", project_id="proj1", text="hi"
        )
        assert ok is False

    async def test_nudge_by_session_name(self, db, providers, lens):
        # Plain (non-supervisor) session names pass through the lens
        # verbatim — they are already real runtime names.
        row, handle = await _seed_running_supervisor(db, providers, project_id="proj1")
        ok = await lens.nudge(
            kind="session", target_id=row.name, project_id="proj1", text="wake"
        )
        assert ok is True
        assert providers.create("fake").sent_nudges == [(handle.name, "wake")]

    async def test_nudge_translates_supervisor_address(self, db, providers, lens):
        # ``supervisor-<pid>`` is translated to ``n-supervisor--<pid>``
        # before the DB lookup; the same running session is reached.
        row, handle = await _seed_running_supervisor(db, providers, project_id="proj1")
        assert row.name == _supervisor_runtime_name("proj1")
        ok = await lens.nudge(
            kind="session",
            target_id=_supervisor_address("proj1"),
            project_id="proj1",
            text="wake",
        )
        assert ok is True
        assert providers.create("fake").sent_nudges == [(handle.name, "wake")]


# ---------------------------------------------------------------------------
# tail_assistant_turn()
# ---------------------------------------------------------------------------


class TestTailAssistantTurn:
    """Exercise the real ClaudeTranscriptReader.

    The reader's ``base_dir`` defaults to ``Path.home()``; we redirect it
    to ``tmp_path`` by pointing HOME at tmp_path (Path.home() honors HOME).
    Then we plant a JSONL file exactly where Claude would write one, in
    the shape the reader parses (see src/sessions/transcripts/claude.py:97
    ``_entry_from_line`` — needs ``type``, ``uuid``, ``timestamp``,
    ``message.content``).
    """

    @staticmethod
    def _write_transcript(
        tmp_path, work_dir: str, session_key: str, entries: list[dict]
    ):
        from src.sessions.transcripts.claude import slug_work_dir

        slug = slug_work_dir(work_dir)
        proj = tmp_path / ".claude" / "projects" / slug
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / f"{session_key}.jsonl"
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e))
                f.write("\n")
        return path

    async def _seed_task_with_transcript(
        self, db, providers, work_dir, session_key
    ):
        """Sessions row wired to the given work_dir + session_key."""
        from src.sessions.provider import SessionSpec

        fake = providers.create("fake")
        task_id = "task-T"
        await db.create_task(
            Task(id=task_id, project_id="proj1", title="t", description="d")
        )
        session_name = f"s-{task_id}"
        spec = SessionSpec(
            session_name=session_name,
            work_dir=work_dir,
            command=("claude",),
            instance_token="tok-t",
        )
        await fake.start(spec)
        row = SessionRecord(
            id="sess-T",
            project_id="proj1",
            profile_id="claude",
            harness="claude",
            provider="fake",
            name=session_name,
            lifecycle="task",
            work_dir=work_dir,
            epoch="e1",
            instance_token="tok-t",
            started_at=time.time(),
            task_id=task_id,
            session_key=session_key,
            state="running",
        )
        await db.create_session(row)
        return row

    async def test_returns_assistant_text_newer_than_since(
        self, db, providers, lens, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        work_dir = "/tmp/wd-tail-a"
        session_key = "abc-123"
        # One old user, one old assistant, one fresh assistant. We want
        # ``since`` between the old assistant and the fresh one.
        old_ts = "2024-01-01T00:00:00Z"
        new_ts = "2030-01-01T00:00:00Z"
        entries = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "timestamp": old_ts,
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "timestamp": old_ts,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old reply"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "a1",
                "timestamp": new_ts,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "fresh reply"}],
                },
            },
        ]
        self._write_transcript(tmp_path, work_dir, session_key, entries)
        row = await self._seed_task_with_transcript(
            db, providers, work_dir, session_key
        )

        # since = between the two assistant entries (2024 vs 2030).
        import datetime as _dt

        since = _dt.datetime(
            2025, 1, 1, tzinfo=_dt.timezone.utc
        ).timestamp()
        got = await lens.tail_assistant_turn(
            kind="task", target_id=row.task_id, project_id="proj1", since=since
        )
        assert got == "fresh reply"

    async def test_returns_none_when_nothing_newer_than_since(
        self, db, providers, lens, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        work_dir = "/tmp/wd-tail-b"
        session_key = "def-456"
        old_ts = "2020-01-01T00:00:00Z"
        entries = [
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": None,
                "timestamp": old_ts,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "stale"}],
                },
            },
        ]
        self._write_transcript(tmp_path, work_dir, session_key, entries)
        row = await self._seed_task_with_transcript(
            db, providers, work_dir, session_key
        )

        # since = now → the 2020 entry is not newer.
        got = await lens.tail_assistant_turn(
            kind="task",
            target_id=row.task_id,
            project_id="proj1",
            since=time.time(),
        )
        assert got is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed_running_task(db, providers) -> tuple[SessionRecord, SessionHandle]:
    """Create a task session on the fake provider *and* the sessions row."""
    from src.sessions.provider import SessionSpec

    fake = providers.create("fake")
    task_id = "task-A"
    await db.create_task(Task(id=task_id, project_id="proj1", title="t", description="d"))
    session_name = f"s-{task_id}"
    spec = SessionSpec(
        session_name=session_name,
        work_dir="/tmp/wd",
        command=("claude",),
        instance_token="tok-task",
    )
    handle = await fake.start(spec)
    row = SessionRecord(
        id="sess-task",
        project_id="proj1",
        profile_id="claude",
        harness="claude",
        provider="fake",
        name=session_name,
        lifecycle="task",
        work_dir="/tmp/wd",
        epoch="e1",
        instance_token="tok-task",
        started_at=time.time(),
        task_id=task_id,
        state="running",
    )
    await db.create_session(row)
    return row, handle


async def _seed_running_supervisor(
    db, providers, *, project_id: str
) -> tuple[SessionRecord, SessionHandle]:
    from src.sessions.provider import SessionSpec

    fake = providers.create("fake")
    # DB row + provider entry are keyed on the *runtime* session name
    # (``n-supervisor--<pid>``); the messaging address is
    # ``supervisor-<pid>`` and the lens translates one to the other.
    name = _supervisor_runtime_name(project_id)
    spec = SessionSpec(
        session_name=name,
        work_dir="/tmp/wd",
        command=("claude",),
        instance_token="tok-sup",
    )
    handle = await fake.start(spec)
    row = SessionRecord(
        id="sess-sup",
        project_id=project_id,
        profile_id="supervisor",
        harness="claude",
        provider="fake",
        name=name,
        lifecycle="named",
        work_dir="/tmp/wd",
        epoch="e1",
        instance_token="tok-sup",
        started_at=time.time(),
        state="running",
    )
    await db.create_session(row)
    return row, handle
