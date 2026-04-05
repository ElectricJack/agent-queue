"""Event-driven and periodic hook engine for automated workflows.

Hooks enable the system to react to task lifecycle events or run on a timer
without human intervention.  Each hook follows a simple pipeline::

    trigger -> render prompt template ({{event.*}} placeholders)
    -> invoke supervisor LLM with full tool access

The supervisor has shell, file I/O, and task management tools, so hooks can
create tasks, check status, run commands, etc. -- anything a human user can
do via Discord chat, a hook can do autonomously.

Three trigger types are supported:

- **Periodic**: fires on a timer (``interval_seconds``), checked every
  orchestrator tick (~5s).  Actual firing granularity is bounded by the
  tick interval — a 10s periodic hook will fire every 10-15s, not exactly
  every 10s.  Optionally, a ``schedule`` block can constrain *when* the
  hook is eligible to fire — by time-of-day, day-of-week, day-of-month,
  or cron expression.  See ``src/schedule.py`` for details.
- **Event**: fires when a matching EventBus event arrives (e.g.
  ``task.completed``).  Events are delivered asynchronously via
  ``_on_event``, which re-queries all enabled hooks for matches.
- **Scheduled**: fires once at a specific epoch timestamp (``fire_at``),
  then auto-deletes itself.  Used for deferred one-shot work — e.g.
  "remind me to check the deploy in 30 minutes" or "run this prompt
  tomorrow at 9am".  Created via the ``schedule_hook`` command.

Concurrency and cooldown interaction::

    Hook fires → _last_run_time[hook_id] = now, task added to _running
    ↓
    Subsequent triggers check THREE gates before firing:
      1. Concurrency: len(_running) < max_concurrent_hooks   (global cap)
      2. In-flight:   hook_id not in _running                 (per-hook cap)
      3. Cooldown:    now - _last_run_time[hook_id] >= cooldown_seconds
    ↓
    All three must pass → hook fires again
    ↓
    When task finishes: tick() reaps it from _running,
    freeing the per-hook and global concurrency slots

Tool access in hook LLM calls:

    Each hook invocation creates a fresh Supervisor instance with the same
    tool set that human users have via Discord chat.  This means hooks can:
    - Create tasks (``/add-task``)
    - Query task status
    - Send notifications
    - Use any registered MCP tools
    The Supervisor is stateless — no conversation history carries between
    hook invocations.  The rendered prompt is the entire context.

Integration with the orchestrator:

    The orchestrator creates the HookEngine at ``initialize()`` and calls
    ``hooks.tick()`` every cycle (step 7 of ``run_one_cycle``).  The engine
    holds a back-reference to the orchestrator (via ``set_orchestrator``)
    for LLM invocation (Supervisor creation) and memory search access.

    See ``src/orchestrator.py::initialize()`` and ``run_one_cycle()`` for
    the integration points.
    See ``specs/hooks.md`` for the full specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from src.chat_providers import LoggedChatProvider, create_chat_provider
from src.config import AppConfig, ChatProviderConfig
from src.database import Database
from src.event_bus import EventBus
from src.file_watcher import FileWatcher, WatchRule
from src.logging_config import CorrelationContext
from src.models import Hook, HookRun, ProjectStatus
from src.schedule import matches_schedule, parse_schedule

logger = logging.getLogger(__name__)


class HookEngine:
    """Manages hook lifecycle: scheduling, prompt rendering, LLM invocation.

    The engine subscribes to all EventBus events (wildcard ``*``) so it can
    match event-driven hooks.  Periodic hooks are checked on each ``tick()``
    call from the orchestrator loop.  Running hooks are tracked as asyncio
    Tasks with a configurable concurrency cap.
    """

    def __init__(self, db: Database, bus: EventBus, config: AppConfig):
        self.db = db
        self.bus = bus
        self.config = config
        # In-flight hook executions.  Keyed by hook_id so we can:
        # (a) enforce "one run at a time per hook" — skip if already in-flight,
        # (b) count towards the global max_concurrent_hooks cap,
        # (c) reap finished tasks and surface exceptions in tick().
        self._running: dict[str, asyncio.Task] = {}
        # Tracks when each hook last started (epoch seconds).  Used for both
        # periodic interval checks ("has enough time passed?") and cooldown
        # enforcement ("is the hook still in its cooldown window?").
        # Pre-populated from DB at initialize() to survive daemon restarts.
        self._last_run_time: dict[str, float] = {}
        # FileWatcher for file/folder change monitoring.  Created at
        # initialize() if file_watcher_enabled is True.  Watches are
        # extracted from hook trigger configs that reference file.changed
        # or folder.changed event types.
        self.file_watcher: FileWatcher | None = None
        # Supervisor instance for LLM invocations.  Set via set_supervisor().
        self._supervisor = None

    async def initialize(self) -> None:
        """Subscribe to EventBus for event-driven hooks and restore state.

        Three setup steps:
        1. Register a wildcard EventBus subscriber so ``_on_event`` receives
           every event type.  The method then filters for matching hooks.
        2. Subscribe to ``config.reloaded`` so the hook engine picks up
           changes to ``hook_engine`` settings at runtime.
        3. Pre-populate ``_last_run_time`` from the DB so that periodic hooks
           don't all fire immediately on daemon startup.  Without this, a
           restart would cause every periodic hook to trigger simultaneously
           (because their in-memory last-run timestamps would default to 0).
        """
        self.bus.subscribe("*", self._on_event)
        self.bus.subscribe("config.reloaded", self._on_config_reloaded)
        # Pre-populate last run times from DB.
        # Prefer the hook's own last_triggered_at field (persisted directly on
        # the hook row) over the hook_runs table.  This is faster and survives
        # hook_run pruning.  Fall back to the most recent hook_run for hooks
        # that existed before the last_triggered_at column was added.
        hooks = await self.db.list_hooks(enabled=True)
        for hook in hooks:
            if hook.last_triggered_at:
                self._last_run_time[hook.id] = hook.last_triggered_at
            else:
                last_run = await self.db.get_last_hook_run(hook.id)
                if last_run:
                    self._last_run_time[hook.id] = last_run.started_at

        # Initialize FileWatcher for file/folder change event hooks
        if self.config.hook_engine.file_watcher_enabled:
            self.file_watcher = FileWatcher(
                self.bus,
                debounce_seconds=self.config.hook_engine.file_watcher_debounce_seconds,
                poll_interval=self.config.hook_engine.file_watcher_poll_interval,
            )
            await self._sync_file_watches(hooks)

    async def _on_config_reloaded(self, data: dict) -> None:
        """Handle config.reloaded events — update hook engine config reference."""
        config = data.get("config")
        if config is not None:
            self.config = config

    async def _sync_file_watches(self, hooks: list[Hook] | None = None) -> None:
        """Synchronize FileWatcher rules from hook trigger configs.

        Scans all enabled hooks for ``file.changed`` and ``folder.changed``
        event triggers that include a ``watch`` config block, and registers
        corresponding WatchRules with the FileWatcher.

        Called at initialize() and can be called again to pick up new hooks.
        """
        if not self.file_watcher:
            return

        if hooks is None:
            hooks = await self.db.list_hooks(enabled=True)

        # Track which watch IDs are still active
        active_ids: set[str] = set()

        for hook in hooks:
            trigger = json.loads(hook.trigger)
            if trigger.get("type") != "event":
                continue

            event_type = trigger.get("event_type", "")
            watch_cfg = trigger.get("watch")
            if not watch_cfg:
                continue

            if event_type not in ("file.changed", "folder.changed"):
                continue

            paths = watch_cfg.get("paths", [])
            if not paths:
                continue

            # Resolve base_dir from project workspace if available
            base_dir = watch_cfg.get("base_dir", "")
            if not base_dir:
                try:
                    ws_path = await self.db.get_project_workspace_path(hook.project_id)
                    if ws_path:
                        base_dir = ws_path
                except Exception:
                    pass

            watch_type = "folder" if event_type == "folder.changed" else "file"
            rule = WatchRule(
                watch_id=hook.id,
                project_id=watch_cfg.get("project_id", hook.project_id),
                paths=paths,
                recursive=watch_cfg.get("recursive", False),
                extensions=watch_cfg.get("extensions"),
                watch_type=watch_type,
                base_dir=base_dir,
            )
            self.file_watcher.add_watch(rule)
            active_ids.add(hook.id)

        # Remove watches for hooks that no longer exist or are disabled
        stale_ids = set(self.file_watcher._watches.keys()) - active_ids
        for stale_id in stale_ids:
            self.file_watcher.remove_watch(stale_id)

    async def tick(self) -> None:
        """Called every orchestrator cycle (~5s). Manage hook lifecycle.

        This method performs two duties each tick:

        **1. Reap completed hook tasks** — scan ``_running`` for asyncio
        Tasks that have finished.  Surface any unhandled exceptions as log
        errors (they are otherwise silently swallowed by asyncio).  Remove
        finished tasks from the dict so their hook_id is eligible to fire
        again.

        **2. Check periodic hook schedules** — for each enabled periodic
        hook, compare the current time against its ``interval_seconds`` and
        ``cooldown_seconds``.  If both thresholds are met and the global
        concurrency cap (``max_concurrent_hooks``) has room, launch the
        hook as a new asyncio task.

        Event-driven hooks are NOT checked here — they are triggered
        asynchronously via ``_on_event`` when the EventBus delivers a
        matching event.
        """
        # Phase 1: Reap completed hook tasks and surface exceptions.
        done = [hid for hid, t in self._running.items() if t.done()]
        for hid in done:
            task = self._running.pop(hid)
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.error("Hook task %s failed: %s", hid, exc)

        # Phase 2: Check periodic hooks that are due to fire.
        hooks = await self.db.list_hooks(enabled=True)
        now = time.time()
        max_concurrent = self.config.hook_engine.max_concurrent_hooks

        # Phase 1b: Prune _running entries for hooks that no longer exist in
        # the DB (e.g., deleted and recreated during rule reconciliation).
        # Without this, the orphaned _running entry blocks a concurrency slot
        # and the old asyncio task may keep running alongside the new hook.
        active_hook_ids = {h.id for h in hooks}
        orphaned = [
            hid
            for hid in self._running
            if hid not in active_hook_ids and not self._running[hid].done()
        ]
        for hid in orphaned:
            logger.warning(
                "Cancelling orphaned hook task %s (hook no longer exists in DB)",
                hid,
            )
            self._running[hid].cancel()
        # Also clean up finished orphaned entries
        stale = [
            hid for hid in self._running if hid not in active_hook_ids and self._running[hid].done()
        ]
        for hid in stale:
            self._running.pop(hid, None)

        # Pre-fetch project statuses to skip hooks for paused projects.
        checked_projects: dict[str, bool] = {}  # project_id -> is_paused
        for hook in hooks:
            pid = hook.project_id
            if pid not in checked_projects:
                project = await self.db.get_project(pid)
                checked_projects[pid] = (
                    project is not None and project.status == ProjectStatus.PAUSED
                )

        for hook in hooks:
            if len(self._running) >= max_concurrent:
                break  # Global concurrency cap reached
            if hook.id in self._running:
                continue  # Already in-flight — skip
            if checked_projects.get(hook.project_id, False):
                logger.debug(
                    "Skipping hook %s (%s): project %s is paused",
                    hook.id,
                    hook.name,
                    hook.project_id,
                )
                continue

            trigger = json.loads(hook.trigger)
            trigger_type = trigger.get("type")

            if trigger_type == "periodic":
                interval = trigger.get("interval_seconds", 3600)
                last = self._resolve_last_run(hook)
                if now - last >= interval:
                    if self._check_cooldown(hook, now):
                        # Check schedule constraints (if any)
                        schedule = parse_schedule(trigger)
                        if schedule is not None:
                            now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
                            last_dt = (
                                datetime.fromtimestamp(last, tz=timezone.utc) if last else None
                            )
                            if not matches_schedule(schedule, now=now_dt, last_run=last_dt):
                                continue  # Schedule doesn't match — skip

                        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
                        timing_data: dict = {
                            "current_time": now_iso,
                            "current_time_epoch": now,
                        }
                        if last:
                            timing_data["last_run_time"] = datetime.fromtimestamp(
                                last, tz=timezone.utc
                            ).isoformat()
                            timing_data["last_run_time_epoch"] = last
                            timing_data["seconds_since_last_run"] = now - last
                        self._launch_hook(hook, "periodic", event_data=timing_data)

            elif trigger_type == "scheduled":
                fire_at = trigger.get("fire_at")
                if fire_at is not None and now >= fire_at:
                    if self._check_cooldown(hook, now):
                        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
                        scheduled_iso = datetime.fromtimestamp(fire_at, tz=timezone.utc).isoformat()
                        timing_data: dict = {
                            "current_time": now_iso,
                            "current_time_epoch": now,
                            "scheduled_for": scheduled_iso,
                            "scheduled_for_epoch": fire_at,
                        }
                        self._launch_hook(hook, "scheduled", event_data=timing_data)
                        # Auto-delete the hook after launching (one-shot).
                        # Use fire-and-forget so tick() doesn't block.
                        asyncio.create_task(self._delete_scheduled_hook(hook.id, hook.name))

        # Phase 3: Poll file watcher for filesystem changes.
        # The file watcher emits file.changed / folder.changed events on
        # the EventBus, which are then picked up by _on_event like any
        # other event-driven hook.
        if self.file_watcher:
            try:
                await self.file_watcher.check()
            except Exception as e:
                logger.warning("FileWatcher check failed: %s", e)

    async def _on_event(self, data: dict) -> None:
        """Handle EventBus events for event-driven hooks.

        Called by the EventBus wildcard subscription (``*``) so this method
        receives *every* event.  For each event:

        1. Extract the event type from ``data["_event_type"]``.
        2. Scan all enabled hooks for those with ``trigger.type == "event"``
           and a matching ``trigger.event_type``.
        3. Skip hooks that are already in-flight, on cooldown, or would
           exceed the concurrency cap.
        4. Launch matching hooks with the full event data payload so
           context steps can reference ``{{event.field}}`` placeholders.

        Note: this re-queries enabled hooks from the DB on every event.
        This is acceptable because events are infrequent (task lifecycle
        transitions) and the query is fast (typically <10 hooks).
        """
        event_type = data.get("_event_type", "")
        hooks = await self.db.list_hooks(enabled=True)
        now = time.time()

        # Batch-fetch paused-project status to avoid N queries per event
        checked_projects: dict[str, bool] = {}

        for hook in hooks:
            if hook.id in self._running:
                continue

            trigger = json.loads(hook.trigger)
            if trigger.get("type") != "event":
                continue
            if trigger.get("event_type") != event_type:
                continue

            # Hooks are project-scoped — only fire on events from the same project
            event_project = data.get("project_id", "")
            if event_project and hook.project_id != event_project:
                continue

            # Skip hooks for paused projects (cached per project)
            pid = hook.project_id
            if pid not in checked_projects:
                project = await self.db.get_project(pid)
                checked_projects[pid] = (
                    project is not None and project.status == ProjectStatus.PAUSED
                )
            if checked_projects[pid]:
                logger.debug(
                    "Skipping hook %s (%s): project %s is paused",
                    hook.id,
                    hook.name,
                    pid,
                )
                continue

            if not self._check_cooldown(hook, now):
                continue

            if len(self._running) >= self.config.hook_engine.max_concurrent_hooks:
                break

            self._launch_hook(
                hook,
                f"event:{event_type}",
                event_data=data,
            )

    def _resolve_last_run(self, hook: Hook) -> float:
        """Return the last-run timestamp for a hook, checking both in-memory cache and DB.

        After rule reconciliation, hooks get new UUIDs.  The in-memory
        ``_last_run_time`` dict won't have an entry for the new ID, but
        the hook's ``last_triggered_at`` field (preserved from the old hook
        during reconciliation) still holds the correct timestamp.  Without
        this fallback, newly reconciled hooks would default to epoch 0 and
        fire immediately on every Discord reconnect.
        """
        ts = self._last_run_time.get(hook.id)
        if ts is not None:
            return ts
        # Fallback: use the DB-persisted timestamp from the hook row.
        if hook.last_triggered_at:
            self._last_run_time[hook.id] = hook.last_triggered_at
            return hook.last_triggered_at
        return 0

    def _check_cooldown(self, hook: Hook, now: float) -> bool:
        """Return True if enough time has passed since last run."""
        last = self._resolve_last_run(hook)
        return (now - last) >= hook.cooldown_seconds

    def _launch_hook(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> None:
        """Launch hook execution as a fire-and-forget asyncio task.

        Records the launch time immediately (before the task runs) so that
        cooldown checks in subsequent ticks/events see the hook as recently
        fired.  The timestamp is persisted to the database so it survives
        daemon restarts.  The asyncio Task is stored in ``_running`` so
        ``tick()`` can reap it and surface any exceptions.
        """
        now = time.time()
        self._last_run_time[hook.id] = now
        # Persist to DB so the timestamp survives daemon restarts.
        # Fire-and-forget — errors are logged but don't block hook launch.
        asyncio.create_task(self._persist_last_triggered(hook.id, now))
        task = asyncio.create_task(self._execute_hook(hook, trigger_reason, event_data))
        self._running[hook.id] = task

    async def _persist_last_triggered(self, hook_id: str, ts: float) -> None:
        """Persist last_triggered_at to the hooks table."""
        try:
            await self.db.update_hook(hook_id, last_triggered_at=ts)
        except Exception as e:
            logger.warning("Failed to persist last_triggered_at for hook %s: %s", hook_id, e)

    async def _delete_scheduled_hook(self, hook_id: str, hook_name: str) -> None:
        """Delete a one-shot scheduled hook after it has been launched.

        Called as a fire-and-forget task from ``tick()`` when a scheduled
        hook's ``fire_at`` time has been reached and the hook has been
        launched.  The hook is removed from the database so it won't fire
        again on the next tick.  Run history is preserved.
        """
        try:
            # Only delete the hook row, not its run history — keep the
            # audit trail.  Use update to disable first in case the delete
            # races with another tick.
            await self.db.update_hook(hook_id, enabled=False)
            await self.db.delete_hook(hook_id)
            logger.info(
                "Auto-deleted scheduled hook %s (%s) after firing",
                hook_id,
                hook_name,
            )
        except Exception as e:
            logger.warning("Failed to auto-delete scheduled hook %s: %s", hook_id, e)

    async def _execute_hook(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> None:
        """Execute the full hook pipeline for a single invocation.

        Steps:
        1. Run context-gathering steps (shell, file, http, db, git) sequentially
        2. Check short-circuit conditions -- if a step signals "nothing to do",
           the hook is marked skipped and the LLM is never called (saves tokens)
        3. Render the prompt template with step results and event data
        4. Invoke the LLM via a Supervisor instance with full tool access
        5. Record the run outcome (completed/failed/skipped) in the database
        """
        with CorrelationContext(
            hook_id=hook.id,
            project_id=hook.project_id,
            component="hooks",
        ):
            await self._execute_hook_inner(hook, trigger_reason, event_data)

    async def _execute_hook_inner(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> None:
        """Inner implementation of the hook execution pipeline.

        Runs with the correlation context already set (by ``_execute_hook``).
        The pipeline renders ``{{event.*}}`` placeholders in the prompt
        template, then invokes the supervisor LLM with full tool access.

        Hook output is streamed into a dedicated Discord thread (matching
        task execution behavior), keeping the main channel clean.  The thread
        is created at the start and all progress/results are posted there.
        A brief completion/failure notification is posted back to the main
        channel as a reply to the thread-root message.

        Error handling: any exception is caught, logged, and recorded as a
        "failed" HookRun.  Individual hook failures do not propagate to the
        caller (tick/event handler).
        """
        run = HookRun(
            id=str(uuid.uuid4())[:12],
            hook_id=hook.id,
            project_id=hook.project_id,
            trigger_reason=trigger_reason,
            status="running",
            event_data=json.dumps(event_data) if event_data else None,
            started_at=time.time(),
        )
        await self.db.create_hook_run(run)

        orchestrator = getattr(self, "_orchestrator", None)

        # Notify that a hook is running via the event bus.
        thread_send = None
        try:
            await self.bus.emit("notify.text", {
                "event_type": "notify.text",
                "message": (
                    f"🪝 Hook **{hook.name}** is running "
                    f"(trigger: `{trigger_reason}`)."
                ),
                "project_id": hook.project_id,
            })
        except Exception:
            logger.debug("Failed to emit hook-running notification", exc_info=True)

        # Create a thread for streaming hook output if a thread-creation
        # callback is still available (legacy path for non-bus transports).
        if orchestrator and getattr(orchestrator, "_create_thread", None):
            try:
                thread_name = f"🪝 Hook: {hook.name}"[:100]
                initial_msg = (
                    f"**Hook running** — trigger: `{trigger_reason}`\nProject: `{hook.project_id}`"
                )
                thread_result = await orchestrator._create_thread(
                    thread_name,
                    initial_msg,
                    hook.project_id,
                    None,
                )
                if thread_result:
                    thread_send, _thread_main_notify = thread_result
                    logger.debug(
                        "Created thread for hook %s (%s)",
                        hook.name,
                        hook.id,
                    )
                else:
                    logger.warning(
                        "Thread creation returned None for hook %s",
                        hook.name,
                    )
            except Exception as e:
                logger.error(
                    "Failed to create thread for hook %s: %s",
                    hook.name,
                    e,
                    exc_info=True,
                )
        # (Running notification already sent via event bus above.)

        try:
            # Render prompt (substitute {{event.*}} placeholders)
            prompt = self._render_prompt(hook.prompt_template, event_data)
            await self.db.update_hook_run(run.id, prompt_sent=prompt)

            # Invoke LLM — track tool calls for the completion summary
            tool_labels: list[str] = []

            async def _on_hook_progress(event: str, detail: str | None) -> None:
                if event == "tool_use" and detail:
                    tool_labels.append(detail)
                    # Stream tool-use updates into the thread
                    if thread_send:
                        await thread_send(f"🔧 `{detail}`")

            timeout = self.config.hook_engine.hook_timeout_seconds
            try:
                response, tokens = await asyncio.wait_for(
                    self._invoke_llm(
                        hook,
                        prompt,
                        trigger_reason=trigger_reason,
                        on_progress=_on_hook_progress,
                        event_data=event_data,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                elapsed = int(time.time() - run.started_at)
                logger.warning(
                    "Hook %s timed out after %ds (limit: %ds)",
                    hook.name,
                    elapsed,
                    timeout,
                )
                await self.db.update_hook_run(
                    run.id,
                    status="failed",
                    llm_response=f"Hook execution timed out after {elapsed}s (limit: {timeout}s)",
                    completed_at=time.time(),
                )
                timeout_msg = f"🪝 Hook **{hook.name}** timed out after {elapsed}s."
                if thread_send:
                    try:
                        await thread_send(timeout_msg)
                    except Exception:
                        pass
                try:
                    await self.bus.emit("notify.text", {
                        "event_type": "notify.text",
                        "message": timeout_msg,
                        "project_id": hook.project_id,
                    })
                except Exception:
                    pass
                return

            await self.db.update_hook_run(
                run.id,
                status="completed",
                llm_response=response,
                tokens_used=tokens,
                completed_at=time.time(),
            )
            logger.info("Hook %s completed", hook.name, extra={"tokens": tokens})

            # Build completion message
            parts = [f"🪝 Hook **{hook.name}** completed."]
            if tool_labels:
                chain = " → ".join(f"`{t}`" for t in tool_labels)
                parts.append(f"🔧 {chain}")
            if response:
                summary = response if len(response) <= 4000 else response[:4000] + "…"
                parts.append(f"> {summary}")
            completion_msg = "\n".join(parts)

            if thread_send:
                # Post full result in the thread — no main-channel reply
                # to avoid notification spam (the thread-root "Agent working"
                # message already provides visibility; details live in-thread).
                await thread_send(completion_msg)
            else:
                try:
                    await self.bus.emit("notify.text", {
                        "event_type": "notify.text",
                        "message": completion_msg,
                        "project_id": hook.project_id,
                    })
                except Exception:
                    pass

        except Exception as e:
            logger.error("Hook %s failed: %s", hook.name, e, exc_info=True)
            await self.db.update_hook_run(
                run.id,
                status="failed",
                llm_response=str(e),
                completed_at=time.time(),
            )
            error_msg = f"🪝 Hook **{hook.name}** failed: {e}"
            if thread_send:
                try:
                    await thread_send(error_msg)
                except Exception:
                    pass
            try:
                await self.bus.emit("notify.text", {
                    "event_type": "notify.text",
                    "message": error_msg,
                    "project_id": hook.project_id,
                })
            except Exception:
                pass

    def _render_prompt(
        self,
        template: str,
        event_data: dict | None = None,
    ) -> str:
        """Render ``{{event.*}}`` placeholders in the prompt template.

        Supported placeholders:
        - ``{{event}}`` — the full event data dict as JSON
        - ``{{event.field}}`` — a single field from the event data dict

        Unrecognized placeholders are left unchanged.
        """

        def replacer(match):
            key = match.group(1)
            if key == "event":
                return json.dumps(event_data) if event_data else ""
            if key.startswith("event."):
                field = key[6:]
                if event_data:
                    return str(event_data.get(field, ""))
                return ""
            return match.group(0)

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)

    async def _build_hook_context(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> str:
        """Build a dynamic context preamble for the hook's LLM prompt.

        Fetches project metadata (name, workspace path, repo URL, default
        branch) and renders the ``hook-context`` prompt template with these
        values.  This gives the hook's LLM a clear understanding of:

        - **Which project** it's operating on (name, ID, workspace path)
        - **Why it fired** (periodic, event, manual, cron)
        - **Its role**: a dispatcher that should create tasks for code work
        - **What tools** it has (task management, git, notes, memory, etc.)

        For periodic hooks, timing context (current time, last run time) is
        included so the LLM can scope its work to changes since the last run.

        The preamble is prepended to the rendered hook prompt so it always
        appears at the top of the LLM's context, regardless of what the
        hook author wrote in the prompt template.
        """
        # Fetch project details for richer context
        project = await self.db.get_project(hook.project_id)
        project_name = project.name if project else hook.project_id

        workspace_dir = await self.db.get_project_workspace_path(hook.project_id)

        # Build optional context lines (only include if available)
        ws_line = f"Workspace: `{workspace_dir}`\n" if workspace_dir else ""
        repo_line = f"Repository: `{project.repo_url}`\n" if project and project.repo_url else ""
        branch_line = (
            f"Default branch: `{project.repo_default_branch}`\n"
            if project and project.repo_default_branch
            else ""
        )

        # Build timing context for periodic and scheduled hooks
        timing_line = ""
        if event_data and trigger_reason == "scheduled":
            parts = []
            if event_data.get("current_time"):
                parts.append(f"Current time: `{event_data['current_time']}`")
            if event_data.get("scheduled_for"):
                parts.append(f"Originally scheduled for: `{event_data['scheduled_for']}`")
            parts.append("This is a one-shot scheduled hook — it will auto-delete after this run.")
            timing_line = "\n".join(parts) + "\n"
        elif event_data and trigger_reason == "periodic":
            parts = []
            if event_data.get("current_time"):
                parts.append(f"Current time: `{event_data['current_time']}`")
            if event_data.get("last_run_time"):
                parts.append(f"Last run: `{event_data['last_run_time']}`")
                secs = event_data.get("seconds_since_last_run")
                if secs is not None:
                    parts.append(f"Elapsed since last run: {int(secs)}s")
            else:
                parts.append("Last run: *first run*")
            timing_line = "\n".join(parts) + "\n"

        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_identity(
            "hook-context",
            {
                "hook_name": hook.name,
                "project_id": hook.project_id or "",
                "project_name": project_name,
                "workspace_dir": ws_line,
                "repo_url": repo_line,
                "default_branch": branch_line,
                "trigger_reason": trigger_reason,
                "timing_context": timing_line,
            },
        )
        result, _ = builder.build()
        return result

    async def _invoke_llm(
        self,
        hook: Hook,
        prompt: str,
        trigger_reason: str = "unknown",
        on_progress=None,
        event_data: dict | None = None,
    ) -> tuple[str, int]:
        """Invoke the LLM through the Supervisor.

        Uses the Supervisor's process_hook_llm() method instead of creating
        a fresh ChatAgent instance. Preserves per-hook llm_config overrides
        by temporarily swapping the provider.
        """
        # Build dynamic context preamble
        context_preamble = await self._build_hook_context(
            hook,
            trigger_reason,
            event_data=event_data,
        )

        if self._supervisor:
            # Build per-hook provider override (if configured) — passed as a
            # parameter instead of swapping on the shared Supervisor, so
            # concurrent hooks don't race on self._supervisor._provider.
            hook_provider = None
            if hook.llm_config:
                llm_cfg = json.loads(hook.llm_config)
                raw_model = llm_cfg.get("model", self.config.chat_provider.model)
                provider_config = ChatProviderConfig(
                    provider=llm_cfg.get("provider", self.config.chat_provider.provider),
                    model=str(raw_model) if raw_model else "",
                    base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
                )
                hook_provider = create_chat_provider(provider_config)
                if hook_provider:
                    orchestrator = self._orchestrator
                    if hasattr(orchestrator, "llm_logger") and orchestrator.llm_logger._enabled:
                        hook_provider = LoggedChatProvider(
                            hook_provider, orchestrator.llm_logger, caller="hook_engine"
                        )

            response = await self._supervisor.process_hook_llm(
                hook_context=context_preamble,
                rendered_prompt=prompt,
                project_id=hook.project_id,
                hook_name=hook.name,
                on_progress=on_progress,
                provider=hook_provider,
            )

            tokens = len(context_preamble + prompt) // 4 + len(response) // 4
            return response, tokens

        # Fallback: create Supervisor directly (backward compat)
        from src.supervisor import Supervisor

        if hook.llm_config:
            llm_cfg = json.loads(hook.llm_config)
            raw_model = llm_cfg.get("model", self.config.chat_provider.model)
            provider_config = ChatProviderConfig(
                provider=llm_cfg.get("provider", self.config.chat_provider.provider),
                model=str(raw_model) if raw_model else "",
                base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
            )
        else:
            provider_config = self.config.chat_provider

        orchestrator = self._orchestrator
        supervisor = Supervisor(orchestrator, self.config)
        supervisor.set_active_project(hook.project_id)

        provider = create_chat_provider(provider_config)
        if provider and hasattr(orchestrator, "llm_logger") and orchestrator.llm_logger._enabled:
            provider = LoggedChatProvider(provider, orchestrator.llm_logger, caller="hook_engine")
        supervisor._provider = provider

        if not supervisor._provider:
            raise RuntimeError(f"Failed to create LLM provider: {provider_config.provider}")

        full_prompt = context_preamble + prompt
        response = await supervisor.chat(
            text=full_prompt,
            user_name="hook:" + hook.name,
            on_progress=on_progress,
        )
        tokens = len(full_prompt) // 4 + len(response) // 4
        return response, tokens

    async def fire_hook(self, hook_id: str) -> str:
        """Manually trigger a hook, ignoring cooldown.

        Used by the ``/fire-hook`` Discord command to allow operators to
        run a hook on-demand (e.g. for testing or urgent checks).  Unlike
        the normal periodic/event trigger path, this bypasses cooldown
        checks — but it still respects the "already running" guard (a
        hook cannot be fired if it's already in-flight).

        Returns the hook ID (used as a proxy run identifier).
        """
        hook = await self.db.get_hook(hook_id)
        if not hook:
            raise ValueError(f"Hook '{hook_id}' not found")
        if hook.id in self._running:
            raise ValueError(f"Hook '{hook_id}' is already running")

        now = time.time()
        self._last_run_time[hook.id] = now
        asyncio.create_task(self._persist_last_triggered(hook.id, now))
        task = asyncio.create_task(self._execute_hook(hook, "manual"))
        self._running[hook.id] = task
        return hook.id

    async def shutdown(self) -> None:
        """Cancel all running hook tasks and wait for them to finish.

        Uses ``asyncio.gather(..., return_exceptions=True)`` to ensure we
        don't propagate CancelledError — we just want everything stopped.
        """
        for hook_id, task in self._running.items():
            if not task.done():
                task.cancel()
        # Wait for all to finish
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    def set_orchestrator(self, orchestrator) -> None:
        """Store reference to the orchestrator (for LLM invocation).

        ``_invoke_llm`` creates a Supervisor which requires an orchestrator
        reference to register its tools (task management, status queries).

        This is a circular reference (orchestrator owns hooks, hooks reference
        orchestrator) that is broken at shutdown via ``hooks.shutdown()``.
        """
        self._orchestrator = orchestrator

    def set_supervisor(self, supervisor) -> None:
        """Set the Supervisor instance for LLM invocations."""
        self._supervisor = supervisor
