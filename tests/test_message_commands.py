"""Tests for ``MessageCommandsMixin`` — supervisor-agent §6.1 / §12.

Covers the command envelopes, the ``messages.enabled`` gate, and the
``message.*`` event payloads (which must validate against the registry the
same way every other emitter's do).
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.handler import CommandHandler
from src.commands.message_commands import MESSAGES_DISABLED_ERROR, message_to_dict
from src.config import MessagesConfig
from src.database import Database
from src.event_schemas import validate_payload
from src.messages.delivery import MessageDeliveryEngine
from src.models import Agent, AgentState, Message, Project, SessionRecord, Task, TaskStatus
from tests.db_fixtures import lease_dsn


class _RecordingBus:
    """Captures emitted events and validates each against the registry."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        errors = validate_payload(event_type, payload)
        assert not errors, f"{event_type} payload invalid: {errors}"
        self.events.append((event_type, payload))

    def of_type(self, event_type: str) -> list[dict]:
        return [p for t, p in self.events if t == event_type]


@pytest.fixture
async def setup(tmp_path):
    db = Database(lease_dsn("messages.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test"))

    bus = _RecordingBus()
    orch = MagicMock()
    orch.db = db
    orch.bus = bus
    orch._emit_notify = AsyncMock()

    config = MagicMock()
    config.messages = MessagesConfig(enabled=True)

    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    yield handler, db, bus
    await db.close()


def _send_args(**overrides) -> dict:
    args = {
        "project_id": "p1",
        "to_kind": "session",
        "to_id": "supervisor-p1",
        "from_kind": "user",
        "from_id": "discord:1",
        "body": "what is the status?",
    }
    args.update(overrides)
    return args


async def _create_live_worker(db, *, project_id: str, suffix: str) -> SessionRecord:
    agent_id = f"agent-{suffix}"
    task_id = f"task-{suffix}"
    session = SessionRecord(
        id=f"session-{suffix}",
        task_id=task_id,
        agent_id=agent_id,
        project_id=project_id,
        profile_id="worker",
        harness="codex",
        provider="fake",
        name=f"s-{task_id}",
        lifecycle="task",
        work_dir="/tmp",
        epoch="test",
        instance_token=f"token-{suffix}",
        started_at=1,
        state="running",
    )
    await db.create_agent(
        Agent(
            id=agent_id,
            name=f"worker-{suffix}",
            profile_id="worker",
            state=AgentState.BUSY,
        )
    )
    await db.create_task(
        Task(
            id=task_id,
            project_id=project_id,
            title="work",
            description="do work",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id=agent_id,
        )
    )
    await db.create_session(session)
    return session


# ---------------------------------------------------------------------------
# Rollout gate
# ---------------------------------------------------------------------------


class TestDisabledGate:
    @pytest.mark.parametrize(
        "command,args",
        [
            ("_cmd_message_send", _send_args()),
            ("_cmd_message_reply", {"message_id": "msg-1", "body": "hi"}),
            ("_cmd_message_inbox", {"to_kind": "session", "to_id": "s"}),
            ("_cmd_message_list", {}),
        ],
    )
    async def test_every_command_refuses_when_disabled(self, setup, command, args):
        handler, _db, _bus = setup
        handler.config.messages = MessagesConfig(enabled=False)
        result = await getattr(handler, command)(args)
        assert result == {"error": MESSAGES_DISABLED_ERROR}


# ---------------------------------------------------------------------------
# message_send
# ---------------------------------------------------------------------------


class TestSend:
    async def test_creates_row_and_returns_queued(self, setup):
        handler, db, _bus = setup
        result = await handler._cmd_message_send(_send_args(subject="status?"))
        assert result["state"] == "queued"
        stored = await db.get_message(result["message_id"])
        assert stored.body == "what is the status?"
        assert stored.subject == "status?"
        assert stored.delivered_at is None

    async def test_no_success_key_injected(self, setup):
        """House convention: `_cmd_*` returns domain data, not {"success": ...}."""
        handler, _db, _bus = setup
        result = await handler._cmd_message_send(_send_args())
        assert "success" not in result

    async def test_emits_message_sent(self, setup):
        handler, _db, bus = setup
        result = await handler._cmd_message_send(_send_args(thread_id="discord:9"))
        sent = bus.of_type("message.sent")
        assert len(sent) == 1
        assert sent[0]["message_id"] == result["message_id"]
        assert sent[0]["to_id"] == "supervisor-p1"
        assert sent[0]["thread_id"] == "discord:9"

    @pytest.mark.parametrize(
        "override,fragment",
        [
            ({"to_kind": "nowhere"}, "Invalid to_kind"),
            ({"to_id": ""}, "to_id is required"),
            ({"from_kind": "robot"}, "Invalid from_kind"),
            ({"from_id": ""}, "from_id is required"),
            ({"body": "   "}, "body is required"),
            ({"priority": "high"}, "priority must be an integer"),
            ({"project_id": "ghost"}, "not found"),
        ],
    )
    async def test_validation_errors(self, setup, override, fragment):
        handler, _db, _bus = setup
        result = await handler._cmd_message_send(_send_args(**override))
        assert fragment in result["error"]

    async def test_missing_project_without_active(self, setup):
        handler, _db, _bus = setup
        args = _send_args()
        args.pop("project_id")
        result = await handler._cmd_message_send(args)
        assert "project_id is required" in result["error"]

    async def test_unknown_reply_to_id_rejected(self, setup):
        handler, _db, _bus = setup
        result = await handler._cmd_message_send(_send_args(reply_to_id="msg-nope"))
        assert "not found" in result["error"]


_GLOBAL_SUPERVISOR_SCOPE = {
    "kind": "session", "session_id": "global-admin", "project_id": None, "elevated": True,
}


