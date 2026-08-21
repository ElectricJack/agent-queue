"""P3 end-to-end delivery integration test — supervisor-agent §5/§7/§8.

Drives the *real* :class:`~src.orchestrator.core.Orchestrator` with
FakeProvider-backed sessions and ``messages.enabled=True``, then exercises
the three delivery paths the P3 wave promises end-to-end:

1. **Nudge into a sleeping supervisor.**  A ``user → session:supervisor-<pid>``
   message triggers :meth:`SessionLens.ensure_started`, spins up a fake
   supervisor session, and the cascade nudges the message in and marks it
   delivered ``via="nudge"``.

2. **Prime picks up a message parked against a busy task.**  A
   ``user → task:<id>`` message finds the task's session mid-turn, is
   skipped by the delivery pass (``skipped_busy``), then the next
   ``aq prime`` call for that task claims it via CAS with ``via="prime"``.

3. **Reply thread.**  A reply flows back on the supervisor thread and
   ``message.list`` (command layer) surfaces both sides of the exchange.

The test uses ``run_one_cycle`` where cheap and calls the specific cascade
step (``_deliver_messages``) where the wider cycle would drag in irrelevant
work.  Both are on the real ``Orchestrator`` instance — no stubs.
"""

from __future__ import annotations

import time

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.runtimes.base import Runtime
from src.sessions.provider import SessionSpec
from src.sessions.spec import named_session_name


# ---------------------------------------------------------------------------
# Minimal runtime factory (no task work exercised, but Orchestrator wants one)
# ---------------------------------------------------------------------------


class _NullRuntimeFactory:
    def create(self, agent_type, profile=None, llm_logger=None) -> Runtime:  # pragma: no cover
        raise AssertionError("no task execution in this integration test")


# ---------------------------------------------------------------------------
# Fixture: real Orchestrator with messages+sessions on, FakeProvider selected.
# ---------------------------------------------------------------------------


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    # Session runtime + messages: both on.  Provider "fake" is the in-tree
    # in-memory provider — ``default_session_registry`` always registers it.
    config.sessions.enabled = True
    config.sessions.provider = "fake"
    config.messages.enabled = True
    # Keep the P3 throttle from silently swallowing the second pass we drive
    # below (test walks wall-clock forward across ~3 passes).
    config.messages.delivery_interval = 0.0
    # Legacy path — nothing in this test spawns worktrees or real repos.
    config.worktrees.enabled = False

    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()
    # Seed the supervisor profile the SessionLens needs to cold-start the
    # supervisor session (spec §6: only supervisor-named sessions are
    # wake-on-demand).
    # ``upsert`` because the migration may have already seeded a
    # supervisor row on ``initialize`` — either way, ensure it's harness
    # ``claude`` (matches the harness registered below) and lifecycle
    # ``named`` (what the lens' wake-on-demand check requires).
    await o.db.upsert_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="claude",
            lifecycle="named",
        )
    )
    # The lens picks a harness by name; register one so the spec builder
    # can compose the (unused, fake-provider) launch command.
    from src.sessions.harness_parser import Harness

    o.harness_registry.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    yield o
    await o.shutdown()


@pytest.fixture
def handler(orch):
    """CommandHandler bound to the real orchestrator (message + prime paths)."""
    h = CommandHandler(orch, orch.config)
    h._active_project_id = None
    return h


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


PROJECT_ID = "p-int"


async def _seed_project(orch):
    await orch.db.create_project(Project(id=PROJECT_ID, name="integration"))


def _supervisor_address(project_id: str = PROJECT_ID) -> str:
    return f"supervisor-{project_id}"


def _supervisor_runtime_name(project_id: str = PROJECT_ID) -> str:
    return named_session_name("supervisor", project_id)


