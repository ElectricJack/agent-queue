"""``digest_preview`` and ``digest_status`` (implementation spec §9).

Preview is the dashboard's dry run: it must agree with delivery because it
calls the same builder, and it must be free of side effects -- no window
reserved, nothing sent, no cursor advanced.  Status is the schedule's identity
and health: destination, configuration generation, next evaluation and the
deliveries that need an operator's attention.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update

from src.commands.handler import CommandHandler
from src.commands.principal import (
    CapabilityPolicy,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.config import AppConfig, DatabaseConfig, DiscordConfig, DiscordDigestConfig
from src.database import Database
from src.database.tables import digest_windows, task_comments, tasks
from src.event_bus import EventBus
from src.models import Project, Task, TaskCompletion
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

CHANNEL = "123456789012345678"
NOW = 2_000_000.0
HOUR = 3600.0


def discord(**digest_kwargs) -> DiscordConfig:
    return DiscordConfig(
        bot_token="t", guild_id="1", channel_id=CHANNEL, digest=DiscordDigestConfig(**digest_kwargs)
    )


@pytest.fixture
async def env(tmp_path):
    dsn = lease_dsn("digest-commands")
    db = Database(dsn)
    await db.initialize()
    await db.create_project(Project(id="p", name="Agent Queue"))
    await db.create_project(Project(id="other", name="Other"))
    config = AppConfig(
        discord=discord(),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=dsn),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus(env="dev")
    yield CommandHandler(orch, config), db, config
    await db.close()


async def completed_task(db, task_id, *, project_id="p", at=NOW - 600, title=None):
    await db.create_task(
        Task(id=task_id, project_id=project_id, title=title or task_id, description="")
    )
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(status="COMPLETED"))
    await db.save_task_completion(
        TaskCompletion(
            id="completion-" + uuid4().hex,
            task_id=task_id,
            outcome="pass",
            summary=f"finished {task_id}",
            completed_at=at,
        )
    )


async def progress_note(db, task_id, body, *, project_id="p", at=NOW - 300):
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_comments).values(
                id="comment-" + uuid4().hex,
                task_id=task_id,
                project_id=project_id,
                body=body,
                author_kind="agent",
                author_id="worker",
                kind="progress",
                created_at=at,
            )
        )


def generation(config) -> int:
    """The generation the commands will actually query for this config."""
    from src.digest.schedule import config_generation

    return config_generation(config.discord)


async def open_escalation(db, escalation_id, *, project_id, with_pending_delivery=False):
    incident, _created = await db.create_escalation(
        id=escalation_id,
        now=NOW,
        task_id=None,
        project_id=project_id,
        source_kind="task_failed",
        source_identity=f"attempt-{escalation_id}",
        incident_key=f"incident-{escalation_id}",
        supervisor_owner=f"supervisor-{project_id}",
        summary="Blocked",
        investigation="looked",
        decision_requested="retry or hold?",
        severity="high",
    )
    if with_pending_delivery:
        await db.enqueue_escalation_delivery(
            escalation_id=incident["id"],
            kind="root",
            dedup_key=f"{escalation_id}:root",
            payload={"text": "blocked"},
            available_at=NOW,
        )
    return incident


def session_principal(project_id: str | None, *, elevated: bool = False) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy(aq_commands=frozenset({"digest_preview", "digest_status"})),
        session_id="s1",
        project_id=project_id,
        elevated=elevated,
    )


class TestPreview:
    async def test_an_idle_window_explains_its_silence(self, env):
        handler, _db, _config = env
        result = await handler.execute("digest_preview", {"now": NOW})
        assert result["success"] is True
        assert result["would_send"] is False
        assert result["suppression_reason"] == result["reason"]
        assert result["text"] == ""
        assert result["destination"] == f"discord:{CHANNEL}"

    async def test_activity_renders_the_message_that_would_be_sent(self, env):
        handler, db, config = env
        await completed_task(db, "t1", title="Ship the digest")
        result = await handler.execute("digest_preview", {"now": NOW})
        assert result["would_send"] is True
        assert result["completed_count"] == 1
        assert "finished t1" in result["text"]
        assert result["window"]["until"] == NOW
        assert result["window"]["since"] == NOW - config.discord.digest.interval_minutes * 60

    async def test_preview_agrees_with_the_shared_builder(self, env):
        handler, db, _config = env
        await completed_task(db, "t1", title="Ship the digest")
        await progress_note(db, "t1", "green on the second run")
        result = await handler.execute("digest_preview", {"now": NOW})

        from src.digest import DigestWindow, build_digest

        inputs = await db.collect_digest_activity(
            DigestWindow(since=NOW - HOUR, until=NOW), now=NOW
        )
        assert result["text"] == build_digest(inputs).text

    async def test_preview_writes_nothing(self, env):
        handler, db, _config = env
        await completed_task(db, "t1")
        await handler.execute("digest_preview", {"now": NOW})
        await handler.execute("digest_preview", {"now": NOW})
        async with db._engine.connect() as conn:
            windows = (await conn.execute(select(digest_windows))).mappings().all()
        assert windows == []

    async def test_the_configured_project_filter_bounds_what_a_preview_sees(self, env):
        handler, db, config = env
        await completed_task(db, "mine", project_id="p", title="Mine")
        await completed_task(db, "theirs", project_id="other", title="Theirs")
        config.discord = discord(project_ids=["p"])
        result = await handler.execute("digest_preview", {"now": NOW})
        assert "finished mine" in result["text"]
        assert "finished theirs" not in result["text"]

    async def test_a_project_scoped_session_only_sees_its_own_project(self, env):
        handler, db, _config = env
        await completed_task(db, "mine", project_id="p", title="Mine")
        await completed_task(db, "theirs", project_id="other", title="Theirs")
        with principal_context(session_principal("p")):
            result = await handler.execute("digest_preview", {"now": NOW})
        assert "finished mine" in result["text"]
        assert "finished theirs" not in result["text"]

    async def test_a_project_outside_the_configured_selection_is_refused(self, env):
        handler, _db, config = env
        config.discord = discord(project_ids=["p"])
        with principal_context(session_principal("other")):
            result = await handler.execute("digest_preview", {"now": NOW})
        assert result["success"] is False
        assert result["error_code"] == "out_of_scope"

    async def test_an_unscoped_session_is_refused(self, env):
        handler, _db, _config = env
        with principal_context(session_principal(None)):
            result = await handler.execute("digest_preview", {"now": NOW})
        assert result["error_code"] == "out_of_scope"

    async def test_invalid_settings_are_reported_with_the_preview(self, env):
        handler, _db, config = env
        config.discord = discord(interval_minutes=5, project_ids=["ghost"])
        result = await handler.execute("digest_preview", {"now": NOW})
        joined = " ".join(result["settings_errors"])
        assert "interval_minutes must be between 15 and 1440" in joined
        assert "unknown project 'ghost'" in joined

    async def test_a_highlight_already_sent_is_not_offered_again(self, env):
        handler, db, _config = env
        await completed_task(db, "t1", title="Ship the digest")
        first = await handler.execute("digest_preview", {"now": NOW})
        assert first["would_send"] is True

        from src.digest import DigestWindow, build_digest

        inputs = await db.collect_digest_activity(
            DigestWindow(since=NOW - HOUR, until=NOW), now=NOW
        )
        sent = build_digest(inputs)
        window, _created = await db.reserve_digest_window(
            destination=f"discord:{CHANNEL}",
            config_generation=generation(_config),
            window_start=NOW - 2 * HOUR,
            window_end=NOW - 1200,
            activity_cursor=None,
            due_at=NOW - 1200,
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id == window["id"])
                .values(
                    send_status="sent",
                    external_receipt_id="receipt",
                    receipt_confirmed_at=NOW - 1200,
                    payload={
                        "reported_keys": sorted(sent.reported_keys),
                        "reported_highlights": sorted(sent.reported_highlights),
                    },
                )
            )
        again = await handler.execute("digest_preview", {"now": NOW})
        assert again["would_send"] is False
        assert again["suppression_reason"] == "already_reported"

    async def test_a_scoped_preview_does_not_count_another_projects_escalations(self, env):
        handler, db, _config = env
        await open_escalation(db, "esc-mine", project_id="p")
        await open_escalation(db, "esc-theirs", project_id="other")
        with principal_context(session_principal("p")):
            scoped = await handler.execute("digest_preview", {"now": NOW})
        unscoped = await handler.execute("digest_preview", {"now": NOW})
        assert scoped["open_escalations"] == 1
        assert unscoped["open_escalations"] == 2

    async def test_the_configured_selection_bounds_the_escalation_count(self, env):
        handler, db, config = env
        await open_escalation(db, "esc-mine", project_id="p")
        await open_escalation(db, "esc-theirs", project_id="other")
        config.discord = discord(project_ids=["p"])
        result = await handler.execute("digest_preview", {"now": NOW})
        assert result["open_escalations"] == 1

    async def test_a_new_generation_does_not_inherit_old_window_timing(self, env):
        handler, db, config = env
        await db.reserve_digest_window(
            destination=f"discord:{CHANNEL}",
            config_generation=generation(config),
            window_start=NOW - 4 * HOUR,
            window_end=NOW - 3 * HOUR,
            activity_cursor=None,
            due_at=NOW - 3 * HOUR,
        )
        inherited = await handler.execute("digest_preview", {"now": NOW})
        assert inherited["window"]["since"] == NOW - 3 * HOUR
        assert inherited["window"]["catchup"] is True

        config.discord = discord(interval_minutes=30)
        fresh = await handler.execute("digest_preview", {"now": NOW})
        assert fresh["window"]["since"] == NOW - 1800
        assert fresh["window"]["catchup"] is False

    async def test_a_new_generation_does_not_inherit_reported_history(self, env):
        handler, db, config = env
        await completed_task(db, "t1", title="Ship the digest")
        assert (await handler.execute("digest_preview", {"now": NOW}))["would_send"] is True

        from src.digest import DigestWindow, build_digest

        inputs = await db.collect_digest_activity(
            DigestWindow(since=NOW - HOUR, until=NOW), now=NOW
        )
        sent = build_digest(inputs)
        window, _created = await db.reserve_digest_window(
            destination=f"discord:{CHANNEL}",
            config_generation=generation(config),
            window_start=NOW - 2 * HOUR,
            window_end=NOW - 1200,
            activity_cursor=None,
            due_at=NOW - 1200,
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id == window["id"])
                .values(
                    send_status="sent",
                    external_receipt_id="receipt",
                    receipt_confirmed_at=NOW - 1200,
                    payload={
                        "reported_keys": sorted(sent.reported_keys),
                        "reported_highlights": sorted(sent.reported_highlights),
                    },
                )
            )
        assert (await handler.execute("digest_preview", {"now": NOW}))["would_send"] is False

        config.discord = discord(categories=["work"])
        fresh = await handler.execute("digest_preview", {"now": NOW})
        assert fresh["would_send"] is True


class TestStatus:
    async def test_status_reports_the_configured_schedule_and_generation(self, env):
        handler, _db, config = env
        result = await handler.execute("digest_status", {"now": NOW})
        assert result["success"] is True
        assert result["destination"] == f"discord:{CHANNEL}"
        assert result["channel_id"] == CHANNEL
        assert result["digest"]["interval_minutes"] == 60
        assert result["digest"]["categories"] == ["budget", "system", "vcs", "work"]
        assert result["escalation"]["supervisor_delivery_timeout_minutes"] == 15

        from src.digest.schedule import config_generation

        assert result["config_generation"] == config_generation(config.discord)

    async def test_a_settings_change_moves_the_generation(self, env):
        handler, _db, config = env
        before = await handler.execute("digest_status", {"now": NOW})
        config.discord = discord(interval_minutes=30)
        after = await handler.execute("digest_status", {"now": NOW})
        assert after["config_generation"] != before["config_generation"]

    async def test_next_evaluation_follows_the_last_window(self, env):
        handler, db, _config = env
        await db.reserve_digest_window(
            destination=f"discord:{CHANNEL}",
            config_generation=generation(_config),
            window_start=NOW - HOUR,
            window_end=NOW - 600,
            activity_cursor=None,
            due_at=NOW - 600,
        )
        result = await handler.execute("digest_status", {"now": NOW})
        assert result["last_window_end"] == NOW - 600
        assert result["next_evaluation_at"] == NOW - 600 + HOUR

    async def test_delivery_health_counts_windows_needing_attention(self, env):
        handler, db, _config = env
        for index, status in enumerate(("pending", "retry", "unknown", "suppressed")):
            window, _ = await db.reserve_digest_window(
                destination=f"discord:{CHANNEL}",
                config_generation=generation(_config),
                window_start=NOW - HOUR - index * 10,
                window_end=NOW - index * 10,
                activity_cursor=None,
                due_at=NOW,
            )
            async with db._engine.begin() as conn:
                await conn.execute(
                    update(digest_windows)
                    .where(digest_windows.c.id == window["id"])
                    .values(
                        send_status=status,
                        suppression_reason="no_activity" if status == "suppressed" else None,
                    )
                )
        result = await handler.execute("digest_status", {"now": NOW})
        assert result["delivery_health"]["pending"] == 1
        assert result["delivery_health"]["retry"] == 1
        assert result["delivery_health"]["unknown"] == 1
        assert "suppressed" not in result["delivery_health"]
        assert len(result["recent_windows"]) == 4

    async def test_disabled_external_escalation_is_surfaced_as_a_warning(self, env):
        handler, _db, config = env
        config.discord.escalation.enabled = False
        result = await handler.execute("digest_status", {"now": NOW})
        assert any("still created" in note for note in result["warnings"])

    async def test_pending_escalation_delivery_is_visible(self, env):
        handler, db, _config = env
        incident, _created = await db.create_escalation(
            id="esc-1",
            now=NOW,
            task_id=None,
            project_id="p",
            source_kind="task_failed",
            source_identity="attempt-1",
            incident_key="incident-1",
            supervisor_owner="supervisor-p",
            summary="Blocked",
            investigation="looked",
            decision_requested="retry or hold?",
            severity="high",
        )
        await db.enqueue_escalation_delivery(
            escalation_id=incident["id"],
            kind="root",
            dedup_key="esc-1:root",
            payload={"text": "blocked"},
            available_at=NOW,
        )
        result = await handler.execute("digest_status", {"now": NOW})
        assert result["open_escalations"] == 1
        assert result["pending_escalation_deliveries"] == 1

    async def test_a_scoped_status_hides_another_projects_escalation_health(self, env):
        handler, db, _config = env
        await open_escalation(db, "esc-mine", project_id="p", with_pending_delivery=True)
        await open_escalation(db, "esc-theirs", project_id="other", with_pending_delivery=True)
        with principal_context(session_principal("p")):
            scoped = await handler.execute("digest_status", {"now": NOW})
        unscoped = await handler.execute("digest_status", {"now": NOW})
        assert scoped["open_escalations"] == 1
        assert scoped["pending_escalation_deliveries"] == 1
        assert unscoped["open_escalations"] == 2
        assert unscoped["pending_escalation_deliveries"] == 2

    async def test_a_settings_change_starts_a_fresh_schedule(self, env):
        handler, db, config = env
        window, _ = await db.reserve_digest_window(
            destination=f"discord:{CHANNEL}",
            config_generation=generation(config),
            window_start=NOW - 2 * HOUR,
            window_end=NOW - HOUR,
            activity_cursor=None,
            due_at=NOW - HOUR,
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id == window["id"])
                .values(send_status="retry")
            )
        before = await handler.execute("digest_status", {"now": NOW})
        assert before["last_window_end"] == NOW - HOUR
        assert before["delivery_health"]["retry"] == 1
        assert len(before["recent_windows"]) == 1

        config.discord = discord(project_ids=["p"])
        after = await handler.execute("digest_status", {"now": NOW})
        assert after["config_generation"] != before["config_generation"]
        assert after["last_window_end"] is None
        assert after["next_evaluation_at"] == NOW + HOUR
        assert after["delivery_health"]["retry"] == 0
        assert after["recent_windows"] == []


class TestSurface:
    def test_both_commands_are_registered_with_schemas_and_a_category(self):
        from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

        by_name = {tool["name"]: tool for tool in _ALL_TOOL_DEFINITIONS}
        for name in ("digest_preview", "digest_status"):
            assert _TOOL_CATEGORIES[name] == "digest"
            assert by_name[name]["input_schema"]["additionalProperties"] is False
        assert hasattr(CommandHandler, "_cmd_digest_preview")
        assert hasattr(CommandHandler, "_cmd_digest_status")

    def test_the_generated_api_exposes_both_routes(self):
        from src.api.codegen import build_category_routers

        paths = {route.path for router in build_category_routers() for route in router.routes}
        assert {"/api/digest/preview", "/api/digest/status"} <= paths

    def test_typed_response_models_are_registered(self):
        from src.api.models import get_all_response_models

        models = get_all_response_models()
        assert models["digest_preview"].__name__ == "DigestPreviewResponse"
        assert models["digest_status"].__name__ == "DigestStatusResponse"