class TestSendRecipientProject:
    """A task, or a session addressed by id, supplies an omitted project.

    Its readers — a durable message wait and ``message_status`` — only see
    rows of the recipient's project (the 2026-09-28 smart-ember miss).
    """

    @pytest.mark.parametrize("to_kind", ["task", "session"])
    async def test_global_supervisor_without_project_uses_the_recipients(self, setup, to_kind):
        handler, db, _bus = setup
        worker = await _create_live_worker(db, project_id="p1", suffix="a")
        to_id = worker.task_id if to_kind == "task" else worker.id
        args = _send_args(to_kind=to_kind, to_id=to_id, body="APPROVED")
        args.pop("project_id")
        result = await handler.execute("message_send", {**args, "_scope": _GLOBAL_SUPERVISOR_SCOPE})
        assert result["message"]["project_id"] == "p1", result

    async def test_recipient_project_beats_the_ambient_active_project(self, setup):
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        worker = await _create_live_worker(db, project_id="p2", suffix="b")
        handler._active_project_id = "p1"
        args = _send_args(to_kind="task", to_id=worker.task_id)
        args.pop("project_id")
        result = await handler._cmd_message_send(args)
        assert result["message"]["project_id"] == "p2"

    async def test_explicit_project_is_kept(self, setup):
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        worker = await _create_live_worker(db, project_id="p2", suffix="c")
        result = await handler._cmd_message_send(
            _send_args(project_id="p1", to_kind="task", to_id=worker.task_id)
        )
        assert result["message"]["project_id"] == "p1"

    @pytest.mark.parametrize(
        ("to_kind", "to_id"), [("task", "task-ghost"), ("session", "supervisor-p1")]
    )
    async def test_unresolved_recipient_keeps_the_projectless_system_default(
        self, setup, to_kind, to_id
    ):
        handler, _db, _bus = setup
        args = _send_args(to_kind=to_kind, to_id=to_id)
        args.pop("project_id")
        result = await handler.execute("message_send", {**args, "_scope": _GLOBAL_SUPERVISOR_SCOPE})
        assert result["message"]["project_id"] is None, result


class TestSendSupervisorAddress:
    """A ``supervisor-<suffix>`` naming no project is stored as a real mailbox.

    2026-10-01 amber-bridge-26: five rows to ``session:supervisor-aq`` (project
    ``agent-queue``) were stored verbatim, so the agent-queue supervisor's own
    ``message_inbox`` — which expands only its name and
    ``supervisor-<own project>`` — never listed them.
    """

    @pytest.mark.parametrize("alias", ["supervisor-aq", "supervisor-"])
    async def test_alias_is_stored_as_the_supervisor_of_the_message_project(
        self, setup, alias
    ):
        handler, db, bus = setup
        result = await handler._cmd_message_send(_send_args(to_id=alias))
        assert result["message"]["to_id"] == "supervisor-p1", result
        assert (await db.get_message(result["message_id"])).to_id == "supervisor-p1"
        assert bus.of_type("message.sent")[0]["to_id"] == "supervisor-p1"

    async def test_alias_resolves_through_the_active_project(self, setup):
        handler, _db, _bus = setup
        handler._active_project_id = "p1"
        args = _send_args(to_id="supervisor-aq")
        args.pop("project_id")
        result = await handler._cmd_message_send(args)
        assert result["message"]["to_id"] == "supervisor-p1", result

    async def test_supervisor_inbox_lists_a_row_sent_to_an_alias(self, setup):
        handler, db, _bus = setup
        await db.create_session(SessionRecord(
            id="sess-sup", project_id="p1", profile_id="supervisor", harness="codex",
            provider="tmux", name="n-supervisor--p1", lifecycle="named", work_dir="/tmp",
            epoch="e", instance_token="token", started_at=1, state="running",
        ))
        sent = await handler._cmd_message_send(_send_args(to_id="supervisor-aq"))
        inbox = await handler._cmd_message_inbox({"to_kind": "session", "to_id": "sess-sup"})
        assert [m["id"] for m in inbox["messages"]] == [sent["message_id"]]

    @pytest.mark.parametrize(
        ("to_id", "project_id"),
        [
            ("supervisor-p1", "p1"),
            # CHAT-1: a suffix naming a project wins over the message's project.
            ("supervisor-p2", "p1"),
        ],
    )
    async def test_suffix_naming_a_project_is_kept(self, setup, to_id, project_id):
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        result = await handler._cmd_message_send(
            _send_args(to_id=to_id, project_id=project_id)
        )
        assert result["message"]["to_id"] == to_id, result
        assert result["message"]["project_id"] == project_id

    async def test_global_supervisor_address_is_kept(self, setup):
        handler, _db, _bus = setup
        args = _send_args(to_id="supervisor-global")
        args.pop("project_id")
        result = await handler._cmd_message_send(args)
        assert result["message"]["to_id"] == "supervisor-global", result
        assert result["message"]["project_id"] is None

    async def test_alias_without_a_project_is_refused_and_not_stored(self, setup):
        handler, db, _bus = setup
        args = _send_args(to_id="supervisor-aq")
        args.pop("project_id")
        result = await handler.execute("message_send", {**args, "_scope": _GLOBAL_SUPERVISOR_SCOPE})
        assert "session:supervisor-aq names no project" in result["error"], result
        assert "supervisor-<project-id>" in result["error"]
        assert "supervisor-global" in result["error"]
        assert await db.list_messages() == []


