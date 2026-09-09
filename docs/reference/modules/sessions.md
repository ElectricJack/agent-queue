# Module catalog — sessions

Worker sessions, harnesses, terminals and claims: the 30 production modules
that turn a routed task into a coding-agent CLI running in a terminal, and keep
the daemon's picture of it honest.

Prose for everything here lives on four pages:

* [Sessions](../../concepts/sessions.md) — the concepts and the lifecycle.
* [Harness reference](../harnesses.md) — harness files, dialogs, transcripts.
* [Terminals and claims](../terminals-and-claims.md) — pane, terminal, claim
  file, environment.
* [Session troubleshooting](../../guides/session-troubleshooting.md) — recovery.

## Package entry points

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/sessions/__init__.py`](../../../src/sessions/__init__.py) | Exports the provider contract and owns `SessionProviderRegistry`, which caches one provider instance per name. | [concepts/sessions.md](../../concepts/sessions.md) | Caching is not an optimisation: the tmux provider owns per-session locks and the observation cache. `tests/test_session_provider_conformance.py` |
| [`src/sessions/provider.py`](../../../src/sessions/provider.py) | The `SessionProvider` ABC plus `SessionSpec`, `SessionHandle`, `Cap`, `DialogRule` and the typed failures every backend raises. | [concepts/sessions.md](../../concepts/sessions.md) | Callers gate on `Cap`, never on `provider.name`. `tests/test_session_provider_conformance.py`, `tests/test_session_executable.py` |
| [`src/sessions/spec.py`](../../../src/sessions/spec.py) | Composes profile + harness + task into one immutable launch description; the only place session names and argv are derived. | [reference/harnesses.md](../harnesses.md) | Providers stay free of harness knowledge because everything harness-specific happens here. `tests/test_session_spec.py`, `tests/test_session_tool_allowlist.py` |
| [`src/sessions/env.py`](../../../src/sessions/env.py) | Builds the child environment: the nine `AQ_*` identity markers, the database-isolation block, harness env and extras. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | `AQ_TASK_ID` is omitted, never empty, for a session with no task. `tests/test_session_runtime_units.py` |
| [`src/sessions/reconciler.py`](../../../src/sessions/reconciler.py) | The cascade step that owns session lifecycle: observe, drain-ack, exits, orphans, stall ladder, named convergence, backstop. | [concepts/sessions.md](../../concepts/sessions.md) | The module's governing rule is "unknown is not dead". `tests/test_session_reconciler.py`, `tests/test_pool_reconciler.py` |
| [`src/sessions/exit_classifier.py`](../../../src/sessions/exit_classifier.py) | Turns a dead process with an open task into one of four typed verdicts. | [concepts/sessions.md](../../concepts/sessions.md) | Rate-limit detection reads pane text — a hint, used only to choose between two safe outcomes. `tests/test_session_reconciler.py` |

## Providers

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/sessions/tmux.py`](../../../src/sessions/tmux.py) | The default provider on Linux and WSL2: one detached tmux session per agent, attachable, surviving daemon restarts. | [concepts/sessions.md](../../concepts/sessions.md) | POSIX-only by construction; importing it on Windows raises `ImportError` so registry construction skips it. `tests/test_tmux_integration.py`, `tests/test_tmux_startup_dialogs.py`, `tests/test_tmux_nudge_recovery.py` |
| [`src/sessions/subprocess.py`](../../../src/sessions/subprocess.py) | Degraded provider for hosts without tmux: a detached process group and a log file, with no pane, peek or nudge. | [reference/harnesses.md](../harnesses.md) | `list_running` sees only sessions this daemon process started, so nothing is re-adopted after a restart. `tests/test_session_provider_conformance.py` |
| [`src/sessions/fake.py`](../../../src/sessions/fake.py) | In-memory, scriptable provider used by every reconciler and cascade test, and a supported `sessions.provider` value. | [concepts/sessions.md](../../concepts/sessions.md) | Importable from production on purpose; scriptable death, partial listings and swallowed nudges. `tests/test_session_reconciler.py` |
| [`src/sessions/proctable.py`](../../../src/sessions/proctable.py) | Scans `/proc` for processes carrying an `AQ_*` marker, and kills process trees fenced on instance token and start time. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | Blocking `/proc` reads are wrapped in `asyncio.to_thread`; never call the `_sync` internals from the event loop. `tests/test_session_runtime_units.py` |
| [`src/sessions/state_cache.py`](../../../src/sessions/state_cache.py) | Answers every liveness question of one reconciler tick from one `list-panes -a` plus one `ps`, TTL-cached. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | A failed `ps` returns `None` (optimistic alive); an unreachable tmux server raises with the last-known-good snapshot. `tests/test_session_runtime_units.py` |
| [`src/sessions/dialogs.py`](../../../src/sessions/dialogs.py) | Runs a harness's declared startup-dialog rules against a live pane, under one shared budget with a quiet window. | [reference/harnesses.md](../harnesses.md) | Provider-agnostic: it sees two callables, so it is unit-testable without tmux. `tests/test_tmux_startup_dialogs.py` |