async def _seed_busy_task_session(orch, *, task_id: str = "t-busy"):
    """Task with a live FakeProvider session whose activity is fresh (busy)."""
    await orch.db.create_task(
        Task(
            id=task_id,
            project_id=PROJECT_ID,
            title="busy",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    fake = orch.session_providers.create("fake")
    session_name = f"s-{task_id}"
    handle = await fake.start(
        SessionSpec(
            session_name=session_name,
            work_dir=str(orch.config.workspace_dir),
            command=("claude",),
            instance_token="tok-busy",
        )
    )
    fake.sessions[handle.name].activity = time.time()  # fresh → busy
    await orch.db.create_session(
        SessionRecord(
            id=f"sess-{task_id}",
            project_id=PROJECT_ID,
            profile_id="claude",
            harness="claude",
            provider="fake",
            name=session_name,
            lifecycle="task",
            work_dir=str(orch.config.workspace_dir),
            epoch="e1",
            instance_token="tok-busy",
            started_at=time.time(),
            task_id=task_id,
            state="running",
        )
    )
    return handle


# ---------------------------------------------------------------------------
# 1. Nudge into a sleeping supervisor
# ---------------------------------------------------------------------------


class TestUserToSupervisorNudge:
    async def test_wakes_supervisor_and_nudges(self, orch, handler):
        await _seed_project(orch)

        # user → session:supervisor-<pid> — the address the messaging layer
        # uses (spec §5); the lens translates it to the ``n-supervisor--``
        # runtime name so the reconciler can adopt it later.
        result = await handler.execute(
            "message_send",
            {
                "project_id": PROJECT_ID,
                "to_kind": "session",
                "to_id": _supervisor_address(),
                "from_kind": "user",
                "from_id": "discord:1",
                "body": "what's the status?",
                "thread_id": "t-int-1",
            },
        )
        assert "error" not in result, result
        message_id = result["message_id"]

        # No supervisor row exists → activity = "sleeping"; delivery pass
        # cold-starts one via SessionLens.ensure_started, then nudges.
        await orch._deliver_messages()

        fake = orch.session_providers.create("fake")
        runtime_name = _supervisor_runtime_name()
        assert runtime_name in fake.sessions, (
            f"supervisor cold-start expected — sessions={list(fake.sessions)}"
        )
        # One nudge, addressed to the runtime name (not the messaging address).
        assert len(fake.sent_nudges) == 1
        nudged_name, nudged_text = fake.sent_nudges[0]
        assert nudged_name == runtime_name
        assert message_id in nudged_text
        assert "what's the status?" in nudged_text

        stored = await orch.db.get_message(message_id)
        assert stored.delivered_at is not None
        assert stored.via == "nudge"


# ---------------------------------------------------------------------------
# 2. Busy task session → stays pending → prime claims it
# ---------------------------------------------------------------------------


class TestBusySessionThenPrime:
    async def test_pending_across_pass_then_delivered_via_prime(self, orch, handler):
        await _seed_project(orch)
        await _seed_busy_task_session(orch, task_id="t-busy")

        send_result = await handler.execute(
            "message_send",
            {
                "project_id": PROJECT_ID,
                "to_kind": "task",
                "to_id": "t-busy",
                "from_kind": "user",
                "from_id": "discord:1",
                "body": "heads up while you work",
            },
        )
        assert "error" not in send_result, send_result
        msg_id = send_result["message_id"]

        # Delivery pass: recipient is busy → skipped, message untouched.
        await orch._deliver_messages()
        stored = await orch.db.get_message(msg_id)
        assert stored.delivered_at is None, "busy recipient should not be delivered by nudge"

        # A second pass, still busy — still pending (guards against a
        # nudge sneaking in on the retry loop).
        await orch._deliver_messages()
        stored = await orch.db.get_message(msg_id)
        assert stored.delivered_at is None

        # aq prime for the same task claims it via CAS with via="prime".
        prime_result = await handler.execute("prime", {"task_id": "t-busy"})
        assert prime_result.get("success") is True, prime_result

        stored = await orch.db.get_message(msg_id)
        assert stored.delivered_at is not None
        assert stored.via == "prime"

        # And the rendered document actually contains the message envelope
        # (proves prime read the row it just marked).
        assert msg_id in prime_result["body"]
        assert "heads up while you work" in prime_result["body"]


# ---------------------------------------------------------------------------
# 3. Reply flows back and message.list shows the thread
# ---------------------------------------------------------------------------


class TestReplyThread:
    async def test_reply_appears_in_message_list_thread(self, orch, handler):
        await _seed_project(orch)

        send = await handler.execute(
            "message_send",
            {
                "project_id": PROJECT_ID,
                "to_kind": "session",
                "to_id": _supervisor_address(),
                "from_kind": "user",
                "from_id": "discord:1",
                "body": "please summarize",
                "thread_id": "th-reply",
            },
        )
        assert "error" not in send, send
        original_id = send["message_id"]

        # Deliver the original (this cold-starts the supervisor + nudges).
        await orch._deliver_messages()
        assert (await orch.db.get_message(original_id)).delivered_at is not None

        # Supervisor replies through the command layer (what an agent-side
        # ``aq reply`` invocation ultimately calls).
        reply_result = await handler.execute(
            "message_reply",
            {
                "message_id": original_id,
                "body": "here is the summary",
                # Explicit from_id keeps the reply's identity legible;
                # otherwise the mixin falls back to original.to_id
                # (``supervisor-p-int``) which is fine too.
                "from_id": _supervisor_address(),
            },
        )
        assert "error" not in reply_result, reply_result
        reply_id = reply_result["reply_id"]

        # message.list scoped to the thread returns both rows, threaded.
        listing = await handler.execute(
            "message_list",
            {
                "project_id": PROJECT_ID,
                "thread_id": "th-reply",
            },
        )
        assert "error" not in listing, listing
        ids = {m["id"] for m in listing["messages"]}
        assert original_id in ids
        assert reply_id in ids

        # The reply links back to the original (spec §6.2 mirror rule).
        reply_row = next(m for m in listing["messages"] if m["id"] == reply_id)
        assert reply_row["reply_to_id"] == original_id
        assert reply_row["to_kind"] == "user"
        assert reply_row["to_id"] == "discord:1"
        assert reply_row["body"] == "here is the summary"