class TestSupervisorAgentMessage:
    async def test_task_target_resolves_live_session_and_mirrors_comment(self, setup):
        """A supervisor targets the current worker, never a stale session name."""
        handler, db, _bus = setup
        await db.create_agent(
            Agent(id="agent-1", name="worker-one", profile_id="worker", state=AgentState.BUSY)
        )
        await db.create_task(
            Task(
                id="task-1",
                project_id="p1",
                title="work",
                description="do work",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="agent-1",
            )
        )
        await db.create_session(
            SessionRecord(
                id="session-live",
                task_id="task-1",
                agent_id="agent-1",
                project_id="p1",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="s-task-1",
                lifecycle="task",
                work_dir="/tmp",
                epoch="test",
                instance_token="token",
                started_at=1,
                state="running",
            )
        )

        result = await handler._cmd_agent_message({"target": "task-1", "body": "stop tests"})

        assert result["state"] == "queued"
        assert result["target"] == {"task_id": "task-1", "session_id": "session-live"}
        message = await db.get_message(result["message_id"])
        assert (message.to_kind, message.to_id, message.from_kind) == (
            "session",
            "session-live",
            "system",
        )
        comments = await db.list_task_comments("task-1", project_id="p1")
        assert comments["comments"][0]["author_kind"] == "supervisor"
        assert comments["comments"][0]["body"] == "stop tests"

    async def test_rejects_task_without_live_session(self, setup):
        handler, db, _bus = setup
        await db.create_task(
            Task(id="task-1", project_id="p1", title="work", description="do work")
        )

        result = await handler._cmd_agent_message({"target": "task-1", "body": "hello"})

        assert result == {"error": "Task 'task-1' has no live worker session"}

    @pytest.mark.parametrize("target", ["task-p2", "session-p2", "worker-p2"])
    async def test_project_scope_rejects_foreign_targets(self, setup, target):
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        await _create_live_worker(db, project_id="p2", suffix="p2")

        result = await handler._cmd_agent_message(
            {"target": target, "body": "cross-project", "project_id": "p1"}
        )

        assert result == {"error": f"No live worker target '{target}' in project 'p1'"}
        assert await db.list_messages(project_id="p2") == []
        assert (await db.list_task_comments("task-p2", project_id="p2"))["comments"] == []

    async def test_project_scoped_broadcast_only_targets_its_project(self, setup):
        handler, db, _bus = setup
        own_session = await _create_live_worker(db, project_id="p1", suffix="p1")
        await db.create_project(Project(id="p2", name="other"))
        await _create_live_worker(db, project_id="p2", suffix="p2")

        result = await handler._cmd_agent_message(
            {"all_running": True, "body": "project guidance", "project_id": "p1"}
        )

        assert result["count"] == 1
        assert [row["session_id"] for row in result["recipients"]] == [own_session.id]
        assert len(await db.list_messages(project_id="p1")) == 1
        assert await db.list_messages(project_id="p2") == []

    async def test_reply_to_threads_guidance_onto_the_workers_message(self, setup):
        """2026-09-27 noble-crest: guidance answering a threaded worker message
        lacked the thread, so the worker's message-thread wait expired."""
        handler, db, _bus = setup
        session = await _create_live_worker(db, project_id="p1", suffix="w")
        asked = await handler._cmd_message_send(
            _send_args(
                from_kind="session",
                from_id=session.id,
                to_kind="session",
                to_id="supervisor-p1",
                thread_id="task-w:graph-filing",
            )
        )

        result = await handler._cmd_agent_message(
            {"target": "task-w", "body": "filed", "reply_to": asked["message_id"]}
        )

        message = await db.get_message(result["message_id"])
        assert (message.to_kind, message.to_id) == ("session", session.id)
        assert message.thread_id == "task-w:graph-filing"
        assert message.reply_to_id == asked["message_id"]
        assert (await db.get_message(asked["message_id"])).read_at is not None

    async def test_reply_to_must_name_a_message_in_the_workers_project(self, setup):
        handler, db, _bus = setup
        await _create_live_worker(db, project_id="p1", suffix="w")
        await db.create_project(Project(id="p2", name="other"))
        foreign = await handler._cmd_message_send(
            _send_args(project_id="p2", to_id="supervisor-p2", thread_id="t")
        )

        # A project-pinned caller is told "not found" for anything outside its
        # view, so the refusal never confirms a foreign id exists.
        for reply_to in ("msg-missing", foreign["message_id"]):
            result = await handler._cmd_agent_message(
                {"target": "task-w", "body": "filed", "reply_to": reply_to, "project_id": "p1"}
            )
            assert result == {"error": f"Message '{reply_to}' not found"}
        assert (
            await handler._cmd_agent_message(
                {"target": "task-w", "body": "filed", "reply_to": "msg-missing"}
            )
        ) == {"error": "Message 'msg-missing' not found"}
        # An unscoped caller can read the row, so it is refused for what it is.
        unscoped = await handler._cmd_agent_message(
            {"target": "task-w", "body": "filed", "reply_to": foreign["message_id"]}
        )
        assert "not a message of the target's project 'p1'" in unscoped["error"]
        assert await db.list_messages(to_kind="session", to_id="session-w") == []

    async def test_reply_to_another_projects_report_names_the_supported_answer(self, setup):
        """2026-10-09 clear-zenith-84: supervisor-agent-queue read another
        project's report from its inbox, then ``aq agent message <task>
        --reply-to <that id>`` answered "not found" for the body it held."""
        handler, db, _bus = setup
        await _create_live_worker(db, project_id="p1", suffix="w")
        await db.create_project(Project(id="p2", name="other"))
        report = await handler._cmd_message_send(
            _send_args(
                project_id="p2",
                from_kind="session",
                from_id="supervisor-p2",
                to_id="supervisor-p1",
            )
        )
        assert report["message"]["project_id"] == "p2", report

        result = await handler._cmd_agent_message(
            {
                "target": "task-w",
                "body": "filed",
                "reply_to": report["message_id"],
                "project_id": "p1",
            }
        )

        assert result == {
            "error": (
                f"Message '{report['message_id']}' is not a message of the target's project "
                f"'p1': reply_to threads guidance onto that worker's own message. Send "
                f"without reply_to, and answer '{report['message_id']}' with "
                f"`aq message reply {report['message_id']}`."
            )
        }
        assert await db.list_messages(to_kind="session", to_id="session-w") == []

    async def test_reply_to_cannot_broadcast(self, setup):
        handler, db, _bus = setup
        await _create_live_worker(db, project_id="p1", suffix="w")
        result = await handler._cmd_agent_message(
            {"all_running": True, "body": "filed", "reply_to": "msg-1"}
        )
        assert "error" in result and "all_running" in result["error"]
        assert await db.list_messages(to_kind="session", to_id="session-w") == []

    async def test_status_reports_queued_delivered_and_acknowledged(self, setup):
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args())

        assert (await handler._cmd_message_status({"message_id": sent["message_id"]}))["state"] == "queued"
        await db.mark_delivered(sent["message_id"], via="nudge")
        delivered = await handler._cmd_message_status({"message_id": sent["message_id"]})
        assert delivered["state"] == "delivered"
        assert delivered["via"] == "nudge"
        await db.mark_read(sent["message_id"])
        assert (await handler._cmd_message_status({"message_id": sent["message_id"]}))["state"] == "acknowledged"

    @pytest.mark.parametrize("to_kind", ["supervisor", "session", "task"])
    async def test_status_hides_messages_from_other_projects(self, setup, to_kind):
        """Another project's traffic to its own mailboxes stays out of view."""
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        worker = await _create_live_worker(db, project_id="p2", suffix="p2")
        to_id = {"supervisor": "supervisor-p2", "session": worker.id, "task": worker.task_id}
        sent = await handler._cmd_message_send(
            _send_args(
                project_id="p2",
                to_kind="session" if to_kind == "supervisor" else to_kind,
                to_id=to_id[to_kind],
            )
        )

        result = await handler._cmd_message_status(
            {"message_id": sent["message_id"], "project_id": "p1"}
        )

        assert result == {"error": f"Message '{sent['message_id']}' not found"}


