"""Tests for ``MessageCommandsMixin`` — supervisor-agent §6.1 / §12.

Covers the command envelopes, the ``messages.enabled`` gate, and the
``message.*`` event payloads (which must validate against the registry the
same way every other emitter's do).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.commands.message_commands import MESSAGES_DISABLED_ERROR, message_to_dict
from src.config import MessagesConfig
from src.database import Database
from src.event_schemas import validate_payload
from src.models import Message, Project


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
    db = Database(str(tmp_path / "messages.db"))
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