## Harnesses

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/sessions/harness_parser.py`](../../../src/sessions/harness_parser.py) | Parses one harness markdown file into an immutable `Harness`, and folds a single-level `base` into it. | [reference/harnesses.md](../harnesses.md) | Unknown config keys warn rather than fail, so a file written for a newer daemon still loads. `tests/test_harness_parser.py` |
| [`src/sessions/harness_registry.py`](../../../src/sessions/harness_registry.py) | In-memory registry projected from the vault, project scope shadowing system scope, kept current by the vault watcher. | [reference/harnesses.md](../harnesses.md) | No database table — the file is the source of truth; a parse failure keeps the previous entry. `tests/test_harness_parser.py` |
| [`src/sessions/harness_manifest.py`](../../../src/sessions/harness_manifest.py) | Records the hash of every shipped harness version so an unedited vault copy can be refreshed in place and an edited one left alone. | [reference/harnesses.md](../harnesses.md) | A ratchet: the test fails if a shipped file's current hash is not recorded. `tests/test_harness_manifest.py`, `tests/test_cli_vault_reset_harness.py` |
| [`src/sessions/default_harnesses/claude.md`](../../../src/sessions/default_harnesses/claude.md) | Shipped harness definition for the `claude` CLI, plus the measured reasoning behind every field. | [reference/harnesses.md](../harnesses.md) | Shipped content, seeded into `vault/harnesses/`; the vault copy wins once it exists. |
| [`src/sessions/default_harnesses/codex.md`](../../../src/sessions/default_harnesses/codex.md) | Shipped harness definition for the `codex` CLI, including the hook-trust flag and its deliberate `resume: none`. | [reference/harnesses.md](../harnesses.md) | Shipped content; records the CLI versions each claim was verified against. |
| [`src/sessions/default_harnesses/gemini.md`](../../../src/sessions/default_harnesses/gemini.md) | Shipped harness definition for the `gemini` CLI, with no hooks and no transcript reader. | [reference/harnesses.md](../harnesses.md) | Shipped content; declares no `transcript_paths` rather than implying a reader that does not exist. |

## Transcripts

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/sessions/transcripts/__init__.py`](../../../src/sessions/transcripts/__init__.py) | Resolves the transcript reader for a harness, or `None` when that harness has none. | [reference/harnesses.md](../harnesses.md) | Readers live outside providers: the same file is readable however the session was hosted. `tests/test_transcript_readers.py` |
| [`src/sessions/transcripts/base.py`](../../../src/sessions/transcripts/base.py) | The reader ABC and the normalized `TranscriptEntry` shape shared across harnesses. | [reference/harnesses.md](../harnesses.md) | `base_dir` is injectable so tests never collide with a real `~/.claude`. `tests/test_transcript_readers.py` |
| [`src/sessions/transcripts/claude.py`](../../../src/sessions/transcripts/claude.py) | Reads the Claude CLI's per-session JSONL files, resolved by a slug derived from the working directory. | [reference/harnesses.md](../harnesses.md) | Byte-offset incremental; a partial trailing line is returned unconsumed and re-read whole. `tests/test_transcript_readers.py` |
| [`src/sessions/transcripts/codex.py`](../../../src/sessions/transcripts/codex.py) | Reads the Codex CLI's date-partitioned rollout files and discovers the session UUID the CLI chose. | [reference/harnesses.md](../harnesses.md) | Takes text from one channel and tool calls from the other; mixing them double-counts every turn. `tests/test_transcript_codex.py` |
| [`src/sessions/transcripts/watcher.py`](../../../src/sessions/transcripts/watcher.py) | Polls every live session's transcript, emitting turn events, one token-usage row per assistant entry, and lease refreshes. | [reference/harnesses.md](../harnesses.md) | Read offsets are mirrored durably by transcript path, so a relaunch on the same workspace cannot replay the file. `tests/test_transcript_watcher.py`, `tests/test_transcript_checkpoints.py` |