# ---------------------------------------------------------------------------
# message_reply
# ---------------------------------------------------------------------------


class TestReply:
    async def test_mirrors_sender_and_links(self, setup):
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args(thread_id="discord:9"))
        result = await handler._cmd_message_reply(
            {"message_id": sent["message_id"], "body": "3 tasks running"}
        )
        reply = await db.get_message(result["reply_id"])
        assert reply.to_kind == "user"
        assert reply.to_id == "discord:1"
        assert reply.from_kind == "session"
        assert reply.from_id == "supervisor-p1"
        assert reply.thread_id == "discord:9"
        assert reply.reply_to_id == sent["message_id"]

    async def test_marks_original_read(self, setup):
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args())
        await handler._cmd_message_reply({"message_id": sent["message_id"], "body": "ok"})
        assert (await db.get_message(sent["message_id"])).read_at is not None

    async def test_emits_message_replied(self, setup):
        handler, _db, bus = setup
        sent = await handler._cmd_message_send(_send_args(thread_id="t"))
        result = await handler._cmd_message_reply(
            {"message_id": sent["message_id"], "body": "ok", "via": "transcript_tail"}
        )
        replied = bus.of_type("message.replied")
        assert len(replied) == 1
        assert replied[0]["reply_id"] == result["reply_id"]
        assert replied[0]["via"] == "transcript_tail"
        assert replied[0]["body"] == "ok"

    async def test_unknown_message(self, setup):
        handler, _db, _bus = setup
        result = await handler._cmd_message_reply({"message_id": "msg-x", "body": "hi"})
        assert "not found" in result["error"]

    async def test_empty_body_rejected(self, setup):
        handler, _db, _bus = setup
        sent = await handler._cmd_message_send(_send_args())
        result = await handler._cmd_message_reply({"message_id": sent["message_id"], "body": ""})
        assert "body is required" in result["error"]

    async def test_task_recipient_replies_as_session(self, setup):
        """`task` isn't a valid from_kind, so the reply is attributed to a session."""
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args(to_kind="task", to_id="task-1"))
        result = await handler._cmd_message_reply(
            {"message_id": sent["message_id"], "body": "done"}
        )
        reply = await db.get_message(result["reply_id"])
        assert reply.from_kind == "session"
        assert reply.from_id == "task-1"

    async def test_reply_to_a_system_sent_message_fails_cleanly(self, setup):
        """`system` is a valid from_kind but not a valid to_kind.

        The reply mirrors `to_kind = original.from_kind`, so this used to hit
        `ck_messages_to_kind` and surface a raw sqlite3.IntegrityError with the
        whole chat body echoed into the bound-parameter dump.
        """
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(
            _send_args(from_kind="system", from_id="orchestrator", body="secret body text")
        )
        result = await handler._cmd_message_reply(
            {"message_id": sent["message_id"], "body": "ok"}
        )
        assert "cannot reply to a 'system' message" in result["error"]
        # No raw SQL, and above all no message body, in the error text.
        assert "IntegrityError" not in result["error"]
        assert "secret body text" not in result["error"]
        assert "reply_id" not in result
        # Nothing was written.
        assert len(await db.list_messages(project_id="p1")) == 1


# ---------------------------------------------------------------------------
# message_inbox
# ---------------------------------------------------------------------------


class TestInbox:
    async def test_read_only_by_default(self, setup):
        handler, db, bus = setup
        sent = await handler._cmd_message_send(_send_args())
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1"}
        )
        assert result["count"] == 1
        assert (await db.get_message(sent["message_id"])).delivered_at is None
        assert bus.of_type("message.delivered") == []

    async def test_inject_marks_delivered_and_emits(self, setup):
        handler, db, bus = setup
        sent = await handler._cmd_message_send(_send_args())
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert result["injected"] == 1
        assert (await db.get_message(sent["message_id"])).delivered_at is not None
        delivered = bus.of_type("message.delivered")
        assert delivered[0]["method"] == "inject"

    async def test_inject_is_idempotent(self, setup):
        """Second inject claims nothing — the compare-and-set does the work."""
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args())
        first = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        second = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert first["injected"] == 1
        assert second["injected"] == 0

    async def test_archive_after_inject_archived_exactly_once(self, setup):
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args(archive_after_inject=True))
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert result["archived"] == 1
        assert (await db.get_message(sent["message_id"])).archived_at is not None
        again = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert again["archived"] == 0

    async def test_inject_limit_defaults_to_max_inject_per_prompt(self, setup):
        handler, _db, _bus = setup
        handler.config.messages = MessagesConfig(enabled=True, max_inject_per_prompt=2)
        for _ in range(5):
            await handler._cmd_message_send(_send_args())
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert result["injected"] == 2

    async def test_invalid_recipient(self, setup):
        handler, _db, _bus = setup
        result = await handler._cmd_message_inbox({"to_kind": "nope", "to_id": "x"})
        assert "Invalid to_kind" in result["error"]


