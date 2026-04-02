# Specification: src/main.py

## 1. Overview

`src/main.py` is the entry point for the Agent Queue daemon. Its responsibilities are:

- Parse the single optional CLI argument (config file path).
- Load configuration and initialize all top-level components.
- Run the Discord bot and the orchestrator scheduler loop concurrently inside a single asyncio event loop.
- Install OS signal handlers so that SIGTERM or SIGINT triggers a clean shutdown.
- After the async run completes, check whether a restart was requested and, if so, replace the current process with a fresh copy of itself using `os.execv`.

The module exports two callable symbols: `run` (the async coroutine) and `main` (the synchronous entry point).

---

## Source Files

- `src/main.py`

---

## 2. CLI Interface

The program accepts at most one positional argument:

```
python -m src.main [config_path]
```

| Argument | Type | Default | Description |
|---|---|---|---|
| `config_path` | string (optional) | `~/.agent-queue/config.yaml` | Path to the YAML configuration file. The default is produced by `os.path.expanduser`. |

The default path is stored in the module-level constant `DEFAULT_CONFIG_PATH`.

No flags, no subcommands, no `argparse` — the path is read directly from `sys.argv[1]` when present.

---

## 3. Startup Sequence

The `run(config_path: str) -> bool` coroutine performs startup in the following strict order:

1. **Load configuration** — Call `load_config(config_path)`. This returns a fully-populated config dataclass with all `${ENV_VAR}` substitutions resolved.

2. **Ensure database directory exists** — Call `os.makedirs(os.path.dirname(config.database_path), exist_ok=True)`. This guarantees the SQLite file can be created without a missing-directory error.

3. **Create adapter factory** — Instantiate `AdapterFactory()`. This is the registry/factory used by the orchestrator to construct agent adapters (e.g. Claude Code).

4. **Create and initialize orchestrator** — Instantiate `Orchestrator(config, adapter_factory=adapter_factory)`, then `await orch.initialize()`. The `initialize()` call opens the database connection, runs migrations, and prepares internal state.

5. **Create Discord bot** — Instantiate `AgentQueueBot(config, orch)`. The bot holds a reference to the orchestrator so that Discord commands can invoke orchestrator operations directly.

6. **Create shutdown event** — Create an `asyncio.Event()` named `shutdown_event`. This event is the single mechanism by which signal handlers, the main coroutine, and the scheduler loop coordinate a clean stop.

7. **Register OS signal handlers** — Register a synchronous callback `handle_signal` on both `SIGTERM` and `SIGINT` via `loop.add_signal_handler`. The callback does one thing: call `shutdown_event.set()`.

8. **Create concurrent tasks** — Create two `asyncio.Task` objects (`bot_task` and `scheduler_task`) as described in Section 4.

9. **Await shutdown** — Block with `await shutdown_event.wait()`. The coroutine sits here until a signal fires or the bot internally sets the event.

---

## 4. Concurrent Tasks

Two tasks run concurrently from the moment `run()` reaches step 8:

### 4.1 `bot_task` — Discord Bot

Wraps the coroutine `run_bot()`.

Behaviour:
- Calls `await bot.start(config.discord.bot_token)`.
- The bot connects to Discord's gateway and begins receiving events.
- If the task is cancelled (as part of shutdown), `asyncio.CancelledError` is caught and silently suppressed, allowing clean teardown.

### 4.2 `scheduler_task` — Orchestrator Loop

Wraps the coroutine `run_scheduler()`.

Behaviour:
1. **Wait for bot readiness** — Calls `await bot.wait_until_ready()` before doing any work. This ensures the bot is connected to Discord before the orchestrator starts sending notifications.
2. **Polling loop** — Repeats until `shutdown_event` is set:
   a. Call `await orch.run_one_cycle()` — executes one full orchestration tick (promotes tasks, assigns work, checks heartbeats, etc.).
   b. Attempt `await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)`. If the event fires before the timeout, the loop exits on the next iteration check. If the timeout expires (`asyncio.TimeoutError` is caught and discarded), the loop immediately executes the next cycle.

The effective cycle interval is approximately 5 seconds when idle. All I/O operations within `run_one_cycle()` are async (git operations use `asyncio.create_subprocess_exec()`, database uses `aiosqlite`, LLM calls use async HTTP), so the event loop is never blocked and the Discord bot remains responsive even during long orchestrator cycles.

---

## 5. Signal Handling

Both `SIGTERM` (process termination) and `SIGINT` (keyboard interrupt / Ctrl-C) are handled identically.

The handler is registered using the asyncio event loop's `add_signal_handler` method, which means the callback runs safely inside the event loop on the next iteration rather than as a raw Unix signal handler.

When either signal is received:
1. `shutdown_event.set()` is called.
2. `await shutdown_event.wait()` in the main coroutine unblocks.
3. The `finally` block in `run()` begins the shutdown sequence (see Section 7).

No other signals are handled. There is no SIGHUP reload or SIGUSR1/2 handling.

---

## 6. Restart Mechanism

The restart mechanism allows the daemon to replace itself with a fresh process without manual intervention. It is triggered by the Discord `/restart` command (or the equivalent chat command).

### How it works

1. **Flag is set on the bot object** — When a restart is requested, `AgentQueueBot` sets its `_restart_requested` attribute to `True` and then calls `shutdown_event.set()` to initiate normal shutdown.

2. **Flag is read during teardown** — In the `finally` block of `run()`, the value of `bot._restart_requested` is captured into a local variable `restart` *before* `bot.close()` is called.

3. **`run()` returns the flag** — The coroutine returns `restart` (a `bool`). `True` means restart was requested; `False` means normal shutdown.

4. **`main()` performs the exec** — After `asyncio.run(run(config_path))` returns, `main()` checks the return value:
   - If `False`: the function returns normally, and the process exits.
   - If `True`:
     a. A message is printed: `"Restart requested — exec'ing new process..."`.
     b. The executable path is resolved to an absolute path using `shutil.which(sys.argv[0])`, falling back to `os.path.abspath(sys.argv[0])` if `which` returns `None`.
     c. `os.execv(exe, sys.argv)` is called. This replaces the current process image with a new invocation of the same executable and the same arguments. The PID is preserved. No new process is forked.

### Why `execv` instead of subprocess

Using `os.execv` means the process ID stays the same, any process supervisor (systemd, Docker, etc.) continues tracking the same PID, and there is no transient period with two running instances.

---

## 7. Shutdown Sequence

The `finally` block in `run()` executes regardless of how shutdown was triggered (signal, restart request, or unhandled exception). The steps are:

1. **Capture restart flag** — Read `bot._restart_requested` before closing the bot (the attribute may be reset or become unavailable after close).

2. **Close the Discord bot** — `await bot.close()`. This disconnects from Discord's gateway and stops the bot's internal event dispatch.

3. **Cancel `bot_task`** — `bot_task.cancel()`. Cancels the asyncio task that was running `run_bot()`. The task's `CancelledError` handler suppresses the exception.

4. **Cancel `scheduler_task`** — `scheduler_task.cancel()`. Cancels the polling loop task.

5. **Shut down the orchestrator** — `await orch.shutdown()`. This closes the database connection and performs any other orchestrator-level cleanup (e.g. stopping in-progress agent subprocesses).

Note: the cancelled tasks are not explicitly awaited after cancellation. Cleanup relies on the tasks' own `CancelledError` handling and on `orch.shutdown()` for database/agent cleanup.

After the `finally` block completes, `run()` returns the `restart` boolean to `main()`.
