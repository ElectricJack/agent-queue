# Legacy Chat Removal (Supervisor Cutover Finish) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the in-process Discord chat agent so supervisor *sessions* (tmux) are the only chat path, and delete the `legacy_chat` config flag.

**Architecture:** The Discord bot stops constructing its own `Supervisor` and stops calling `Supervisor.chat()`. All incoming chat routes through `message_send` to the per-project supervisor session (already implemented; currently gated behind `supervisor_agent.legacy_chat=false` + channel binding). Unbound channels get a graceful notice instead of a silent Gemini fallback. `Supervisor` itself, `config.chat_provider`, and `Supervisor.chat()` **stay** — playbook runner (`src/playbooks/runner.py`), plugin `invoke_llm` fallback (`src/orchestrator/core.py`), and `runtime: supervisor` tasks still use them.

**Tech Stack:** Python 3.12, discord.py, pytest (`pytest tests/ -n auto`).

## Global Constraints

- `Supervisor.chat()` (src/runtimes/supervisor.py:777) is NOT removed — only the Discord bot's use of it.
- `config.chat_provider` (ChatProviderConfig) is NOT removed — it remains the utility-LLM config.
- `supervisor_agent.enabled` remains; only `legacy_chat` is deleted.
- The bot must never construct a `Supervisor` or any chat provider.
- All state changes go through CommandHandler; commands return `{"success": bool, ...}` dicts.
- Line length 100 (ruff, py312). Async-first.
- Work on branch `chat-cutover`, never on main.

---

### Task 1: Delete the `legacy_chat` flag (config + main.py)

**Files:**
- Modify: `src/config.py:1065` (field), `src/config.py:2218` (loader)
- Modify: `src/main.py:102-130` (skip-init block)
- Test: `tests/test_supervisor_cutover.py` (config-flag tests)

**Interfaces:**
- Produces: `SupervisorAgentConfig` without `legacy_chat`. Task 2 relies on this: routing no longer consults `legacy_chat`.

- [ ] **Step 1: Write/adjust failing tests**

In `tests/test_supervisor_cutover.py`, find tests that construct configs with `legacy_chat` or assert both flag paths. Replace flag-matrix tests with:

```python
def test_supervisor_agent_config_has_no_legacy_chat():
    from src.config import SupervisorAgentConfig
    cfg = SupervisorAgentConfig()
    assert not hasattr(cfg, "legacy_chat")


def test_config_loader_ignores_legacy_chat_key(tmp_path):
    # A YAML file still carrying legacy_chat must load without error.
    # Reuse the file-based config-loading pattern already used in this
    # test module / tests/test_config.py (write minimal yaml with
    # supervisor_agent: {enabled: true, legacy_chat: false}, call load_config,
    # assert cfg.supervisor_agent.enabled is True).
    ...
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_supervisor_cutover.py -v` — new tests FAIL (attribute exists).

- [ ] **Step 3: Implement**

`src/config.py`:
- Delete the field at :1065 (`legacy_chat: bool = True  # keep Supervisor.chat() wiring until cutover`).
- At :2218 delete `legacy_chat=bool(sa.get("legacy_chat", True)),` from the `SupervisorAgentConfig(...)` construction. Do NOT reject unknown `legacy_chat` keys in YAML — silently ignore (old configs must keep loading).

`src/main.py:102-130`: delete the `_sa_cfg` / `_skip_supervisor_chat_init` block and its comment; keep an unconditional:

```python
    # Initialise the shared Supervisor's chat provider.  Non-fatal: chat runs
    # via supervisor sessions; this provider only serves supervisor-runtime
    # tasks, playbooks, and the plugin invoke_llm fallback.
    if not shared_supervisor.initialize():
        logger.warning(
            "Shared Supervisor: chat provider failed to initialise — "
            "supervisor-runtime tasks and playbook LLM calls will fail until "
            "credentials are configured"
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor_cutover.py tests/test_config.py -v` — PASS. Fix any other test referencing `legacy_chat` (grep `tests/` for `legacy_chat`).

- [ ] **Step 5: Commit**

```bash
git add src/config.py src/main.py tests/
git commit -m "feat(cutover): delete supervisor_agent.legacy_chat flag"
```

### Task 2: Discord bot — session routing only, no in-process chat agent

**Files:**
- Modify: `src/discord/bot.py`
- Test: `tests/test_supervisor_cutover.py`, plus whichever existing bot tests construct `AgentQueueBot` (grep `tests/` for `AgentQueueBot`)

**Interfaces:**
- Consumes: `SupervisorAgentConfig` without `legacy_chat` (Task 1).
- Produces: `AgentQueueBot` with no `self.agent`; a `handler` property returning `self.orchestrator._command_handler`; `supervisor_session_routing_enabled(config)` reduced to `supervisor_agent.enabled` only (keep the function name — it is imported by tests).

Detailed changes (implementer: read the file first; line numbers are pre-edit):

1. **Constructor (bot.py:93-105):** delete `self.agent = Supervisor(...)` (:99) and the two callback wirings on `self.agent.handler` (:102, :105). Remove the now-unused `Supervisor` import. Add:

```python
    @property
    def handler(self):
        """Daemon-wide CommandHandler (wired by main.py before transports start)."""
        return self.orchestrator._command_handler
```

2. **setup_hook / on_ready wiring (bot.py:368-386):** replace the `if not self.orchestrator._command_handler: set_command_handler/set_supervisor` block (:372-375) and the whole "Initialize LLM client via Supervisor" block (:377-386) with callback wiring on the shared handler:

```python
                # main.py wires the daemon-wide CommandHandler/Supervisor
                # before transports start; register bot callbacks on it.
                if self.handler is not None:
                    self.handler._on_project_deleted = self.clear_project_channels
                    self.handler._on_project_created = self._on_project_created
```

3. **Other `self.agent` call sites:** replace `self.agent.handler.execute(` with `self.handler.execute(` at :222, :932-934, :1237. Delete the `set_active_project` block (:1212-1218) — active-project context was for the in-process chat brain only.

4. **`supervisor_session_routing_enabled` (bot.py:66-79):** body becomes:

```python
def supervisor_session_routing_enabled(config: Any) -> bool:
    """Chat routes to supervisor sessions when the supervisor agent is enabled."""
    sa = getattr(config, "supervisor_agent", None)
    return bool(getattr(sa, "enabled", False))
```

Update its docstring and the module docstring (bot.py:31-39) — no more legacy path.

5. **on_message (bot.py:1046-1266) rewrite of the routing tail.** Keep everything through text/attachment assembly (:1046-1139) and the per-channel lock + contextvars (:1141-1148). Delete: cold-model check (:1149-1154), `thinking_msg`/`thinking_view`/`tool_names_used`/`response` locals, `is_ready` gate (:1161-1167), `llm_context` build (:1169-1189), `set_active_project` (:1212-1218), `history` (:1220-1223), the `use_supervisor_session` conditional (:1225-1228), and the entire legacy branch from :1268 to the end of the legacy chat section (~:1399 — everything that calls `self.agent.chat()`, updates the thinking message, or handles chat retries). Keep the reply-quoting block (:1191-1210) — the quoted text still helps the session supervisor. New tail inside the lock:

```python
            if not supervisor_session_routing_enabled(self.config):
                await message.reply(
                    "Chat is disabled — enable `supervisor_agent` in config.yaml "
                    "to talk to the project supervisor."
                )
                return

            if project_channel_id is None:
                known = ", ".join(f"`#{ch.name}`" for ch in self._project_channels.values())
                hint = f" Project channels: {known}." if known else (
                    " No project channels are bound yet — create a project or use "
                    "`set_project_channel` to bind one."
                )
                await message.reply(
                    "This channel isn't bound to a project, so there's no supervisor "
                    f"session to route to.{hint}"
                )
                return

            send_result = await self.handler.execute(
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
            if isinstance(send_result, dict) and "error" in send_result:
                await self._send_long_message(
                    message.channel,
                    f"**Message queue error:** {send_result['error']}",
                    reply_to=message,
                )
                return
            try:
                await self._safe_api_call(
                    message.add_reaction("\U0001f4ec"),
                    critical=False,
                    context="on_message ack reaction",
                )
            except Exception:
                pass  # fail-open
```

Note `user_text` assembly (mention-strip + attachments + reply-quote) must still happen before this tail; keep `async with message.channel.typing():` only if trivially retained, otherwise drop it (the reply is immediate).

6. **ThinkingView:** delete the `ThinkingView` class (bot.py:~409-450) and any remaining references (grep the file for `ThinkingView`, `thinking_msg`, `tool_names_used`, `reload_credentials`, `is_model_loaded`, `is_ready`, `self.agent`). Zero occurrences of `self.agent` must remain.

- [ ] **Step 1: Write failing tests** in `tests/test_supervisor_cutover.py`:

```python
def test_bot_has_no_inprocess_chat_agent():
    import inspect
    import src.discord.bot as botmod
    src_text = inspect.getsource(botmod)
    assert "self.agent" not in src_text
    assert ".chat(" not in src_text  # bot never calls Supervisor.chat


def test_routing_enabled_depends_only_on_enabled_flag():
    from types import SimpleNamespace
    from src.discord.bot import supervisor_session_routing_enabled
    cfg = SimpleNamespace(supervisor_agent=SimpleNamespace(enabled=True))
    assert supervisor_session_routing_enabled(cfg) is True
    cfg2 = SimpleNamespace(supervisor_agent=SimpleNamespace(enabled=False))
    assert supervisor_session_routing_enabled(cfg2) is False
```

Also add/adapt an async unbound-channel test following this module's existing bot-test pattern: message in the global bot channel (project_channel_id None, routing enabled) → `message.reply` called with text containing "isn't bound to a project", `message_send` NOT executed.

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_supervisor_cutover.py -v`
- [ ] **Step 3: Implement** per the numbered changes above.
- [ ] **Step 4: Run** `pytest tests/test_supervisor_cutover.py -v` then any bot test files found by `grep -rl AgentQueueBot tests/` — all PASS. Also `ruff check src/discord/bot.py`.
- [ ] **Step 5: Commit**

```bash
git add src/discord/bot.py tests/
git commit -m "feat(cutover): Discord chat routes only to supervisor sessions"
```

### Task 3: Sweep, full suite, docs

**Files:**
- Modify: `docs/specs/design/*supervisor*` / wherever `legacy_chat` or the legacy Discord chat path is documented (grep `docs/ src/` for `legacy_chat` and `Supervisor.chat` Discord references)
- Modify: any remaining source referencing the removed path

**Interfaces:** none new.

- [ ] **Step 1: Sweep** — `grep -rn legacy_chat src/ tests/ docs/` and `grep -rn "ThinkingView" src/ tests/`; remove/update every hit (docs: state that Discord chat is session-routed only; `chat_provider` is the utility-LLM config for playbooks/supervisor-runtime).
- [ ] **Step 2: Full suite** — `pytest tests/ -n auto`; expected: all pass. Fix regressions.
- [ ] **Step 3: Commit**

```bash
git add -u
git commit -m "docs(cutover): document session-only Discord chat; sweep legacy_chat refs"
```