class TestInboxIncludeConsumed:
    """The crashed-inject backstop (fair-impact-65).

    ``inject`` marks rows delivered before the caller can render them, so an
    agent whose own output dies has an empty pending queue and no way back to
    the bodies it already burned. ``include_consumed`` is that way back, and it
    must never move delivery state in either direction.
    """

    async def test_absent_unless_requested(self, setup):
        """An unflagged call's envelope is unchanged — no new keys."""
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args())
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1"}
        )
        assert "consumed" not in result
        assert "consumed_messages" not in result

    async def test_recovers_the_body_a_crashed_inject_burned(self, setup):
        handler, _db, _bus = setup
        sent = await handler._cmd_message_send(_send_args())
        # Delivery consumed, bodies discarded — the worker's parser crashed.
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert (
            await handler._cmd_message_inbox(
                {"to_kind": "session", "to_id": "supervisor-p1"}
            )
        )["count"] == 0

        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "include_consumed": True}
        )
        assert result["count"] == 0
        assert result["consumed"] == 1
        assert result["consumed_messages"][0]["id"] == sent["message_id"]
        assert result["consumed_messages"][0]["body"] == _send_args()["body"]

    async def test_recovers_an_archived_body(self, setup):
        """``archive_after_inject`` sweeps rows in the same pass that delivers
        them, so those bodies are invisible to every other read."""
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args(archive_after_inject=True))
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        assert (await db.get_message(sent["message_id"])).archived_at is not None
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "include_consumed": True}
        )
        assert [m["id"] for m in result["consumed_messages"]] == [sent["message_id"]]

    async def test_reported_on_the_inject_path_too(self, setup):
        """A worker recovering mid-turn runs the same command with both flags."""
        handler, _db, _bus = setup
        sent = await handler._cmd_message_send(_send_args())
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True,
             "include_consumed": True}
        )
        assert result["injected"] == 0
        assert [m["id"] for m in result["consumed_messages"]] == [sent["message_id"]]

    async def test_does_not_re_claim_or_re_emit(self, setup):
        handler, _db, bus = setup
        await handler._cmd_message_send(_send_args())
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        before = bus.of_type("message.delivered")
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True,
             "include_consumed": True}
        )
        assert result["injected"] == 0
        assert result["archived"] == 0
        assert bus.of_type("message.delivered") == before

    async def test_pending_and_consumed_are_disjoint(self, setup):
        handler, _db, _bus = setup
        old = await handler._cmd_message_send(_send_args(body="old"))
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        new = await handler._cmd_message_send(_send_args(body="new"))
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "include_consumed": True}
        )
        assert [m["id"] for m in result["messages"]] == [new["message_id"]]
        assert [m["id"] for m in result["consumed_messages"]] == [old["message_id"]]

    async def test_read_only_never_mutates(self, setup):
        """A plain consumed re-read leaves both stamps exactly as they were."""
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args())
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        before = await db.get_message(sent["message_id"])
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "include_consumed": True}
        )
        after = await db.get_message(sent["message_id"])
        assert (after.delivered_at, after.read_at, after.archived_at, after.via) == (
            before.delivered_at, before.read_at, before.archived_at, before.via,
        )

    async def test_scoped_to_the_recipient(self, setup):
        handler, _db, _bus = setup
        mine = await handler._cmd_message_send(_send_args(body="mine"))
        other = await handler._cmd_message_send(
            _send_args(to_kind="task", to_id="task-9", body="theirs")
        )
        for args in ({"inject": True},):
            await handler._cmd_message_inbox({"to_kind": "session", "to_id": "supervisor-p1", **args})
            await handler._cmd_message_inbox({"to_kind": "task", "to_id": "task-9", **args})
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "include_consumed": True}
        )
        assert [m["id"] for m in result["consumed_messages"]] == [mine["message_id"]]
        assert other["message_id"] not in {m["id"] for m in result["consumed_messages"]}

    async def test_limit_applies_to_each_half(self, setup):
        handler, _db, _bus = setup
        for index in range(4):
            await handler._cmd_message_send(_send_args(body=f"old{index}"))
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True, "limit": 4}
        )
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1",
             "include_consumed": True, "limit": 2}
        )
        assert result["consumed"] == 2

    async def test_foreign_mailbox_still_refused(self, setup):
        """The re-read is not a way around the fence."""
        handler, db, _bus = setup
        worker = await _create_live_worker(db, project_id="p1", suffix="w1")
        handler._current_scope = {
            "kind": "session", "session_id": worker.id, "project_id": "p1",
            "task_id": worker.task_id, "elevated": False,
        }
        await handler._cmd_message_send(_send_args(body="not yours"))
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "inject": True}
        )
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-p1", "include_consumed": True}
        )
        handler._current_scope = None
        assert "out of scope" in result["error"]

    async def test_system_records_stay_admin_only(self, setup):
        handler, _db, _bus = setup
        handler._current_scope = {
            "kind": "session", "session_id": "sess-x", "project_id": "p1",
            "task_id": None, "elevated": False,
        }
        await handler._cmd_message_send(
            _send_args(project_id=None, to_id="supervisor-global", body="system row")
        )
        await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-global", "inject": True}
        )
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "supervisor-global", "include_consumed": True}
        )
        handler._current_scope = None
        assert "global admin" in result["error"]


# ---------------------------------------------------------------------------
# message_inbox mailbox fence — a non-elevated session token may only read
# (and, with inject, consume) the mailboxes it owns.
# ---------------------------------------------------------------------------


