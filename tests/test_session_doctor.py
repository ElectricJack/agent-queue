"""``sessions.stuck_composer`` — the doctor half of the lost-Enter stall.

The live symptom (2026-09-02): a stall nudge sat in a Claude composer,
unsubmitted, and one manual ``tmux send-keys Enter`` cleared it instantly.
This check is the operator surface for exactly that: report which sessions
are holding a nudge nobody submitted, and press Enter with ``--fix``.
"""

from __future__ import annotations

import sys
import time

import pytest

import src.doctor
from src.doctor.models import Severity
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.provider import NotSubmitted, SessionHandle, SessionSpec
from tests.db_fixtures import lease_dsn

# ``src/doctor/__init__.py`` rebinds the package attribute ``session_checks``
# to the *factory function*, so the submodule is only reachable through
# ``sys.modules`` (see the same note in tests/test_pool_doctor.py).
session_checks = sys.modules["src.doctor.session_checks"]

PROJECT_ID = "proj"
CHECK = "sessions.stuck_composer"
NUDGE = "No progress for 8 min on task t1. Close or continue: ..."


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool"))
    yield database
    await database.close()


class _Handler:
    def __init__(self, provider):
        registry = SessionProviderRegistry({"fake": FakeProvider})
        registry._instances["fake"] = provider
        self.orchestrator = type(
            "_Orch", (), {"session_providers": registry, "config": None}
        )()


async def _running_session(db, provider, *, state="running"):
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    row = SessionRecord(
        id="s1",
        project_id=PROJECT_ID,
        profile_id="worker",
        harness="claude",
        provider="fake",
        name="p-worker--proj--abc",
        lifecycle="pool",
        work_dir="/w",
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        state=state,
        task_id="t1",
    )
    await db.create_session(row)
    await provider.start(
        SessionSpec(
            session_name=row.name,
            work_dir=row.work_dir,
            command=("agent",),
            instance_token=row.instance_token,
        )
    )
    return row


def _handle(row):
    return SessionHandle(name=row.name, provider="fake", instance_token=row.instance_token)


def test_the_check_is_registered_with_a_fix():
    check = session_checks.CHECKS[CHECK]
    assert check.owner == "session-runtime"
    assert check.fix is not None
    assert CHECK in {c.id for c in src.doctor.default_registry().checks()}


