"""Discord callbacks and receipts integrated with the isolated question database."""
from types import SimpleNamespace

from src.commands.handler import CommandHandler
from src.discord.agent_questions import restore_agent_question_views, retry_discord_user_messages
from src.discord.notification_handler import DiscordNotificationHandler
from src.models import TaskStatus
from tests.test_agent_questions import env as env, service, entry
from tests.test_discord_agent_questions import fake_bot, interaction


async def test_saved_question_card_restores_and_reply_reaches_same_session(env):
    svc = service(env)
    bot = fake_bot(env.db)
    orch = SimpleNamespace(db=env.db, bus=env.bus, agent_questions=svc, plugin_registry=None)
    handler = CommandHandler(orch, env.config)
    handler.set_active_project(None)
    handler._current_scope = None
    bot.handler = handler
    notifications = DiscordNotificationHandler(bot, env.bus)
    try:
        await svc.observe(env.row, [entry("May I deploy these changes to production?")])
        q = (await env.db.list_agent_questions())[0]
        assert q["state"] == "human"
        assert q["discord_message_id"] == "800"
        assert bot._send_message.await_count == 1
    finally:
        notifications.shutdown()

    restarted = fake_bot(env.db)
    restarted.handler = handler
    await restore_agent_question_views(restarted)
    view = restarted.add_view.call_args.args[0]
    assert restarted.add_view.call_args.kwargs["message_id"] == 800
    click = interaction()
    await view.children[0].callback(click)
    modal = click.response.send_modal.await_args.args[0]
    modal.answer._value = "No deployment; continue with local tests."
    await modal.on_submit(interaction())
    await svc.tick()

    final = await env.db.get_agent_question(q["id"])
    assert final["state"] == "delivered", final
    assert len(env.provider.sent_nudges) == 1
    assert env.provider.sent_nudges[0][0] == env.row.name
    assert "No deployment; continue with local tests." in env.provider.sent_nudges[0][1]
    assert len(env.provider.starts) == 1
    assert len(await env.db.list_sessions()) == 1
    task = await env.db.get_task(env.row.task_id)
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.claim_epoch == 7
    assert task.assigned_agent_id == "worker"
    # A second card (or a stale still-open modal) has the same durable ID.
    # It cannot overwrite the first answer or submit a second terminal turn.
    second_click = interaction()
    await view.children[0].callback(second_click)
    second_modal = second_click.response.send_modal.await_args.args[0]
    second_modal.answer._value = "Conflicting duplicate answer"
    second_submit = interaction()
    await second_modal.on_submit(second_submit)
    await svc.tick()
    assert len(env.provider.sent_nudges) == 1
    assert (await env.db.get_agent_question(q["id"]))["answer"] == "No deployment; continue with local tests."
    assert "saved and queued" not in second_submit.followup.send.await_args.args[0]
    handler._current_scope = None
    handler.set_active_project(None)


async def test_failed_discord_message_uses_persisted_retry_and_deduplicates_later_events(env):
    message = await env.db.create_message(
        project_id="p", from_kind="session", from_id=env.row.id,
        to_kind="user", to_id="user", body="Please confirm the intended scope.",
    )
    bot = fake_bot(env.db)
    bot._send_message.return_value = None
    notifications = DiscordNotificationHandler(bot, env.bus)
    await env.bus.emit("message.sent", {
        "message_id": message.id, "project_id": "p", "from_kind": "session",
        "from_id": env.row.id, "to_kind": "user", "to_id": "user",
    })
    notifications.shutdown()
    receipt = await env.db.get_message_discord_receipt(message.id)
    assert receipt["discord_message_id"] is None
    assert await env.db.list_pending_message_discord_notifications() == [message.id]
    restarted = fake_bot(env.db)
    await retry_discord_user_messages(restarted)
    assert restarted._send_message.await_count == 1
    receipt = await env.db.get_message_discord_receipt(message.id)
    assert receipt["discord_channel_id"] == "999"
    assert receipt["discord_message_id"] == "800"
    assert await env.db.list_pending_message_discord_notifications() == []
    notifications = DiscordNotificationHandler(restarted, env.bus)
    try:
        await env.bus.emit("message.sent", {
            "message_id": message.id, "project_id": "p", "from_kind": "session",
            "from_id": env.row.id, "to_kind": "user", "to_id": "user",
        })
        await env.bus.emit("message.delivered", {
            "message_id": message.id, "project_id": "p", "method": "platform",
        })
    finally:
        notifications.shutdown()
    assert restarted._send_message.await_count == 1