def _session_scope(**overrides) -> dict:
    scope = {
        "kind": "session",
        "session_id": "sess-1",
        "task_id": "task-1",
        "project_id": "p1",
        "elevated": False,
    }
    scope.update(overrides)
    return scope


async def _seed_session_row(db, *, task_id: str | None = "task-claimed") -> None:
    import time

    from src.models import SessionRecord, Task

    if task_id is not None:
        await db.create_task(Task(id=task_id, project_id="p1", title="claimed", description=""))
    await db.create_session(
        SessionRecord(
            id="sess-1",
            project_id="p1",
            profile_id="worker",
            harness="claude",
            provider="anthropic",
            name="n-sess-1",
            lifecycle="pool",
            work_dir="/tmp/ws",
            epoch="e1",
            instance_token="tok-1",
            started_at=time.time(),
            task_id=task_id,
            state="running",
        )
    )


class TestInboxMailboxFence:
    async def test_supervisor_uuid_inbox_includes_named_mailboxes_in_priority_order(self, setup):
        handler, db, _bus = setup
        await db.create_session(SessionRecord(
            id="sess-1", project_id="p1", profile_id="supervisor", harness="codex",
            provider="tmux", name="n-supervisor--p1", lifecycle="named", work_dir="/tmp",
            epoch="e", instance_token="token", started_at=1, state="running",
        ))
        for recipient, priority in (("sess-1", 3), ("supervisor-p1", 1), ("n-supervisor--p1", 2)):
            await handler._cmd_message_send(_send_args(to_id=recipient, priority=priority))
        args = {"to_kind": "session", "to_id": "sess-1", "limit": 2,
                "_scope": _session_scope(elevated=True)}
        plain = await handler.execute("message_inbox", {**args, "_scope": _session_scope()})
        assert [m["to_id"] for m in plain["messages"]] == ["sess-1"]
        result = await handler.execute("message_inbox", args)
        assert [m["to_id"] for m in result["messages"]] == ["supervisor-p1", "n-supervisor--p1"]
        result = await handler.execute("message_inbox", {**args, "inject": True})
        assert result["injected"] == 2
        remaining = await handler.execute("message_inbox", args)
        assert [m["to_id"] for m in remaining["messages"]] == ["sess-1"]

    async def test_own_session_mailbox_is_readable(self, setup):
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args(to_id="sess-1"))
        result = await handler.execute(
            "message_inbox",
            {"to_kind": "session", "to_id": "sess-1", "_scope": _session_scope()},
        )
        assert "error" not in result
        assert result["count"] == 1

    async def test_foreign_session_mailbox_is_refused_and_not_consumed(self, setup):
        handler, db, _bus = setup
        sent = await handler._cmd_message_send(_send_args(to_id="sess-other"))
        result = await handler.execute(
            "message_inbox",
            {
                "to_kind": "session",
                "to_id": "sess-other",
                "inject": True,
                "_scope": _session_scope(),
            },
        )
        assert "out of scope" in result["error"]
        assert "session:sess-other" in result["error"]
        # The refusal consumed nothing — the message is still pending.
        persisted = await db.get_message(sent["message_id"])
        assert persisted.delivered_at is None

    async def test_pinned_task_mailbox_is_readable_and_foreign_task_refused(self, setup):
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args(to_kind="task", to_id="task-1"))
        own = await handler.execute(
            "message_inbox",
            {"to_kind": "task", "to_id": "task-1", "_scope": _session_scope()},
        )
        assert "error" not in own
        assert own["count"] == 1

        foreign = await handler.execute(
            "message_inbox",
            {"to_kind": "task", "to_id": "task-2", "_scope": _session_scope()},
        )
        assert "out of scope" in foreign["error"]

    async def test_pool_token_reads_its_live_claim_only(self, setup):
        """A pool token pins no task; the session row's claim is the fence."""
        handler, db, _bus = setup
        await _seed_session_row(db, task_id="task-claimed")

        claimed = await handler.execute(
            "message_inbox",
            {
                "to_kind": "task",
                "to_id": "task-claimed",
                "_scope": _session_scope(task_id=None),
            },
        )
        assert "error" not in claimed

        other = await handler.execute(
            "message_inbox",
            {"to_kind": "task", "to_id": "task-x", "_scope": _session_scope(task_id=None)},
        )
        assert "out of scope" in other["error"]

    async def test_pool_token_with_no_claim_cannot_read_any_task_mailbox(self, setup):
        handler, db, _bus = setup
        await _seed_session_row(db, task_id=None)
        result = await handler.execute(
            "message_inbox",
            {"to_kind": "task", "to_id": "task-x", "_scope": _session_scope(task_id=None)},
        )
        assert "out of scope" in result["error"]

    async def test_own_profile_mailbox_is_readable_and_foreign_profile_refused(self, setup):
        handler, db, _bus = setup
        await _seed_session_row(db)
        own = await handler.execute(
            "message_inbox",
            {"to_kind": "profile", "to_id": "worker", "_scope": _session_scope()},
        )
        assert "error" not in own

        foreign = await handler.execute(
            "message_inbox",
            {"to_kind": "profile", "to_id": "reviewer", "_scope": _session_scope()},
        )
        assert "out of scope" in foreign["error"]

    async def test_user_mailbox_is_never_agent_readable(self, setup):
        handler, _db, _bus = setup
        result = await handler.execute(
            "message_inbox",
            {"to_kind": "user", "to_id": "user", "_scope": _session_scope()},
        )
        assert "out of scope" in result["error"]

    async def test_elevated_supervisor_reads_any_mailbox(self, setup):
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args(to_id="somebody-else"))
        result = await handler.execute(
            "message_inbox",
            {
                "to_kind": "session",
                "to_id": "somebody-else",
                "_scope": _session_scope(elevated=True),
            },
        )
        assert "error" not in result
        assert result["count"] == 1

    async def test_local_caller_is_unfenced(self, setup):
        """Direct calls (no scope envelope) keep the trusted-loopback contract."""
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args(to_id="anyone"))
        result = await handler._cmd_message_inbox(
            {"to_kind": "session", "to_id": "anyone"}
        )
        assert "error" not in result
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# message_status mailbox fence — the idle-session nudge names
# ``aq message status <id>``, so a plain session may read the messages
# addressed to its own mailboxes and nothing else.
# ---------------------------------------------------------------------------