class TestStuckComposerCheck:
    async def test_clean_sessions_report_ok(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        await provider.nudge(_handle(row), NUDGE)

        result = await session_checks.run_check(db, _Handler(provider), CHECK)

        assert result.severity is Severity.OK
        assert result.fixable is True

    async def test_an_unsubmitted_nudge_is_reported_with_the_task(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        provider.swallow_next_nudge(row.name)
        with pytest.raises(NotSubmitted):
            await provider.nudge(_handle(row), NUDGE)

        result = await session_checks.run_check(db, _Handler(provider), CHECK)

        assert result.severity is Severity.WARN
        assert result.data["count"] == 1
        assert result.data["sessions"][0]["task_id"] == "t1"
        assert result.data["sessions"][0]["evidence"] == "provider_pending_submit"
        assert row.name in result.detail
        assert "t1" in result.detail

    async def test_fix_presses_enter_and_the_recheck_goes_green(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        provider.swallow_next_nudge(row.name)
        with pytest.raises(NotSubmitted):
            await provider.nudge(_handle(row), NUDGE)

        result = await session_checks.run_check(db, _Handler(provider), CHECK, repair=True)

        assert result.severity is Severity.OK
        assert result.fix_applied is True
        assert provider.sent_nudges == [(row.name, NUDGE)]

    async def test_a_check_run_without_fix_never_presses_a_key(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        provider.swallow_next_nudge(row.name)
        with pytest.raises(NotSubmitted):
            await provider.nudge(_handle(row), NUDGE)

        await session_checks.run_check(db, _Handler(provider), CHECK)

        assert provider.sent_nudges == []

    async def test_no_orchestrator_is_not_an_error(self, db):
        result = await session_checks.run_check(db, None, CHECK)
        assert result.severity is Severity.OK

    async def test_aq_text_the_guard_cannot_read_is_reported_and_never_submitted(self, db):
        """2026-09-27: Codex 0.157 layouts left reminders typed but never
        submitted, and this check reported OK because it only listed text it
        could attribute exactly.  An unreadable record blocks every later
        wake just the same; it is reported, and --fix never presses Enter."""
        from src.sessions import tmux as tmux_module
        from tests.test_tmux_nudge_drafts import Composer, provider_for

        row = await _running_session(db, FakeProvider())
        text = "Handle `aq message status msg-1 --json`."
        composer = Composer(draft=text, below=["", "  tab to queue message    91% context left"])
        composer.typed = True
        tmux = provider_for(composer)
        await tmux._remember_pending(
            _handle(row),
            tmux_module._PendingSubmit(
                instance_token=row.instance_token,
                marker=tmux_module._marker_for(text),
                text=text,
            ),
        )
        composer.mutations.clear()

        result = await session_checks.run_check(db, _Handler(tmux), CHECK)
        assert result.severity is Severity.WARN
        assert result.data["unreadable"] == 1
        assert result.data["sessions"][0]["observable"] is False
        assert "cannot read" in result.detail and row.name in result.detail

        repaired = await session_checks.run_check(db, _Handler(tmux), CHECK, repair=True)
        assert repaired.severity is Severity.WARN
        assert composer.submitted == []
        assert not any(args[0] == "send-keys" for args in composer.mutations)


    async def test_aq_text_the_composer_collapsed_is_cleared_never_submitted(
        self, db, monkeypatch
    ):
        """2026-10-01: a task comment typed into a Claude worker showed as
        ``[Pasted text #1 +6 lines]``, unsubmittable, and held back every later
        wake.  The check names it clearable; --fix clears it -- no Enter -- and
        the recheck goes green.  The message behind it stays queued."""
        from src.sessions import tmux as tmux_module
        from tests.test_tmux_nudge_drafts import provider_for
        from tests.test_tmux_nudge_recovery import COLLAPSED_COMMENT, ClaudeComposer

        monkeypatch.setattr(tmux_module, "_LANDED_POLL_SECONDS", 0.0)
        monkeypatch.setattr(tmux_module, "_CLEAR_SETTLE_SECONDS", 0.0)
        row = await _running_session(db, FakeProvider())
        composer = ClaudeComposer(clear_keys=())
        tmux = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await tmux.nudge(_handle(row), COLLAPSED_COMMENT)
        composer.environment["AQ_CLEAR_KEYS"] = "C-u"  # the operator added the key

        result = await session_checks.run_check(db, _Handler(tmux), CHECK)
        assert result.severity is Severity.WARN
        assert result.data["sessions"][0]["clearable"] is True
        assert "--fix clears it" in result.detail and row.name in result.detail

        repaired = await session_checks.run_check(db, _Handler(tmux), CHECK, repair=True)
        assert repaired.severity is Severity.OK
        assert repaired.fix_applied is True
        assert composer.chunks == [] and composer.submitted == []
        assert "Enter" not in composer.sent_keys()


# ---------------------------------------------------------------------------
# messages.idle_worker_backlog
# ---------------------------------------------------------------------------

BACKLOG = "messages.idle_worker_backlog"


class _LensHandler:
    """An orchestrator stand-in carrying the delivery engine's own lens."""

    def __init__(self, db, provider):
        from src.messages.session_lens import SessionLens
        from src.sessions.harness_registry import HarnessRegistry
        from src.sessions.spec import SessionSpecBuilder

        registry = SessionProviderRegistry({"fake": FakeProvider})
        registry._instances["fake"] = provider
        config = type("_Cfg", (), {"vault_root": "/tmp/vault", "mcp_server": None})()
        harnesses = HarnessRegistry()

        async def _profiles(_profile_id):
            return None

        lens = SessionLens(
            db=db,
            providers=registry,
            spec_builder=SessionSpecBuilder(config, harnesses),
            harness_registry=harnesses,
            config=config,
            profiles_loader=_profiles,
        )
        self.orchestrator = type(
            "_Orch", (), {"session_providers": registry, "config": None, "session_lens": lens}
        )()


async def _worker(db, provider, *, idle: bool):
    """A live task-lifecycle worker on the fake provider (harness without transcripts)."""
    await db.create_task(
        Task(id="t1", project_id=PROJECT_ID, title="t", description="d",
             status=TaskStatus.IN_PROGRESS)
    )
    row = SessionRecord(
        id="s1", project_id=PROJECT_ID, profile_id="worker", harness="fake",
        provider="fake", name="s-t1", lifecycle="task", work_dir="/w", epoch="e",
        instance_token="tok", started_at=time.time(), state="running", task_id="t1",
    )
    await db.create_session(row)
    await provider.start(
        SessionSpec(session_name=row.name, work_dir=row.work_dir, command=("agent",),
                    instance_token=row.instance_token)
    )
    if idle:
        provider.sessions[row.name].activity = time.time() - 120
    return row


async def _message(db, *, to_kind, to_id, age_s, body_kind=None):
    from sqlalchemy import update

    from src.database.tables import messages

    msg = await db.create_message(
        project_id=PROJECT_ID, from_kind="system", from_id="supervisor",
        to_kind=to_kind, to_id=to_id, body="guidance", body_kind=body_kind,
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            update(messages).where(messages.c.id == msg.id)
            .values(created_at=time.time() - age_s)
        )
    return msg


def test_the_backlog_check_is_registered():
    check = session_checks.CHECKS[BACKLOG]
    assert check.owner == "session-runtime"
    assert check.fix is None
    assert BACKLOG in {c.id for c in src.doctor.default_registry().checks()}


class TestIdleWorkerBacklog:
    async def test_old_mail_for_an_idle_task_and_session_is_listed_with_the_reason(self, db):
        """2026-09-27: idle workers sat 15-30 min on supervisor answers."""
        from src.sessions.provider import NudgeDeferred

        provider = FakeProvider()
        row = await _worker(db, provider, idle=True)
        to_task = await _message(db, to_kind="task", to_id="t1", age_s=400)
        to_session = await _message(db, to_kind="session", to_id="s1", age_s=360)
        await _message(db, to_kind="task", to_id="t1", age_s=30)  # too fresh to report
        handler = _LensHandler(db, provider)

        async def refuse(h, text):
            raise NudgeDeferred(f"terminal {h.name!r} has a draft or its input is unknown")

        provider.nudge = refuse
        await handler.orchestrator.session_lens.nudge(
            kind="task", target_id="t1", project_id=PROJECT_ID, text="x"
        )

        result = await session_checks.run_check(db, handler, BACKLOG)

        assert result.severity is Severity.WARN
        listed = {entry["message_id"]: entry for entry in result.data["messages"]}
        assert set(listed) == {to_task.id, to_session.id}
        entry = listed[to_task.id]
        assert (entry["to_kind"], entry["to_id"], entry["session_id"]) == ("task", "t1", "s1")
        assert entry["task_id"] == "t1"
        assert entry["age_seconds"] >= 400
        assert entry["last_nudge_failure"]["reason"].endswith("its input is unknown")
        assert row.name in result.detail

    async def test_busy_workers_and_delivered_mail_are_not_reported(self, db):
        provider = FakeProvider()
        await _worker(db, provider, idle=False)
        await _message(db, to_kind="task", to_id="t1", age_s=400)
        delivered = await _message(db, to_kind="session", to_id="s1", age_s=400)
        await db.mark_delivered(delivered.id, via="nudge")

        result = await session_checks.run_check(db, _LensHandler(db, provider), BACKLOG)

        assert result.severity is Severity.OK

    async def test_mail_for_an_idle_worker_that_is_now_delivered_clears(self, db):
        provider = FakeProvider()
        await _worker(db, provider, idle=True)
        msg = await _message(db, to_kind="task", to_id="t1", age_s=400)
        await db.mark_delivered(msg.id, via="nudge")

        result = await session_checks.run_check(db, _LensHandler(db, provider), BACKLOG)

        assert result.severity is Severity.OK

    async def test_without_an_orchestrator_the_backlog_is_informational(self, db):
        provider = FakeProvider()
        await _worker(db, provider, idle=True)
        msg = await _message(db, to_kind="task", to_id="t1", age_s=400)

        result = await session_checks.run_check(db, None, BACKLOG)

        assert result.severity is Severity.INFO
        assert [entry["message_id"] for entry in result.data["messages"]] == [msg.id]


# ---------------------------------------------------------------------------
# sessions.stall_unreachable
# ---------------------------------------------------------------------------

UNREACHABLE = "sessions.stall_unreachable"
PAINTED = (
    "[fast-jev:opencode 2026-09-27T10:14:22.594Z] session ses_f1dc: 64 msgs, calls=85 "
    "kept=0 dropped=56"
)


async def _stalled(db, provider, *, idle_s=20 * 60, claim_phase="active"):
    row = await _running_session(db, provider)
    await db.update_session(row.id, last_activity=time.time() - idle_s, claim_phase=claim_phase)
    return row


def test_the_unreachable_check_is_registered_read_only():
    check = session_checks.CHECKS[UNREACHABLE]
    assert check.owner == "session-runtime"
    assert check.fix is None
    assert UNREACHABLE in {c.id for c in src.doctor.default_registry().checks()}


class TestStallUnreachable:
    """2026-09-26/27: OpenCode workers idle 20+ minutes, the stall nudge
    deferred on every tick without a trace.  The check names each stalled
    task holder whose composer refuses the nudge, and what it shows."""

    async def test_a_stalled_holder_whose_composer_refuses_is_reported(self, db):
        provider = FakeProvider()
        row = await _stalled(db, provider)
        provider.script_composer_refusal(row.name, "has a draft or its input is unknown", PAINTED)

        result = await session_checks.run_check(db, _Handler(provider), UNREACHABLE)

        assert result.severity is Severity.WARN
        [entry] = result.data["sessions"]
        assert (entry["name"], entry["task_id"]) == (row.name, "t1")
        assert entry["idle_seconds"] >= 20 * 60 - 5
        assert entry["reason"] == "has a draft or its input is unknown"
        assert row.name in result.detail and "[fast-jev:opencode" in result.detail
        assert provider.sent_nudges == []

    async def test_the_refusal_is_named_when_the_composer_shows_nothing(self, db):
        provider = FakeProvider()
        row = await _stalled(db, provider)
        provider.script_composer_refusal(row.name, "is busy or its input is unknown")

        result = await session_checks.run_check(db, _Handler(provider), UNREACHABLE)

        assert result.severity is Severity.WARN
        assert "is busy or its input is unknown" in result.detail

    async def test_a_stalled_holder_that_can_be_nudged_is_ok(self, db):
        provider = FakeProvider()
        await _stalled(db, provider)

        result = await session_checks.run_check(db, _Handler(provider), UNREACHABLE)

        assert result.severity is Severity.OK

    @pytest.mark.parametrize(
        ("idle_s", "claim_phase"),
        [(60, "active"), (20 * 60, None)],
        ids=["inside-the-lease", "pool-worker-between-claims"],
    )
    async def test_sessions_the_ladder_would_not_nudge_are_skipped(self, db, idle_s, claim_phase):
        provider = FakeProvider()
        row = await _stalled(db, provider, idle_s=idle_s, claim_phase=claim_phase)
        provider.script_composer_refusal(row.name, "has a draft or its input is unknown")

        result = await session_checks.run_check(db, _Handler(provider), UNREACHABLE)

        assert result.severity is Severity.OK

    async def test_a_holder_in_a_durable_wait_is_skipped(self, db, monkeypatch):
        provider = FakeProvider()
        row = await _stalled(db, provider)
        provider.script_composer_refusal(row.name, "has a draft or its input is unknown")

        async def blocking(session, claim_epoch, now):
            return {"id": "wait-1"}

        monkeypatch.setattr(db, "blocking_wait_for", blocking)
        result = await session_checks.run_check(db, _Handler(provider), UNREACHABLE)

        assert result.severity is Severity.OK

    async def test_a_disabled_ladder_is_informational(self, db):
        from types import SimpleNamespace

        provider = FakeProvider()
        row = await _stalled(db, provider)
        provider.script_composer_refusal(row.name, "has a draft or its input is unknown")
        config = SimpleNamespace(sessions=SimpleNamespace(lease_ttl_seconds=0))

        result = await session_checks.run_check(
            db, _Handler(provider), UNREACHABLE, config=config
        )

        assert result.severity is Severity.INFO

    async def test_no_orchestrator_is_informational(self, db):
        result = await session_checks.run_check(db, None, UNREACHABLE)
        assert result.severity is Severity.INFO

    async def test_an_opencode_box_painted_over_is_named_without_touching_the_pane(self, db):
        from tests.test_tmux_opencode_composer import FAST_JEV, OpenCodePane
        from tests.test_tmux_opencode_composer import provider_for as opencode_provider

        row = await _stalled(db, FakeProvider())
        pane = OpenCodePane(stray=FAST_JEV)

        result = await session_checks.run_check(
            db, _Handler(opencode_provider(pane)), UNREACHABLE
        )

        assert result.severity is Severity.WARN
        [entry] = result.data["sessions"]
        assert entry["name"] == row.name
        assert entry["input"].startswith("[fast-jev:opencode")
        assert pane.mutations == []
