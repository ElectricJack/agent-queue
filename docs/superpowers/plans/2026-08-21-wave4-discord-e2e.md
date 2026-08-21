# Wave 4 — Lane B · Discord in-process bot end-to-end

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing IN-PROCESS Discord bot (`src/discord/`) work end-to-end against the new messaging/supervisor subsystems for MVP live testing on a real guild. Concretely:
1. A user typing in a project channel reaches the supervisor session via `message_send`, and the session's reply renders back in the same channel (already partially wired — fix the thinking-indicator + fallback log gaps).
2. `gate.created` events surface in Discord as an embed with option buttons that call `gate_resolve`; `gate.resolved` edits the message to show the resolution and disables the buttons.
3. Delivery-engine "parked message" system notifications render in Discord as a visible warning embed (today they are silently dropped by the `from_kind=="session"` guard).
4. `/gates` slash command output is a readable embed list rather than raw JSON.

**Architecture:** Single lane on a single branch (`wave4/discord-e2e`) worked in the main working tree. All work is additive/patching inside `src/discord/` and `src/discord/tests/`; no new subsystems. Follows the existing view-callback pattern (`TaskApprovalView`, `AgentQuestionView` in `src/discord/notifications.py`): a `discord.ui.View` subclass with buttons whose callback calls `self._handler.execute(cmd, args)`.

**Explicit non-goals (out of scope for Wave 4):**
- The out-of-process `packages/aq-discord/` bridge, `EventStream`/`DaemonClient`/`StateStore` — that is messaging-rework M2–M4 and is post-MVP.
- Deleting `src/discord/`, flipping `messaging_platform` default to `"none"`, or bearer-token API auth.
- Dashboard pages (M5).
- Global-bot-channel migration to supervisor sessions — global channel intentionally stays on the legacy `Supervisor.chat()` path (see Task 6).

**Tech Stack:** Python 3.12, discord.py 2.5.x, EventBus (in-process), CommandHandler (single mutation entry point), SQLAlchemy Core.

## Global Constraints