class TestStatusMailboxFence:
    async def test_pool_worker_reads_a_nudged_message_to_its_claimed_task(self, setup):
        """The nudge marks the row delivered, so ``inbox`` no longer lists it.

        ``message_status`` is the only way the worker can reach the body.
        """
        handler, db, _bus = setup
        await _seed_session_row(db, task_id="task-claimed")
        sent = await handler._cmd_message_send(
            _send_args(to_kind="task", to_id="task-claimed", body="rebase onto main first")
        )
        await db.mark_delivered(sent["message_id"], via="nudge")

        inbox = await handler.execute(
            "message_inbox",
            {"to_kind": "task", "to_id": "task-claimed", "_scope": _session_scope(task_id=None)},
        )
        assert inbox["count"] == 0

        result = await handler.execute(
            "message_status",
            {"message_id": sent["message_id"], "_scope": _session_scope(task_id=None)},
        )
        assert "error" not in result, result
        assert result["state"] == "delivered"
        assert result["message"]["body"] == "rebase onto main first"

    async def test_own_session_and_pinned_task_messages_are_readable(self, setup):
        handler, _db, _bus = setup
        for to_kind, to_id in (("session", "sess-1"), ("task", "task-1")):
            sent = await handler._cmd_message_send(_send_args(to_kind=to_kind, to_id=to_id))
            result = await handler.execute(
                "message_status",
                {"message_id": sent["message_id"], "_scope": _session_scope()},
            )
            assert result["state"] == "queued", (to_kind, result)

    @pytest.mark.parametrize(
        ("to_kind", "to_id"),
        [
            ("session", "sess-other"),
            ("session", "supervisor-p1"),
            ("task", "task-2"),
            ("profile", "reviewer"),
            ("user", "dashboard"),
        ],
    )
    async def test_another_recipients_message_is_not_found(self, setup, to_kind, to_id):
        """A refusal must not confirm the id exists, let alone return its body."""
        handler, db, _bus = setup
        await _seed_session_row(db)
        sent = await handler._cmd_message_send(
            _send_args(to_kind=to_kind, to_id=to_id, body="not yours")
        )
        result = await handler.execute(
            "message_status",
            {"message_id": sent["message_id"], "_scope": _session_scope()},
        )
        assert result == {"error": f"Message '{sent['message_id']}' not found"}
        assert (await db.get_message(sent["message_id"])).delivered_at is None

    async def test_pool_token_with_no_claim_reads_no_task_message(self, setup):
        handler, db, _bus = setup
        await _seed_session_row(db, task_id=None)
        sent = await handler._cmd_message_send(_send_args(to_kind="task", to_id="task-x"))
        result = await handler.execute(
            "message_status",
            {"message_id": sent["message_id"], "_scope": _session_scope(task_id=None)},
        )
        assert "not found" in result["error"]

    async def test_elevated_supervisor_reads_any_project_message(self, setup):
        handler, _db, _bus = setup
        sent = await handler._cmd_message_send(_send_args(to_id="sess-other"))
        result = await handler.execute(
            "message_status",
            {"message_id": sent["message_id"], "_scope": _session_scope(elevated=True)},
        )
        assert result["state"] == "queued"


def _supervisor_scope(project_id: str) -> dict:
    return _session_scope(
        session_id=f"sess-sup-{project_id}", task_id=None, project_id=project_id, elevated=True
    )


async def _execute_as(handler, command: str, args: dict, scope: dict) -> dict:
    """Run *command* through the API's scope gate, then the handler — the live path."""
    args = dict(args)
    assert check_command_scope(command, args, RequestScope(**scope)) is None
    return await handler.execute(command, {**args, "_scope": scope})