## Terminals and panes

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/sessions/pane_broadcaster.py`](../../../src/sessions/pane_broadcaster.py) | Owns one pane poll loop per watched session and fans its screens out to every subscriber. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | Cost is `O(watched sessions)`, never `O(sessions × viewers)`; nothing polls unwatched. `tests/test_pane_broadcaster.py`, `tests/test_pane_stream_api.py` |
| [`src/sessions/terminal_pty.py`](../../../src/sessions/terminal_pty.py) | A disposable tmux attach client for the interactive terminal; closing it never stops the agent's pane. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | POSIX terminal modules are imported lazily so API modules stay importable on Windows. `tests/test_terminal_pty.py`, `tests/test_terminal_stream.py` |
| [`src/sessions/questions.py`](../../../src/sessions/questions.py) | Routes a question an agent asked at the end of a turn to the project supervisor, and delivers the answer back under a durable lease. | [concepts/sessions.md](../../concepts/sessions.md) | Transcript content is untrusted; no question changes a claim or resets the recovery ladder. `tests/test_agent_questions.py`, `tests/test_agent_question_end_to_end.py` |
| [`src/panes/__init__.py`](../../../src/panes/__init__.py) | Package marker for the server-side pane-view registry. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | Intentionally empty. |
| [`src/panes/registry.py`](../../../src/panes/registry.py) | Server-side mirror of the dashboard's pane-view registry: which view ids exist and which an agent may push. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | Kept in sync with `dashboard/src/panes/<view>/manifest.ts` by hand. `tests/test_pane_registry_parity.py` |

## Claims and environment

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/claim_file.py`](../../../src/claim_file.py) | Reads, writes and conditionally removes `<work_dir>/.aq/claim.json`, the worker's proof of what it holds. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | A stdlib-only leaf on purpose: it breaks the cycle between `src.commands` and `src.sessions`. `tests/test_claim_commands.py`, `tests/test_import_cycles.py` |
| [`src/env_scrub.py`](../../../src/env_scrub.py) | The single pure function that decides which of the daemon's environment variables an agent subprocess may inherit. | [reference/terminals-and-claims.md](../terminals-and-claims.md) | Best-effort denylist plus explicit allowlist; `security.env_scrub_enabled` is a kill switch, not a tuning knob. `tests/test_env_scrub.py`, `tests/test_session_doctor.py` |

## Running the focused tests

```bash
aq test tests/test_session_reconciler.py tests/test_session_spec.py tests/test_session_provider_conformance.py
aq test tests/test_harness_parser.py tests/test_harness_manifest.py tests/test_transcript_readers.py
aq test tests/test_terminal_stream.py tests/test_pane_broadcaster.py tests/test_env_scrub.py
```

## Modules this shard deliberately does not own

Several things a reader might expect here belong to a neighbouring shard,
because they are surfaces *onto* sessions rather than the session runtime:

| Path | Owning shard | Where it is documented |
|---|---|---|
| `src/api/terminal_stream.py`, `src/api/pane_stream.py`, `src/api/sessions.py` | `api` | the REST/WebSocket reference; behaviour is summarised in [terminals and claims](../terminals-and-claims.md) |
| `dashboard/src/components/InteractiveTerminal.tsx`, `dashboard/src/ws/terminalSocket.ts`, `dashboard/src/ws/usePaneStream.ts`, `dashboard/src/api/useTerminalInput.ts` | `dashboard` | the dashboard guide |
| `src/agents/terminals.py` | `routing` | agents and routing |
| `src/commands/session_commands.py`, `src/commands/claim_commands.py` | `cli` | the CLI reference |
| `src/doctor/session_checks.py` | `operations` | the operations guide; the checks are used in [troubleshooting](../../guides/session-troubleshooting.md) |
| `src/orchestrator/task_checkpoint.py` | `scheduler` | scheduling; pause behaviour is described in [sessions](../../concepts/sessions.md) |