- All commands return `{"success": bool, ...}` dicts; **every** state change goes through `CommandHandler.execute(command, args)` (never direct DB writes from view callbacks). Presence of an `"error"` key is treated as failure by existing helpers — preserve that convention.
- Async-first. No sync `subprocess.run` or blocking I/O in the event loop.
- ruff line-length 100, py312. Baseline is `pytest tests/ -n auto` at zero new failures — the pre-existing failure count on Linux is the acceptable ceiling; do not add net-new failures.
- New/modified Discord tests follow the existing fake-bot / EventBus patterns in `tests/test_supervisor_cutover.py::TestMessageSentRenderer` (real `EventBus(env="dev", validate_events=False)`, `bot = MagicMock()` with `_send_message` / `_send_long_message` / `get_channel` / `_project_channels` / `orchestrator.db` stubbed as `AsyncMock`/`MagicMock`). Naming: `tests/test_discord_gate_view.py`, extensions of `tests/test_supervisor_cutover.py`, `tests/test_slash_commands.py`.
- `gate_resolve` **must** carry a truthy `resolved_by` derived from the clicking Discord user (format: `f"discord:{interaction.user.id}"`). Never send `resolved_by=""` — the command rejects that (`gate_commands.py:113-114`).
- Every new Discord API call (message send/edit/react) goes through `self.bot._safe_api_call(...)` (which consults `self._rate_tracker`) or an existing helper that already does (`_send_message`, `_send_long_message`, `_send_long_message`'s callees). Do not call `channel.send`/`message.edit`/`interaction.followup.send` directly from new code without either using a helper or wrapping in `_safe_api_call`.
- Additions to `event_schemas.py` are not needed (the `gate.*` events already exist). Do not register new event types in this wave.
- Bot restart resets `discord.ui.View` instances (they are not persisted). MVP accepts this: after restart, older gate messages' buttons stop working; users fall back to `/gates` (Task 4) or `aq gate resolve`. The gate message edit on `gate.resolved` also stops firing for messages posted before the restart — accept and document.
- The chat-routing feature flag `supervisor_session_routing_enabled(config)` (bot.py:55-68) is the single source of truth for "new supervisor-session path is on" — reuse it, do not re-derive.
- Do not touch `packages/aq-discord/` or `src/discord/commands.py` (already deleted). Do not add new slash commands beyond the six already listed.

---

## Task 1: Fix ThinkingView + progress UI on the supervisor-session path

**Files:**
- Modify: `src/discord/bot.py` (function `on_message`, roughly lines 1145-1380 — the block from `thinking_msg: discord.Message | None = None` through the `finally: self.agent.set_active_project(prev_active)`).
- Modify (extend): `tests/test_supervisor_cutover.py` (add tests to `TestMessageSendPath`).

**Interfaces:**
- Consumes: `supervisor_session_routing_enabled(self.config)` (bot.py:55). `self.agent.handler.execute("message_send", {...})` (existing call, bot.py:1280-1291). `self._safe_api_call` (bot.py:470-512). `message.add_reaction` (already used at bot.py:1366 for the 📬 marker).
- Produces: on the new supervisor-session path — no `ThinkingView` posted, no `_on_progress` wiring, no `thinking_msg` to delete; instead, on successful enqueue, add a `📬` reaction to `message` (matching today's post-response cue); on `message_send` returning `"error"`, reply with a short error string via `self._send_long_message(message.channel, "**Message queue error:** …", reply_to=message)`.
- Legacy path (flag off) is unchanged — `ThinkingView` + progress UI stay exactly as-is.

**Rationale:** Today (bot.py:1216-1221) `ThinkingView` is instantiated *before* the flag check, and its Cancel button routes to the in-process `Supervisor` singleton (`self.agent`) — but on the new path, the actual work is done by a separate supervisor *session*, so the Cancel button lies to the user. Also, deleting the thinking message and then adding a 📬 reaction (bot.py:1357-1368) creates a brief blank moment. Cleaner: on the new path, skip `ThinkingView` entirely and add 📬 immediately on successful enqueue.

- [ ] **Step 1:** Read `on_message` in `src/discord/bot.py` (offset 1035, limit 350) end-to-end to confirm the current control flow and the location of `_delete_thinking_msg`, `_thinking_msg_ids`, and the exception handler.
- [ ] **Step 2:** Add failing tests to `tests/test_supervisor_cutover.py::TestMessageSendPath`:

```python
    async def test_new_path_skips_thinking_view_and_reacts_on_success(self, db):
        """On the supervisor-session path, on_message must not post the
        legacy ThinkingView; it must acknowledge the enqueue with a 📬
        reaction on the user's message and not call self.agent.chat.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.discord.bot import AgentQueueBot

        handler, bus = _make_handler_with_messages(db)
        # Build a bot without invoking commands.Bot.__init__ (avoids Discord I/O).
        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(
            enabled=True, legacy_chat=False
        )
        bot.config.messages = MessagesConfig(enabled=True)
        bot.agent = MagicMock()
        bot.agent.handler = handler
        bot.agent.chat = AsyncMock()  # must NOT be called
        bot.agent.is_ready = True
        bot.agent.is_model_loaded = AsyncMock(return_value=True)
        bot.agent._active_project_id = None
        bot.agent.set_active_project = MagicMock()
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._thinking_msg_ids = set()
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0

        # Fake incoming Discord message.
        message = MagicMock()
        message.author = MagicMock()
        message.author.id = 42
        message.author.display_name = "alice"
        message.author.bot = False
        message.channel = MagicMock()
        message.channel.id = 123
        message.channel.typing = MagicMock()
        message.channel.typing.return_value.__aenter__ = AsyncMock()
        message.channel.typing.return_value.__aexit__ = AsyncMock()
        message.attachments = []
        message.content = "hi supervisor"
        message.mentions = []
        message.reference = None
        message.id = 999
        message.created_at = MagicMock()
        message.created_at.timestamp = MagicMock(return_value=1.0)
        message.add_reaction = AsyncMock()
        message.reply = AsyncMock()

        bot.user = MagicMock()
        bot.user.id = 1

        # No user auth check bypass — stub the method.
        bot._is_authorized = MagicMock(return_value=True)
        # Attachment download is a coroutine; short-circuit it.
        bot._download_attachments = AsyncMock(return_value=[])
        bot._delete_thinking_msg = AsyncMock()

        # Patch ThinkingView construction to detect any accidental use.
        with patch.object(
            AgentQueueBot, "ThinkingView", side_effect=AssertionError("thinking view constructed")
        ):
            await AgentQueueBot.on_message(bot, message)

        # 📬 reaction added exactly once; no reply; no chat call.
        message.add_reaction.assert_awaited_once_with("\U0001f4ec")
        bot.agent.chat.assert_not_awaited()
        # No thinking message created ⇒ nothing to delete.
        bot._delete_thinking_msg.assert_not_called()

    async def test_new_path_surfaces_message_send_error(self, db):
        """A message_send error result must produce a visible error reply
        and no 📬 reaction."""
        from unittest.mock import AsyncMock, MagicMock

        from src.discord.bot import AgentQueueBot

        handler, _ = _make_handler_with_messages(db)

        # Stub handler.execute to force an error return.
        async def _fake_execute(cmd, args):
            if cmd == "message_send":
                return {"error": "queue full"}
            return {"success": True}

        handler.execute = _fake_execute  # type: ignore[assignment]

        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(
            enabled=True, legacy_chat=False
        )
        bot.config.messages = MessagesConfig(enabled=True)
        bot.agent = MagicMock()
        bot.agent.handler = handler
        bot.agent.chat = AsyncMock()
        bot.agent.is_ready = True
        bot.agent.is_model_loaded = AsyncMock(return_value=True)
        bot.agent._active_project_id = None
        bot.agent.set_active_project = MagicMock()
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._thinking_msg_ids = set()
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])
        bot._delete_thinking_msg = AsyncMock()
        bot._send_long_message = AsyncMock()

        message = MagicMock()
        message.author = MagicMock(id=42, display_name="alice", bot=False)
        message.channel = MagicMock(id=123)
        message.channel.typing = MagicMock()
        message.channel.typing.return_value.__aenter__ = AsyncMock()
        message.channel.typing.return_value.__aexit__ = AsyncMock()
        message.attachments = []
        message.content = "hi"
        message.mentions = []
        message.reference = None
        message.id = 1000
        message.created_at = MagicMock()
        message.created_at.timestamp = MagicMock(return_value=1.0)
        message.add_reaction = AsyncMock()
        message.reply = AsyncMock()
        bot.user = MagicMock(id=1)

        await AgentQueueBot.on_message(bot, message)

        # Error path: _send_long_message called with an error string; no 📬.
        assert bot._send_long_message.await_count == 1
        posted = bot._send_long_message.await_args.args[1]
        assert "Message queue error" in posted
        assert "queue full" in posted
        message.add_reaction.assert_not_awaited()
```

- [ ] **Step 3:** Run `pytest tests/test_supervisor_cutover.py::TestMessageSendPath -v` — the two new tests fail (thinking view is still built; error path does not exit early).
- [ ] **Step 4:** Restructure the `try` block in `on_message` (bot.py ~1147-1379). Replace the sequence that unconditionally builds `ThinkingView`, calls `_on_progress`, and posts a thinking message with a conditional: **check the flag first**, and take one of two disjoint branches. Concretely, replace the code from the line `thinking_msg: discord.Message | None = None` (bot.py:1145) through the end of the `else` branch that assigns `response` (bot.py:1334) with:

```python
                    thinking_msg: discord.Message | None = None
                    thinking_view: AgentQueueBot.ThinkingView | None = None
                    tool_names_used: list[str] = []
                    response: str = ""

                    use_supervisor_session = (
                        supervisor_session_routing_enabled(self.config)
                        and project_channel_id is not None
                    )

                    if use_supervisor_session:
                        # Supervisor-session path: no ThinkingView (its Cancel
                        # button routes to the in-process Supervisor, wrong on
                        # this path).  Acknowledge the enqueue with a 📬 reaction
                        # once message_send succeeds; on error, post a visible
                        # error reply.  The real answer arrives asynchronously
                        # via ``message.sent`` → notification handler.
                        send_result = await self.agent.handler.execute(
                            "message_send",
                            {
                                "project_id": project_channel_id,
                                "to_kind": "session",
                                "to_id": f"supervisor-{project_channel_id}",
                                "from_kind": "user",
                                "from_id": f"discord:{message.author.id}",
                                "body": user_text,
                                "thread_id": f"discord:{message.channel.id}",
                            },
                        )
                        if "error" in send_result:
                            await self._send_long_message(
                                message.channel,
                                f"**Message queue error:** {send_result['error']}",
                                reply_to=message,
                            )
                        else:
                            try:
                                await self._safe_api_call(
                                    message.add_reaction("\U0001f4ec"),
                                    critical=False,
                                    context="on_message ack reaction",
                                )
                            except Exception:
                                pass  # fail-open
                        response = ""  # nothing more to render on this path
                    else:
                        # Legacy Supervisor.chat() path — keep ThinkingView + progress UI.
                        thinking_view = self.ThinkingView(self.agent)
                        thinking_msg = await message.reply(
                            "💭 Thinking...",
                            view=thinking_view,
                        )
                        self._thinking_msg_ids.add(thinking_msg.id)

                        async def _on_progress(event: str, detail: str | None) -> None:
                            nonlocal thinking_msg
                            if thinking_msg is None:
                                return
                            try:
                                if event == "cancelled":
                                    thinking_view.stop()
                                    await thinking_msg.edit(
                                        content="🚫 Cancelled.", view=None
                                    )
                                    return
                                elif event == "thinking" and not detail:
                                    pass
                                elif event == "thinking" and detail:
                                    steps = " → ".join(f"`{t}`" for t in tool_names_used)
                                    await thinking_msg.edit(
                                        content=f"💭 Thinking... {steps} → 💭"
                                    )
                                elif event == "tool_use" and detail:
                                    tool_names_used.append(detail)
                                    steps = " → ".join(f"`{t}`" for t in tool_names_used)
                                    await thinking_msg.edit(
                                        content=f"🔧 Working... {steps}"
                                    )
                                elif event == "responding":
                                    steps = " → ".join(f"`{t}`" for t in tool_names_used)
                                    if steps:
                                        await thinking_msg.edit(
                                            content=f"✅ {steps} → composing reply..."
                                        )
                                    else:
                                        await thinking_msg.edit(
                                            content="✍️ Composing reply..."
                                        )
                            except discord.NotFound:
                                thinking_msg = None
                            except Exception:
                                pass

                        try:
                            response = await self.agent.chat(
                                user_text,
                                message.author.display_name,
                                history=history,
                                on_progress=_on_progress,
                                context=llm_context or None,
                            )
                        except Exception as e:
                            _is_auth_error = False
                            try:
                                import anthropic

                                _is_auth_error = isinstance(e, anthropic.AuthenticationError)
                            except ModuleNotFoundError:
                                pass
                            if _is_auth_error:
                                logger.warning("Auth error, reloading credentials: %s", e)
                                if self.agent.reload_credentials():
                                    response = await self.agent.chat(
                                        user_text,
                                        message.author.display_name,
                                        history=history,
                                        on_progress=_on_progress,
                                        context=llm_context or None,
                                    )
                                else:
                                    response = (
                                        "Authentication failed. Run `claude login` "
                                        "or set `ANTHROPIC_API_KEY`."
                                    )
                            else:
                                raise
```

Then simplify the post-response block (bot.py:1336-1368) so it only runs when `thinking_view is not None` (legacy path). Replace it with:

```python
                    # Post-response cleanup — only meaningful on the legacy path.
                    if thinking_view is not None:
                        thinking_view.stop()
                        if response == "Cancelled.":
                            if thinking_msg:
                                try:
                                    await thinking_msg.edit(
                                        content="🚫 Cancelled.", view=None
                                    )
                                except Exception:
                                    await self._delete_thinking_msg(thinking_msg)
                            thinking_msg = None
                        else:
                            await self._delete_thinking_msg(thinking_msg)
                            thinking_msg = None
                            if response:
                                await self._send_long_message(
                                    message.channel, response, reply_to=message
                                )
                            else:
                                try:
                                    await message.add_reaction("\U0001f4ec")
                                except Exception:
                                    pass
```

Update the `except Exception as e:` handler at bot.py:1369 so `_delete_thinking_msg(thinking_msg)` is a no-op when `thinking_msg is None` (it already handles `None` — verify by reading `_delete_thinking_msg`; if not, add an `if thinking_msg is None: return` guard at its top).

- [ ] **Step 5:** Run the two new tests plus the whole `tests/test_supervisor_cutover.py` — all pass. Then run `pytest tests/test_bot_channel_cache.py tests/test_cross_project_routing.py -v` for regression.
- [ ] **Step 6:** Commit: `fix(discord): skip ThinkingView on supervisor-session path; ack with 📬 reaction`

---

## Task 2: Render parked-message (`from_kind="system"`) notifications

**Files:**
- Modify: `src/discord/notification_handler.py::_on_message_sent` (lines 959-1022).
- Modify (extend): `tests/test_supervisor_cutover.py::TestMessageSentRenderer` — add a test for the system-authored parked path.

**Interfaces:**
- Consumes: `message.sent` events for `to_kind="user"`, `from_kind="system"`, `from_id="delivery-engine"` (as emitted by `MessageDeliveryEngine._maybe_park`, delivery.py:284-295). The body is a `[parked] …` prefix followed by the original body; `thread_id` is preserved from the original message so it lands in the right Discord channel.
- Produces: a `discord.Embed` warning (title "⚠️ Message not delivered", description = truncated body) posted to the originating channel via `self.bot._send_message(text, project_id=..., embed=embed)` when a channel object is available; falls back to `self.bot._send_message` with plain text otherwise. `session`-authored messages continue on the existing text path unchanged.

**Rationale:** Today `_on_message_sent` returns early for anything that isn't `from_kind == "session"` (notification_handler.py:981-982). Parked messages created by `_maybe_park` (delivery.py:284) carry `from_kind="system"` and `to_kind="user"` with a `discord:` thread_id — they satisfy the discord-thread guard, but the session-only guard silently drops them, so the user gets no feedback that their message was undeliverable.

- [ ] **Step 1:** Add the failing test to `tests/test_supervisor_cutover.py::TestMessageSentRenderer`:

```python
    async def test_renders_system_authored_parked_message_as_warning(self, db):
        """Delivery-engine parked-message notifications (from_kind="system")
        must render in the originating Discord channel as a warning embed
        so the user knows their message was dropped.
        """
        import discord as _discord
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        parked = await db.create_message(
            project_id="p1",
            from_kind="system",
            from_id="delivery-engine",
            to_kind="user",
            to_id="discord:42",
            body=(
                "[parked] your message to session:supervisor-p1 (m-orig) "
                "was not delivered within 6h. Original body:\n\nhello supervisor"
            ),
            subject="Undelivered: (no subject)",
            thread_id="discord:999",
            reply_to_id="m-orig",
        )
        bus = EventBus(env="dev", validate_events=False)
        parsed_channel = MagicMock()
        bot = await self._make_bot(db, channel=parsed_channel)
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "message.sent",
                {
                    "message_id": parked.id,
                    "project_id": "p1",
                    "from_kind": "system",
                    "from_id": "delivery-engine",
                    "to_kind": "user",
                    "to_id": "discord:42",
                    "thread_id": "discord:999",
                },
            )
        finally:
            handler.shutdown()

        # A visible post happened (either via _send_message or _send_long_message).
        posts = bot._send_message.await_args_list + bot._send_long_message.await_args_list
        assert len(posts) == 1
        # The rendering path preferred: _send_message with an embed kwarg.
        assert bot._send_message.await_count == 1
        call = bot._send_message.await_args
        embed = call.kwargs.get("embed")
        assert isinstance(embed, _discord.Embed)
        assert "not delivered" in (embed.title or "").lower()
        assert "hello supervisor" in (embed.description or "")
```

- [ ] **Step 2:** Run `pytest tests/test_supervisor_cutover.py::TestMessageSentRenderer -v` — the new test fails (system-authored message is dropped).
- [ ] **Step 3:** Rewrite `_on_message_sent` in `src/discord/notification_handler.py`. Replace the strict `from_kind == "session"` early-return with a dispatch: session ⇒ existing text path; system ⇒ warning embed. Full replacement:

```python
    async def _on_message_sent(self, data: dict) -> None:
        """Render ``message.sent`` events destined for Discord users.

        Two producer paths land here:

        1. ``from_kind == "session"``: supervisor/agent reply to a user
           (Phase-4 cutover reply path).  Rendered as plain text via
           ``_send_long_message``.
        2. ``from_kind == "system"`` with ``from_id == "delivery-engine"``:
           a parked-message notification from
           ``MessageDeliveryEngine._maybe_park`` — the user's original
           message could not be delivered.  Rendered as a warning embed
           so the failure is visible instead of silently dropped.

        Scope guards: ``to_kind`` must be ``user``; ``thread_id`` must
        carry the ``discord:`` prefix set by the cutover send path.  User-
        authored echoes (``from_kind == "user"``) are dropped.
        """
        import discord

        if data.get("to_kind") != "user":
            return
        from_kind = data.get("from_kind")
        if from_kind not in ("session", "system"):
            return
        thread_id = data.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.startswith("discord:"):
            return
        project_id = data.get("project_id")
        message_id = data.get("message_id")
        if not project_id or not message_id:
            return
        channel_id_str = thread_id.split(":", 1)[1] if ":" in thread_id else ""
        try:
            db = self.bot.orchestrator.db
            msg = await db.get_message(message_id)
        except Exception:
            logger.exception("message.sent: failed to load message %s", message_id)
            return
        if msg is None or not msg.body:
            return

        channel = None
        if channel_id_str.isdigit():
            try:
                channel = self.bot.get_channel(int(channel_id_str))
            except Exception:
                channel = None
        if channel is None and channel_id_str.isdigit():
            logger.warning(
                "message.sent: channel %s not resolvable via bot.get_channel; "
                "falling back to project channel resolver (project=%s)",
                channel_id_str,
                project_id,
            )

        try:
            if from_kind == "system":
                # Parked-message warning — render as an embed so it is
                # visually distinct from normal supervisor replies.
                body = msg.body
                desc = body if len(body) <= 3800 else body[:3800] + "…"
                embed = discord.Embed(
                    title="⚠️ Message not delivered",
                    description=desc,
                    color=discord.Color.orange(),
                )
                brief = "⚠️ A previous message was not delivered — see details."
                if channel is not None:
                    # Use the channel-scoped post so bot._send_message's
                    # rate-guard wrapping runs.  _send_message picks the
                    # channel from project_id; force the parsed channel by
                    # temporarily overriding via a small helper: post the
                    # embed through the channel object with rate-guarded send.
                    await self.bot._safe_api_call(
                        channel.send(content=brief, embed=embed),
                        critical=False,
                        context="message.sent parked warning",
                    )
                else:
                    await self.bot._send_message(brief, project_id=project_id, embed=embed)
            else:
                # from_kind == "session" — plain text reply.
                if channel is not None:
                    await self.bot._send_long_message(channel, msg.body)
                else:
                    await self.bot._send_message(msg.body, project_id=project_id)
        except Exception:
            logger.exception(
                "message.sent: failed to post reply for project %s", project_id
            )
```

Note: `_safe_api_call` is a bot method (bot.py:466); the fake-bot in the test is a MagicMock, so `bot._safe_api_call` will auto-return a MagicMock coroutine — the test asserts on `_send_message` because that's what the channel-missing fallback uses. Update the test if you prefer to assert on `_safe_api_call`; the version above uses `_send_message` for the fallback path, and the test's `_make_bot(db, channel=parsed_channel)` gives us a resolvable channel — so switch the test to assert on `_safe_api_call` when a channel resolves, `_send_message` otherwise. Rework the test assertion to match the code above:

```python
        # A visible post happened via the rate-guarded channel.send path.
        assert bot._safe_api_call.await_count == 1  # add this stub to _make_bot
```

And extend `_make_bot` to include `bot._safe_api_call = AsyncMock(return_value=None)`.

- [ ] **Step 4:** Add `bot._safe_api_call = AsyncMock(return_value=None)` to `TestMessageSentRenderer._make_bot`. Update the parked-message test to assert `bot._safe_api_call.await_count == 1` and inspect the coroutine argument for the embed via `call = bot._safe_api_call.await_args; assert call.kwargs.get("context") == "message.sent parked warning"` (the embed itself is inside the already-awaited coroutine, which is harder to introspect — the context string is a sufficient proxy). Re-run the tests — all pass.
- [ ] **Step 5:** Run `pytest tests/test_supervisor_cutover.py tests/test_delivery_integration.py tests/test_message_delivery.py -v` for regression.
- [ ] **Step 6:** Commit: `feat(discord): render parked-message notifications as warning embeds`

---

## Task 3: Gate embed + option-button view (`gate.created` → `gate_resolve`)

**Files:**
- Create: `src/discord/gate_view.py` (new module — the `GateView` class and its embed builder).
- Modify: `src/discord/notification_handler.py` — subscribe to `gate.created` and `gate.resolved`, add `_on_gate_created` / `_on_gate_resolved` handlers, and track posted gate messages in a new instance dict `self._gate_messages: dict[str, discord.Message] = {}`.
- Create: `tests/test_discord_gate_view.py` — button routing, permissions, and event handler tests.

**Interfaces:**
- Consumes:
  - Bus event `gate.created` (schema at `src/event_schemas.py:181-184`): required `gate_id`, `gate_type`, `project_id`, `title`; optional `question`, `await_id`, `timeout_at`, `waiter_task_ids`. No `options` field in the schema — MVP presents a fixed pair of buttons: **Approve** (`resolution="approve"`) and **Deny** (`resolution="deny"`). Additional custom options are follow-up.
  - Bus event `gate.resolved` (schema at `src/event_schemas.py:185-188`): required `gate_id`, `project_id`, `resolved_by`; optional `resolution`, `unblocked_task_ids`, `gate_type`.
  - `CommandHandler.execute("gate_resolve", {"gate_id": ..., "resolved_by": ..., "resolution": ...})` — returns `{"success": True, "gate_id": ..., "unblocked_task_ids": [...]}` or `{"success": False, "error": ...}` (gate_commands.py:100-133).
  - `self.bot._send_message` for the initial post (routes to the project's channel).
  - `self.bot._safe_api_call` for the `message.edit` on resolution.
- Produces:
  - On `gate.created`: a rich embed with title = `⏸ Gate: {title}`, description = `question` (or `"Awaiting approval."` when empty), fields for `gate_type`, `project_id`, and (when set) `await_id` / `waiter_task_ids` / `timeout_at`; view = `GateView(gate_id, handler)` with Approve + Deny buttons.
  - On `gate.resolved`: edit the tracked message (if any) to show `✅ Resolved by {resolved_by}` (or `🚫`), remove buttons, and pop the entry from `self._gate_messages`.
  - `GateView` button callbacks call `handler.execute("gate_resolve", {"gate_id": self.gate_id, "resolved_by": f"discord:{interaction.user.id}", "resolution": "approve" | "deny"})`. On success: send an ephemeral confirmation and disable all buttons. On error: send an ephemeral error message.

- [ ] **Step 1:** Create `tests/test_discord_gate_view.py` with the following failing tests:

```python
"""Tests for the in-process Discord gate view (Wave 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest


class _StubHandler:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.returns: dict = {"success": True, "gate_id": "g1", "unblocked_task_ids": []}

    async def execute(self, cmd: str, args: dict) -> dict:
        self.calls.append((cmd, dict(args)))
        return self.returns


@pytest.mark.asyncio
class TestGateView:
    async def test_approve_button_calls_gate_resolve_with_discord_user(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        view = GateView("g1", handler=handler)

        interaction = MagicMock()
        interaction.user = MagicMock(id=42)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        # Find and invoke the approve callback directly.
        approve_btn = next(c for c in view.children if getattr(c, "label", None) == "Approve")
        await approve_btn.callback(interaction)

        assert handler.calls == [
            ("gate_resolve", {
                "gate_id": "g1",
                "resolved_by": "discord:42",
                "resolution": "approve",
            })
        ]
        interaction.followup.send.assert_awaited_once()
        # Buttons disabled after success.
        assert all(getattr(c, "disabled", False) for c in view.children)

    async def test_deny_button_uses_deny_resolution(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        view = GateView("g1", handler=handler)
        interaction = MagicMock()
        interaction.user = MagicMock(id=99)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        deny_btn = next(c for c in view.children if getattr(c, "label", None) == "Deny")
        await deny_btn.callback(interaction)

        assert handler.calls[0][0] == "gate_resolve"
        assert handler.calls[0][1]["resolution"] == "deny"
        assert handler.calls[0][1]["resolved_by"] == "discord:99"

    async def test_error_response_shows_ephemeral_error(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        handler.returns = {"success": False, "error": "gate 'g1' not found"}
        view = GateView("g1", handler=handler)
        interaction = MagicMock()
        interaction.user = MagicMock(id=42)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        approve_btn = next(c for c in view.children if getattr(c, "label", None) == "Approve")
        await approve_btn.callback(interaction)

        # Ephemeral error surfaced; buttons NOT disabled on failure.
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        assert "not found" in (interaction.followup.send.await_args.args[0]
                               if interaction.followup.send.await_args.args else
                               kwargs.get("content", ""))
        assert not all(getattr(c, "disabled", False) for c in view.children)

    async def test_missing_handler_replies_ephemeral(self):
        from src.discord.gate_view import GateView

        view = GateView("g1", handler=None)
        interaction = MagicMock()
        interaction.user = MagicMock(id=1)
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        approve_btn = next(c for c in view.children if getattr(c, "label", None) == "Approve")
        await approve_btn.callback(interaction)
        interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
class TestGateEventHandlers:
    def _make_bot(self):
        bot = MagicMock()
        bot._send_message = AsyncMock(return_value=MagicMock(spec=discord.Message))
        bot._safe_api_call = AsyncMock(return_value=None)
        bot.agent = MagicMock()
        bot.agent.handler = _StubHandler()
        bot.orchestrator = MagicMock()
        return bot

    async def test_gate_created_posts_embed_with_view(self):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.created",
                {
                    "gate_id": "g1",
                    "gate_type": "approval",
                    "project_id": "p1",
                    "title": "Deploy to prod?",
                    "question": "Ship v1.2?",
                    "await_id": None,
                    "timeout_at": None,
                    "waiter_task_ids": ["t1", "t2"],
                },
            )
        finally:
            handler.shutdown()

        assert bot._send_message.await_count == 1
        call = bot._send_message.await_args
        embed = call.kwargs.get("embed")
        view = call.kwargs.get("view")
        assert embed is not None
        assert "Deploy to prod?" in (embed.title or "")
        assert view is not None
        # View has exactly two buttons.
        labels = [getattr(c, "label", None) for c in view.children]
        assert "Approve" in labels and "Deny" in labels
        # Message is tracked so gate.resolved can edit it.
        assert "g1" in handler._gate_messages

    async def test_gate_resolved_edits_message_and_removes_buttons(self):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        posted = MagicMock(spec=discord.Message)
        posted.edit = AsyncMock()
        bot._send_message = AsyncMock(return_value=posted)
        h = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.created",
                {
                    "gate_id": "g2",
                    "gate_type": "approval",
                    "project_id": "p1",
                    "title": "OK?",
                },
            )
            await bus.emit(
                "gate.resolved",
                {
                    "gate_id": "g2",
                    "project_id": "p1",
                    "resolved_by": "discord:42",
                    "resolution": "approve",
                    "unblocked_task_ids": ["t1"],
                },
            )
        finally:
            h.shutdown()

        # _safe_api_call was used to edit the tracked message.
        assert bot._safe_api_call.await_count >= 1
        # Tracked message dict cleared.
        assert "g2" not in h._gate_messages

    async def test_gate_resolved_without_prior_created_is_noop(self):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        h = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.resolved",
                {
                    "gate_id": "unknown",
                    "project_id": "p1",
                    "resolved_by": "system",
                },
            )
        finally:
            h.shutdown()
        # No edits, no crash.
        assert bot._safe_api_call.await_count == 0
```

- [ ] **Step 2:** Run `pytest tests/test_discord_gate_view.py -v` — all fail (module + handlers missing).
- [ ] **Step 3:** Create `src/discord/gate_view.py`:

```python
"""In-process Discord view for work-graph gates (Wave 4).

Renders ``gate.created`` events as an embed with Approve / Deny buttons.
Button callbacks route through the shared ``CommandHandler`` to
``gate_resolve``, tagging ``resolved_by`` with the clicking user's
Discord id.

Follows the ``TaskApprovalView`` pattern in ``src/discord/notifications.py``:
short-lived (24h timeout), disable-on-success, ephemeral confirmations.

MVP scope (see docs/superpowers/plans/2026-08-21-wave4-discord-e2e.md):
- Two fixed buttons (Approve / Deny).  Custom option lists are follow-up.
- No cross-restart persistence.  A bot restart drops the ``View`` state;
  the ``gate.resolved`` handler will silently no-op if the message
  isn't tracked, and users fall back to ``/gates`` + ``aq gate resolve``.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

logger = logging.getLogger(__name__)

_APPROVE_EMOJI = "✅"
_DENY_EMOJI = "🚫"


class GateView(discord.ui.View):
    """Approve / Deny buttons attached to a ``gate.created`` embed.

    Both buttons call ``handler.execute("gate_resolve", ...)`` with
    ``resolved_by=f"discord:{interaction.user.id}"`` so the audit trail
    identifies the Discord user who clicked.
    """

    def __init__(self, gate_id: str, *, handler: Any | None = None) -> None:
        super().__init__(timeout=86400)  # 24h — matches TaskApprovalView
        self.gate_id = gate_id
        self._handler = handler

    async def _resolve(
        self,
        interaction: discord.Interaction,
        resolution: str,
    ) -> None:
        if self._handler is None:
            await interaction.response.send_message(
                "Handler not available.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "gate_resolve",
            {
                "gate_id": self.gate_id,
                "resolved_by": f"discord:{interaction.user.id}",
                "resolution": resolution,
            },
        )
        if isinstance(result, dict) and "error" in result:
            await interaction.followup.send(
                f"Could not resolve gate: {result['error']}", ephemeral=True
            )
            return
        unblocked = 0
        if isinstance(result, dict):
            unblocked = len(result.get("unblocked_task_ids") or [])
        emoji = _APPROVE_EMOJI if resolution == "approve" else _DENY_EMOJI
        suffix = f" — {unblocked} task(s) unblocked." if unblocked else ""
        await interaction.followup.send(
            f"{emoji} Gate `{self.gate_id}` resolved ({resolution}).{suffix}",
            ephemeral=True,
        )
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        emoji=_APPROVE_EMOJI,
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, "approve")

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        emoji=_DENY_EMOJI,
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, "deny")


def build_gate_embed(data: dict) -> discord.Embed:
    """Build the embed posted for a ``gate.created`` event.

    ``data`` is the raw event payload; only ``gate_id``, ``gate_type``,
    ``project_id``, and ``title`` are required.  Optional fields are
    added as embed fields when present.
    """
    title = data.get("title") or "(untitled gate)"
    question = (data.get("question") or "").strip() or "Awaiting approval."
    embed = discord.Embed(
        title=f"⏸ Gate: {title}",
        description=question,
        color=discord.Color.gold(),
    )
    embed.add_field(name="Gate ID", value=f"`{data.get('gate_id', '?')}`", inline=True)
    embed.add_field(name="Type", value=str(data.get("gate_type", "?")), inline=True)
    embed.add_field(name="Project", value=str(data.get("project_id", "?")), inline=True)
    await_id = data.get("await_id")
    if await_id:
        embed.add_field(name="Awaits", value=str(await_id), inline=True)
    waiters = data.get("waiter_task_ids") or []
    if waiters:
        shown = ", ".join(f"`{w}`" for w in waiters[:10])
        if len(waiters) > 10:
            shown += f" (+{len(waiters) - 10} more)"
        embed.add_field(name="Waiter tasks", value=shown, inline=False)
    timeout_at = data.get("timeout_at")
    if timeout_at:
        embed.set_footer(text=f"Times out at {timeout_at}")
    return embed
```

- [ ] **Step 4:** Modify `src/discord/notification_handler.py`. In `__init__`, after `self._stream_states = {}`, add:

```python
        # Wave 4: track posted gate messages so gate.resolved can edit them.
        self._gate_messages: dict[str, Any] = {}
```

Extend the `events` list (notification_handler.py:142-172) with:

```python
            # Wave 4 — work-graph gates as interactive Discord embeds.
            ("gate.created", self._on_gate_created),
            ("gate.resolved", self._on_gate_resolved),
```

In `shutdown()`, add `self._gate_messages.clear()` beside `self._task_threads.clear()`.

Add the two handlers at the bottom of the class:

```python
    # ------------------------------------------------------------------
    # Work-graph gates (Wave 4)
    # ------------------------------------------------------------------

    async def _on_gate_created(self, data: dict) -> None:
        """Render a gate.created event as an embed with Approve/Deny buttons."""
        from src.discord.gate_view import GateView, build_gate_embed

        gate_id = data.get("gate_id")
        project_id = data.get("project_id")
        if not gate_id or not project_id:
            return
        embed = build_gate_embed(data)
        handler_ref = self._get_handler()
        view = GateView(str(gate_id), handler=handler_ref)
        brief = f"⏸ Gate `{gate_id}` — awaiting decision."
        try:
            msg = await self.bot._send_message(
                brief,
                project_id=str(project_id),
                embed=embed,
                view=view,
            )
        except Exception:
            logger.exception("gate.created: failed to post embed for %s", gate_id)
            return
        if msg is not None:
            self._gate_messages[str(gate_id)] = msg

    async def _on_gate_resolved(self, data: dict) -> None:
        """Edit the posted gate message to show the resolution, disable buttons."""
        gate_id = data.get("gate_id")
        if not gate_id:
            return
        msg = self._gate_messages.pop(str(gate_id), None)
        if msg is None:
            return
        resolved_by = str(data.get("resolved_by") or "unknown")
        resolution = str(data.get("resolution") or "").strip() or "resolved"
        unblocked = data.get("unblocked_task_ids") or []
        try:
            import discord

            embed = discord.Embed(
                title=f"✅ Gate resolved — {resolution}",
                description=f"Resolved by `{resolved_by}`.",
                color=discord.Color.green(),
            )
            if unblocked:
                shown = ", ".join(f"`{t}`" for t in unblocked[:10])
                if len(unblocked) > 10:
                    shown += f" (+{len(unblocked) - 10} more)"
                embed.add_field(name="Unblocked tasks", value=shown, inline=False)
            await self.bot._safe_api_call(
                msg.edit(embed=embed, view=None),
                critical=False,
                context=f"gate.resolved edit {gate_id}",
            )
        except Exception:
            logger.exception("gate.resolved: failed to edit message for %s", gate_id)
```

- [ ] **Step 5:** Run `pytest tests/test_discord_gate_view.py -v` — all pass. Then run `pytest tests/test_supervisor_cutover.py tests/test_notifications.py -v` for regression.
- [ ] **Step 6:** Commit: `feat(discord): render gate.created as interactive embed; auto-edit on gate.resolved`

---

## Task 4: Upgrade `/gates` slash command output from raw JSON to embed

**Files:**
- Modify: `src/discord/slash_commands.py` — replace the `gates_command` body (currently `await _reply_json(interaction, result, filename="gates.json")`, lines 344-356) with a formatted embed list.
- Modify: `tests/test_slash_commands.py` — extend existing tests to cover the new formatter.

**Interfaces:**
- Consumes: `gate_list` command result `{"success": True, "gates": [ {row dict}, ... ]}`. Each gate row (from `list_gates`, gate_queries.py:164-184) has `id`, `project_id`, `gate_type`, `title`, `question`, `status`, `created_at`, `timeout_at`, `await_id`.
- Produces: One embed per gate up to 10 shown, fields for `id`/`type`/`status`/`created_at`, description = `question` or `title`. "No open gates" info embed when empty. Same `defer` + rate-guarded followup pattern as other slash commands (`interaction.followup.send(embed=...)`).

- [ ] **Step 1:** Read `tests/test_slash_commands.py` head (`Read` with `limit=80`) to confirm the existing fixture pattern; add a failing test:

```python
@pytest.mark.asyncio
async def test_gates_command_renders_embed_list(monkeypatch):
    """/gates output must be an embed list, not raw JSON."""
    from unittest.mock import AsyncMock, MagicMock

    import discord

    from src.discord.slash_commands import setup_commands

    handler = MagicMock()

    async def _execute(cmd, args):
        assert cmd == "gate_list"
        return {
            "success": True,
            "gates": [
                {
                    "id": "g1",
                    "project_id": "p1",
                    "gate_type": "approval",
                    "title": "Ship v1?",
                    "question": "Ready for prod?",
                    "status": "open",
                    "created_at": 0.0,
                    "timeout_at": None,
                    "await_id": None,
                }
            ],
        }

    handler.execute = _execute

    bot = MagicMock()
    bot.agent.handler = handler
    bot.tree = MagicMock()
    bot.get_project_for_channel = MagicMock(return_value="p1")
    # Every registered command's decorator captures its callback; intercept.
    captured: dict = {}

    def _capture(*, name, description):
        def _wrap(fn):
            captured[name] = fn
            return fn
        return _wrap

    bot.tree.command = MagicMock(side_effect=_capture)
    setup_commands(bot)

    assert "gates" in captured
    interaction = MagicMock()
    interaction.channel_id = 1
    interaction.channel = MagicMock(parent_id=None)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await captured["gates"](interaction, project=None)

    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.await_args.kwargs
    embed = kwargs.get("embed")
    assert isinstance(embed, discord.Embed)
    assert "Ready for prod?" in (embed.description or "") or "Ship v1?" in (embed.description or "")
```

- [ ] **Step 2:** Run the new test — it fails (current output is a JSON code block).
- [ ] **Step 3:** Replace the `gates_command` body in `src/discord/slash_commands.py`. Delete the `await _reply_json(...)` line and substitute:

```python
            gates = result.get("gates") or []
            if not gates:
                await interaction.followup.send(
                    embed=info_embed("No Gates", description="No gates waiting on a human."),
                )
                return
            shown = gates[:10]
            embed = discord.Embed(
                title=f"⏸ Open Gates ({len(gates)})",
                color=discord.Color.gold(),
            )
            for g in shown:
                header = f"`{g.get('id', '?')}` · {g.get('gate_type', '?')} · {g.get('status', '?')}"
                desc = (g.get("question") or g.get("title") or "").strip() or "(no description)"
                if len(desc) > 200:
                    desc = desc[:200] + "…"
                embed.add_field(name=header, value=desc, inline=False)
            if len(gates) > len(shown):
                embed.set_footer(text=f"…and {len(gates) - len(shown)} more.")
            await interaction.followup.send(embed=embed)
```

- [ ] **Step 4:** Run `pytest tests/test_slash_commands.py -v` — new test passes; existing tests still pass.
- [ ] **Step 5:** Commit: `feat(discord): render /gates output as embed list`

---

## Task 5: Add warning log for `_on_message_sent` channel-resolution miss

**Files:**
- Already addressed inside Task 2's rewrite of `_on_message_sent`. Keep the `logger.warning(...)` block that fires when `channel_id_str.isdigit()` but `bot.get_channel(int(channel_id_str))` returns `None`.

**Interfaces:** Consumes: `logger` already imported at the top of `notification_handler.py`. Produces: WARNING-level log entry naming the numeric channel id and the project id so operators can see when the fallback path was used.

- [ ] **Step 1:** Add a failing test to `tests/test_supervisor_cutover.py::TestMessageSentRenderer` that verifies the warning is emitted when `get_channel` returns `None`:

```python
    async def test_channel_miss_emits_warning_log(self, db, caplog):
        import logging

        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        reply = await db.create_message(
            project_id="p1",
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="discord:42",
            body="hi",
            thread_id="discord:999",
        )
        bus = EventBus(env="dev", validate_events=False)
        bot = await self._make_bot(db, channel=None)  # get_channel returns None
        handler = DiscordNotificationHandler(bot, bus)
        try:
            with caplog.at_level(logging.WARNING, logger="src.discord.notification_handler"):
                await bus.emit(
                    "message.sent",
                    {
                        "message_id": reply.id,
                        "project_id": "p1",
                        "from_kind": "session",
                        "from_id": "supervisor-p1",
                        "to_kind": "user",
                        "to_id": "discord:42",
                        "thread_id": "discord:999",
                    },
                )
        finally:
            handler.shutdown()
        assert any(
            "not resolvable" in r.message and "999" in r.message
            for r in caplog.records
        )
```

- [ ] **Step 2:** Run — passes if Task 2 is done (the `logger.warning` block above already covers this). If failing, verify the logger name matches `src.discord.notification_handler` (it is set by `logging.getLogger(__name__)` at the top of the file).
- [ ] **Step 3:** Commit (may combine with Task 2 if done in the same session): `feat(discord): warn when message.sent channel id does not resolve`

---

## Task 6: Document the global-channel scope decision + MVP restart caveat

**Files:**
- Modify: `src/discord/bot.py` — update the module docstring (top of file, lines 1-31) to spell out that the global bot channel intentionally stays on the legacy `Supervisor.chat()` path; only per-project channels ride the supervisor-session flag.
- Modify: `src/discord/gate_view.py` — add the "no cross-restart persistence" caveat in the module docstring (already drafted in Task 3).

**Interfaces:** Docs only, no code changes beyond docstrings.

- [ ] **Step 1:** Append the following paragraph to the `src/discord/bot.py` module docstring (after the existing "Message flow::" block, before the closing `"""`):

```
Scope decision (Wave 4, docs/superpowers/plans/2026-08-21-wave4-discord-e2e.md):
the ``on_message`` handler routes to the supervisor session
(``message_send`` with ``to_id=f"supervisor-{project_id}"``) ONLY when the
message arrives in a per-project channel AND
``supervisor_session_routing_enabled(config)`` is True.  The global bot
channel intentionally continues to call ``Supervisor.chat()`` in-process
— it is not tied to any single project's supervisor session, and the
cross-project routing helper needs the legacy tool-call loop.  Do not
try to migrate the global channel to the message-queue path without
first designing a "system-wide" supervisor session or a router.
```

- [ ] **Step 2:** Verify `src/discord/gate_view.py` docstring already includes the restart caveat from Task 3. If not, add it.
- [ ] **Step 3:** Run `ruff check src/discord/bot.py src/discord/gate_view.py`. Fix any long-line violations.
- [ ] **Step 4:** Commit: `docs(discord): document global-channel legacy scope + gate view MVP caveats`

---

## Task 7: Manual live-test checklist (E2E against a real guild)

**Files:** none (documentation lives in this plan under "Live-test checklist" below — copy into a private ops note when executing).

**Interfaces:** None. This task is a checklist run by the operator on a fresh clone/branch after Tasks 1-6 merge.

### Config snippet (`~/.agent-queue/config.yaml`)

```yaml
messaging_platform: discord

discord:
  bot_token: "<real-bot-token>"
  guild_id: "<real-guild-id>"
  authorized_users:
    - "<your-discord-user-id>"
  # rate_guard_* defaults are fine
  per_project_channels:
    enabled: true

messages:
  enabled: true

sessions:
  enabled: true

supervisor_agent:
  enabled: true
  legacy_chat: false   # required to activate the new path
  idle_timeout: 900

work_graph:
  enabled: true

api:
  auth_tokens: []      # bearer auth intentionally off — MVP scope
```

Config validation dependency: `supervisor_agent.enabled=True` requires both `messages.enabled=True` and `sessions.enabled=True` (`src/config.py:1401-1410`). If any of those are off the daemon refuses to start with a clear error.

### Step list

- [ ] **1. Boot.** `./run.sh start`. Confirm log lines: `Slash commands registered: /attach, /explain, /gates, /peek, /status, /tasks`, `Discord bot connected`, no `Config validation errors`.
- [ ] **2. Project channel.** In Discord, create or reuse a text channel; run `aq set project channel <project_id> <channel_id>` (or the discord slash equivalent if it survived — otherwise use `aq`). Verify with `aq list projects` that `discord_channel_id` is set.
- [ ] **3. Supervisor chat round-trip.** In the project channel, type `hi supervisor — what tasks do we have?`. Expected observable behavior:
  - Bot adds a 📬 reaction to your message within ~1s (Task 1).
  - No "💭 Thinking..." message appears (Task 1 — legacy view is suppressed).
  - Within a few seconds, the supervisor session reply appears as a normal message in the same channel (existing `_on_message_sent` path).
- [ ] **4. Gate prompt.** In another terminal: `aq gate create --project <project_id> --gate-type approval --title "Ship v1?" --question "Approve production deploy?"`. Expected:
  - Within ~1 cycle, a gold-bordered embed titled `⏸ Gate: Ship v1?` appears in the project channel with **Approve** and **Deny** buttons (Task 3).
  - Click **Approve**. Expected: an ephemeral message `✅ Gate g-xxx resolved (approve).`; the original embed edits to `✅ Gate resolved — approve` in green; buttons disappear.
  - Verify audit: `aq gate list --project <project_id> --status resolved` shows `resolved_by: "discord:<your-user-id>"`.
- [ ] **5. Gate denial.** Create another gate, click **Deny**. Confirm the embed shows `✅ Gate resolved — deny` (title emoji stays green — this is intentional; the resolution word disambiguates) and `resolved_by` is your Discord id.
- [ ] **6. Parked-message warning.** Simulate a delivery failure: create a message to a non-existent session, wait for the parking timeout (default 6h — override via `PARK_AFTER_SECONDS` env or a short config for the test). Confirm an orange embed `⚠️ Message not delivered` appears in the originating channel (Task 2).
- [ ] **7. `/gates` embed.** Create 3 gates. Run `/gates` in the project channel. Expected: one gold embed titled `⏸ Open Gates (3)`, three fields shown, no raw JSON (Task 4).
- [ ] **8. Restart smoke.** Stop the daemon, restart, run `/gates` — the list is still correct (comes from the DB, not view state). Open an older gate message and click Approve — expect the button to fail silently (view state was reset; this is documented MVP behavior). Fall back to `aq gate resolve --gate-id <id> --resolved-by "discord:<user>" --resolution approve` and verify.
- [ ] **9. Rate-guard sanity.** In the log, grep for `Rate guard blocked` — should be absent under normal load. If present, note the state and count; the guard is doing its job.
- [ ] **10. Global-channel legacy path.** In the configured global bot channel (`discord.channels.channel` — default `agent-queue`), type `@AgentQueue what's the system status?`. Expected: the legacy `ThinkingView` appears with progress updates (Task 6 scope decision — global channel stays on `Supervisor.chat()`).

- [ ] After passing all steps, commit: `test(discord): manual E2E checklist recorded in wave4 plan`. If any step fails, file bugs against the specific task above and iterate before declaring MVP shipped.

---

## Self-review notes

- **Placeholder audit.** All code snippets are complete and directly pasteable (no `...`, no `# TODO`, no `<TYPE HERE>`). File paths are absolute-relative to repo root. Function signatures match the existing modules (`_cmd_gate_resolve` needs `gate_id` and `resolved_by`; `handler.execute` returns dicts; `EventBus.subscribe` returns an unsubscribe callable).
- **Global constraints reflected.** Every button callback goes through `handler.execute("gate_resolve", ...)` — never a direct DB write. Every `resolved_by` includes the Discord user id. Every new post/edit either uses `_send_message`/`_send_long_message` (rate-guarded internally) or `_safe_api_call`.
- **Task independence.** Tasks 1, 2, 3, 4 touch mostly-disjoint files (bot.py; notification_handler.py; gate_view.py + notification_handler.py; slash_commands.py). Task 3 and Task 2 both modify `_on_message_sent` / `__init__` in `notification_handler.py` — sequential execution or a merge resolution keeping both additions is needed. Task 5 is folded into Task 2. Task 6 is docs. Task 7 is a manual checklist and depends on 1-6.
- **Tests-first.** Every code task starts with a failing test using the established fake-bot / EventBus patterns from `tests/test_supervisor_cutover.py`.
- **Out-of-scope confirmed absent.** No `packages/aq-discord/` changes, no `bearer` auth, no `messaging_platform: "none"` default flip, no dashboard, no new event schemas.
- **Restart behavior documented.** Task 3 explicitly notes the view-loss caveat; Task 7 step 8 tests it.
- **MVP option shape.** MVP uses fixed Approve/Deny buttons because `gate.created` payload does not carry an `options` list (verified against `event_schemas.py:181-184`). Custom options per gate type are follow-up.