class TestStatusRecipientProject:
    """A project-pinned reader sees what other projects addressed to its mailboxes.

    2026-10-09 clear-zenith-84: other projects' supervisors escalated to
    ``session:supervisor-agent-queue``.  Each row kept its sender's project
    (CHAT-1; the sender's elevated token pins its own), the delivery engine
    nudged ``Handle `aq message status <id> --json`.`` and marked it delivered,
    and the addressed supervisor — whose token injects its own ``project_id`` —
    was told "not found" for the body it had just been handed.
    """

    async def test_nudged_cross_project_report_is_readable_by_its_recipient(self, setup):
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        await db.create_project(Project(id="p3", name="third"))
        sent = await _execute_as(
            handler,
            "message_send",
            {
                "to_kind": "session",
                "to_id": "supervisor-p1",
                "from_kind": "session",
                "from_id": "supervisor-p2",
                "body": "registry investigation needed",
            },
            _supervisor_scope("p2"),
        )
        assert sent["message"]["project_id"] == "p2", sent
        assert sent["message"]["to_id"] == "supervisor-p1"

        sessions = _NudgeRecorder()
        engine = MessageDeliveryEngine(
            db=db, sessions=sessions, config=MessagesConfig(enabled=True), bus=None
        )
        assert (await engine.run_delivery_pass())["delivered"] == 1
        [(kind, target_id, text)] = sessions.nudges
        assert (kind, target_id) == ("session", "supervisor-p1")
        pointer = re.fullmatch(r"Handle `aq message status (\S+) --json`\.", text)
        assert pointer and pointer.group(1) == sent["message_id"], text

        result = await _execute_as(
            handler, "message_status", {"message_id": pointer.group(1)}, _supervisor_scope("p1")
        )
        assert "error" not in result, result
        assert (result["state"], result["via"]) == ("delivered", "nudge")
        assert result["message"]["body"] == "registry investigation needed"

        # The supported answer reaches the sender's mailbox in its project.
        reply = await _execute_as(
            handler,
            "message_reply",
            {"message_id": pointer.group(1), "body": "filed"},
            _supervisor_scope("p1"),
        )
        assert "error" not in reply, reply
        assert (reply["reply"]["to_id"], reply["reply"]["project_id"]) == ("supervisor-p2", "p2")

        # The boundary holds for every project the row was not addressed to.
        outsider = await _execute_as(
            handler, "message_status", {"message_id": pointer.group(1)}, _supervisor_scope("p3")
        )
        assert outsider == {"error": f"Message '{sent['message_id']}' not found"}

    @pytest.mark.parametrize("mailbox", ["supervisor", "named_supervisor", "session", "task"])
    async def test_another_projects_row_to_an_own_mailbox_is_readable(self, setup, mailbox):
        handler, db, _bus = setup
        await db.create_project(Project(id="p2", name="other"))
        worker = await _create_live_worker(db, project_id="p1", suffix="own")
        to_kind, to_id = {
            "supervisor": ("session", "supervisor-p1"),
            "named_supervisor": ("session", "n-supervisor--p1"),
            "session": ("session", worker.id),
            "task": ("task", worker.task_id),
        }[mailbox]
        sent = await handler._cmd_message_send(
            _send_args(project_id="p2", to_kind=to_kind, to_id=to_id)
        )
        assert sent["message"]["project_id"] == "p2", sent

        result = await handler._cmd_message_status(
            {"message_id": sent["message_id"], "project_id": "p1"}
        )

        assert "error" not in result, result
        assert result["message"]["to_id"] == to_id

    @pytest.mark.parametrize("to_id", ["supervisor-global", "n-supervisor--global"])
    async def test_global_supervisor_mailbox_is_no_projects_mailbox(self, setup, to_id):
        handler, db, _bus = setup
        await db.create_project(Project(id="global", name="named global"))
        sent = await handler._cmd_message_send(_send_args(project_id="p1", to_id=to_id))
        await db.mark_delivered(sent["message_id"], via="nudge")
        result = await handler._cmd_message_status(
            {"message_id": sent["message_id"], "project_id": "global"}
        )
        assert result == {"error": f"Message '{sent['message_id']}' not found"}


class _NudgeRecorder:
    """Every live target is idle; each nudge is recorded and confirmed."""

    def __init__(self) -> None:
        self.nudges: list[tuple[str, str, str]] = []

    async def activity(self, *, kind, target_id, project_id):
        return "idle"

    async def ensure_started(self, *, kind, target_id, project_id):
        return True

    async def nudge(self, *, kind, target_id, project_id, text):
        self.nudges.append((kind, target_id, text))
        return True


# ---------------------------------------------------------------------------
# message_list
# ---------------------------------------------------------------------------


class TestList:
    async def test_filters_and_ordering(self, setup):
        handler, _db, _bus = setup
        await handler._cmd_message_send(_send_args(body="one", thread_id="t1"))
        await handler._cmd_message_send(_send_args(body="two", thread_id="t2"))
        result = await handler._cmd_message_list({"project_id": "p1", "thread_id": "t1"})
        assert result["count"] == 1
        assert result["messages"][0]["body"] == "one"

    async def test_bad_since(self, setup):
        handler, _db, _bus = setup
        result = await handler._cmd_message_list({"since": "yesterday"})
        assert "since must be" in result["error"]

    async def test_bad_limit(self, setup):
        handler, _db, _bus = setup
        result = await handler._cmd_message_list({"limit": -1})
        assert "limit must be" in result["error"]


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


class TestMessageToDict:
    def test_covers_the_brief_projection(self):
        """`BRIEF_PROJECTIONS["message"]` must address real keys."""
        from src.cli.envelope import BRIEF_PROJECTIONS

        row = message_to_dict(
            Message(
                id="msg-1",
                project_id="p1",
                from_kind="user",
                from_id="discord:1",
                to_kind="session",
                to_id="supervisor-p1",
                body="hi",
            )
        )
        for field in BRIEF_PROJECTIONS["message"]:
            assert field in row, f"{field!r} missing from message_to_dict output"

    def test_flattened_addresses_and_read_flag(self):
        row = message_to_dict(
            Message(
                id="msg-1",
                project_id="p1",
                from_kind="user",
                from_id="discord:1",
                to_kind="session",
                to_id="supervisor-p1",
                body="hi",
                read_at=12.0,
            )
        )
        assert row["from"] == "user:discord:1"
        assert row["to"] == "session:supervisor-p1"
        assert row["read"] is True


# ---------------------------------------------------------------------------
# Event registry + WebSocket forwarding
# ---------------------------------------------------------------------------


class TestEventRegistration:
    def test_message_events_are_registered(self):
        from src.event_schemas import EVENT_SCHEMAS, get_schema

        for event_type in ("message.sent", "message.delivered", "message.replied"):
            assert event_type in EVENT_SCHEMAS
            assert get_schema(event_type) is not None

    def test_required_fields_match_the_spec(self):
        from src.event_schemas import EVENT_SCHEMAS

        assert set(EVENT_SCHEMAS["message.sent"]["required"]) == {
            "message_id",
            "project_id",
            "from_kind",
            "from_id",
            "to_kind",
            "to_id",
        }
        assert set(EVENT_SCHEMAS["message.delivered"]["required"]) == {
            "message_id",
            "project_id",
            "method",
        }
        assert set(EVENT_SCHEMAS["message.replied"]["required"]) == {
            "message_id",
            "reply_id",
            "project_id",
            "body",
        }

    def test_websocket_forwards_message_events(self):
        """`aq chat`/dashboard need message.* on /ws/events (spec §6.2)."""
        from src.api.websocket import _FORWARDED_PREFIXES

        assert "notify." in _FORWARDED_PREFIXES
        assert "message." in _FORWARDED_PREFIXES
