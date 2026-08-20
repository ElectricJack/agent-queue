# Performance Assessment: Is a Partial Rust Rewrite the Right Lever?

**Date:** 2026-08-20 · **Branch:** `main` @ `a9ce6905` · **Scope:** read-only analysis + synthetic benchmarks
**Machine:** Windows 11, Python 3.12.4, SQLAlchemy 2.0.49, aiosqlite 0.22.1, SQLite WAL

---

## Verdict

**Performance is not the constraint, and a Rust rewrite would not address any of the three real bottlenecks.**

The daemon has a genuine, measurable, *severe* performance problem — it overruns its own 5-second cycle by 3.5x at 6,000 open tasks. But the cause is an N+1 query loop that a single already-implemented SQL predicate replaces, not the language. **The Python fix is 4x faster than a hypothetical Rust rewrite of the same bad algorithm.** That comparison is the whole answer, and it's measured below, not asserted.

The three real bottlenecks, in order:

1. **`_legacy_promotion_decisions` — an N+1 scan running every 5s, in shadow mode, by default.** 2.9 ms/task × every DEFINED+BLOCKED task. **16.9 s per cycle at 10k tasks; 172 s at 100k.** A single-query replacement already exists in the same file and is 27,000x faster. This is the first thing that breaks and it breaks at ~2,000 open tasks.
2. **`Orchestrator._schedule()` loads the entire `tasks` table every 5 seconds** (`list_tasks()` with no filter). 1.62 s and ~1.7 s of frozen event loop at 100k tasks, to compute an answer that needs only the READY rows.
3. **Blocking synchronous I/O on the asyncio event loop** — most importantly a full `os.walk` + `os.stat` of the vault every 5 seconds, un-offloaded. 590 ms at 10k vault files; 2.8 s at 50k (56% of the cycle spent frozen).

None of these is a Python-vs-Rust question. All three are `this code`-vs-`better code`. The distinction matters enormously, because the remedies differ by roughly three orders of magnitude in cost.

---

## 1. Measurements

Synthetic databases built against the **real schema** (Alembic `upgrade head`), populated with a ~50% `blocks`-edge chain density, then queried through the **real query layer** (`SQLiteDatabaseAdapter`, all mixins). Scripts: `mkdb.py`, `bench.py`, `bench2.py`, `bench3.py`, `bench4.py`, `bench5.py`.

| Fixture | tasks | projects | agents | dep edges | size |
|---|---|---|---|---|---|
| `bench_1k` | 1,000 | 5 | 10 | 477 | 0.7 MB |
| `bench_10k` | 10,000 | 20 | 20 | 4,978 | 3.6 MB |
| `bench_100k` | 100,000 | 50 | 50 | 49,913 | 32.7 MB |
| `bench_500p` | 20,000 | **500** | 200 | 9,679 | 7.0 MB |

### 1.1 Per-cycle work (median of 3+ runs)

| Operation | 1k | 10k | 100k | 500-proj | SQL stmts |
|---|---:|---:|---:|---:|---:|
| `list_projects()` | 1.1 ms | 1.3 ms | 1.3 ms | 3.3 ms | 1 |
| **`list_tasks()`** — full table, every 5 s | **13.6 ms** | **132 ms** | **1,759 ms** | **287 ms** | 1 |
| `list_agents()` | 1.2 ms | 1.0 ms | 1.3 ms | 1.8 ms | 1 |
| `list_workspaces()` | 1.2 ms | 1.1 ms | 1.7 ms | 6.0 ms | 1 |
| token-usage loop (N+1 per project) | 5.2 ms | 19.6 ms | 56.5 ms | **574 ms** | P |
| `count_available_workspaces` loop (N+1) | 5.7 ms | 19.7 ms | 59.1 ms | **584 ms** | P |
| `Scheduler.schedule()` — **pure Python CPU** | 0.4 ms | 4.2 ms | 79 ms | 24 ms | 0 |
| `list_tasks(status=DEFINED)` | 8.3 ms | 76.6 ms | 1,151 ms | 198 ms | 1 |
| `_projected_promotion_decisions()` | 0.04 ms | 0.6 ms | **18.6 ms** | 3.4 ms | 0–1 |
| **`_legacy_promotion_decisions()`** (extrapolated) | **1.57 s** | **16.9 s** | **172 s** | **34.9 s** | 2.5 × d |

The last two rows are the same decision computed two ways. `blocked_state_authoritative` defaults to **`False`** (`src/config.py:1158`), which means shadow mode: *both* run every cycle and **the slow one decides**.

### 1.2 Blocked-state projection — the well-engineered part

| Operation | 1k | 10k | 100k | 500-proj |
|---|---:|---:|---:|---:|
| `recompute_all_blocked()` (full UPDATE + 2 SELECT) | 18.7 ms | 108 ms | 1,020 ms | 217 ms |
| `evaluate_blocked()` (brute-force 5-way EXISTS) | 8.7 ms | 27.6 ms | 330 ms | 56 ms |
| `get_blocked_map()` | 2.0 ms | 10.9 ms | 243 ms | 33 ms |
| `recompute_blocked(10 seeds)` — incremental | 10.9 ms | 7.3 ms | **8.3 ms** | 8.3 ms |

**This subsystem is fine and should be left alone.** The incremental recompute is *flat* — 8.3 ms at 100k tasks, same as at 1k — because `_collect_affected` (`src/database/queries/blocked_state.py:309`) bounds the working set to seeds plus direct dependents. The five correlated `EXISTS` clauses, including the doubly-nested `_waits_for_unsat`, evaluate over 100k rows and 50k edges in 330 ms. SQLite is doing this work in C; there is nothing here for Rust to win.

Your two prior data points check out and are, if anything, understated. The `log_blocked_flips` docstring (`blocked_state.py:365-370`) records the batching win as **13.9 s → 0.06 s** for 1,000 flips on a 10k chain. That is a 230x improvement obtained by replacing per-row transactions with one `executemany` — **in Python, with no language change.** It is the single most instructive precedent in this codebase for the question being asked.

### 1.3 Where the per-query time actually goes

This is the number that decides the Rust argument, so it is decomposed carefully. Same query, 500 iterations, `bench_10k`:

| Path | µs/query | vs sync |
|---|---:|---:|
| sync `sqlite3`, one connection, no txn | **5.1** | 1x |
| bare `aiosqlite`, one connection | 156 | 31x |
| async + SQLAlchemy Core, one shared txn, raw SQL | 323 | 63x |
| async + SQLAlchemy Core, one shared txn | 390 | 76x |
| **async + `engine.begin()` per query** — *what the query layer does* | **885** | **173x** |

Attribution:

| Component | µs | Note |
|---|---:|---|
| actual SQLite execution | 5 | 0.6% |
| **asyncio thread hand-off** | **~103** | measured directly: a bare `await asyncio.to_thread(noop)` costs 102.6 µs on this machine |
| aiosqlite queue/cursor overhead | ~50 | |
| SQLAlchemy Core compile + execute | ~167 | |
| **`BEGIN`/`COMMIT` per query** | **~495** | the query layer opens a transaction for *every single read* |

Two honest conclusions:

- **99.4% of a DB round-trip is framework overhead, not SQL.** That is the strongest fact available to the Rust case, and it is real.
- **But the largest single component (495 µs, 56%) is a per-query transaction — a code choice, not a language property.** Sharing one connection across a cycle's reads recovers it in Python today. And the thread-hop floor (~103 µs) is what you pay for `aiosqlite` specifically; it is not what you pay for "Python."

Event-loop policy makes no difference: Proactor 156 µs vs Selector 171 µs. `uvloop` is unavailable on Windows and would not help here anyway — the bottleneck is a thread hand-off, not the selector.

### 1.4 Concurrency: `StaticPool` means there is none

`create_sqlite_engine` (`src/database/engine.py:52`) uses `poolclass=StaticPool` — **one connection for the entire daemon**, deliberately ("matching the previous aiosqlite single-connection behavior"). Measured:

| Workload (bench_10k) | serial | `asyncio.gather` | speedup |
|---|---:|---:|---:|
| 200 reads | 170.9 ms | 117.3 ms | **1.46x** |
| 100 writes | 178 ms | 51 ms | 3.50x |

Concurrent reads barely overlap. WAL mode's multi-reader capability is unreachable behind a single connection. This is a real ceiling — and note that it is a ceiling Rust does not lift either, because it is a pooling configuration, not a language limit. `create_postgres_engine` right above it correctly uses a 10-connection pool.

### 1.5 Event-loop blocking, measured

A 5 ms heartbeat coroutine measuring its own wake-up lag:

| Condition | max lag |
|---|---:|
| idle baseline | 1.51 ms |
| during `list_tasks()` @ 10k tasks | **89.4 ms** |

At 100k tasks that scales to roughly **1.7 s of frozen loop every 5 seconds** — a 35% duty cycle in which no Discord heartbeat, no API request, and no subprocess reap can be serviced. Of `list_tasks()`'s 1,759 ms at 100k, **946 ms is `_row_to_task()`** (`task_queries.py:485`) — pure Python object construction with a `json.loads` and 4 enum constructions per row. That *is* a genuinely CPU-bound Python hot path. It is also 100% avoidable work.

### 1.6 Vault watcher — `os.walk` + `os.stat`, on the loop, every 5 s

`VaultWatcher._scan_tree` (`src/vault_watcher.py:303`), called synchronously from `async def check()` at `:280`, called from `run_one_cycle` at `core.py:2017`:

| vault files | scan time | µs/file | % of a 5 s cycle spent frozen |
|---:|---:|---:|---:|
| 100 | 5.5 ms | 54.9 | 0.1% |
| 1,000 | 55.5 ms | 55.5 | 1.1% |
| 10,000 | 591 ms | 59.1 | **11.8%** |
| 50,000 | 2,821 ms | 56.4 | **56.4%** |

### 1.7 Everything else measured — all fine

| Thing | Measurement | Verdict |
|---|---|---|
| `EventBus.emit` | 51.5 µs/event × 50 subscribers = **1.03 µs/delivery** | Not a bottleneck |
| `json.dumps`, 1000-task response | 1.92 ms (0.66 MB) | Not a bottleneck |
| `ToolRegistry()` construction | **0.009 ms** | Not a bottleneck; registry is a module-level literal built once at import |
| `search_relevant_categories()` | 2.0 ms | Acceptable; no index, but rarely called |
| `import src.tools.definitions` | 33.5 ms | One-time |
| cold `import src.main` | 1,260 ms | One-time, at startup |
| subprocess spawn | 62 ms serial; **10.4 ms amortised** across 20 concurrent | Not a bottleneck |
| `/api/execute` triple-JSON pass | 3.44 ms vs 1.19 ms ideal (2.9x) | 2.25 ms wasted; trivial fix, low impact |
| SHA-256 of a 20 KB file (warm) | 107 µs, of which **9.3 µs (9%) is CPU** | **I/O-bound, not CPU-bound** |

That last row corrects a tempting misreading. `WorkspaceSpecWatcher._build_file_snapshot` (`src/workspace_spec_watcher.py:679`) unconditionally SHA-256s every matched file every scan, before any mtime comparison — 11.2 s for 2,000 × 20 KB files cold. It looks like a CPU problem. It is not: **91% of that is `open()` + `read()`.** Rust would recover the 9%. Checking mtime first recovers ~97%.

---

## 2. What Breaks First

**Answer: the `_legacy_promotion_decisions` N+1 loop, at roughly 2,000 open (DEFINED + BLOCKED) tasks.**

Not SQLite write contention. Not file descriptors. Not the GIL. A quadratic-in-practice query loop that a single indexed SQL statement already replaces, thirty lines further down the same file.

Per-cycle round-trip budget, using the **measured** 1.16 ms end-to-end cost of `db.get_task()`:

| Scale | round-trips/cycle | time | fits in 5 s? |
|---|---:|---:|---|
| 5 projects, 2 busy, 50 open tasks | 151 | 0.18 s | yes |
| 20 projects, 10 busy, 500 open | 1,330 | 1.54 s | yes, 31% of budget |
| 50 projects, 50 busy, **6,000 open** | 15,260 | **17.7 s** | **no — 3.5x overrun** |
| **500 projects**, 200 busy, 12,000 open | 31,610 | **36.7 s** | **no — 7.3x overrun** |

Formula: `~10 fixed + 2P + 3B + 2.5D` where P = projects, B = busy agents, D = open tasks. The `2.5D` term dominates everything else and comes entirely from the legacy scan.

### The ordered failure sequence

1. **~2,000 open tasks** — `_legacy_promotion_decisions` pushes cycle time past 5 s. Cycles begin to overlap or serialize; dependency-chain progression stalls; the "promotion cascade" ordering invariant that the docstring carefully protects (`core.py:1920-1923`) is defeated by the cycle simply not finishing.
2. **~10,000 vault files** — the un-offloaded `os.walk` adds 0.6 s of frozen loop per cycle. Discord's 41.25 s heartbeat window still survives; API latency becomes visibly spiky.
3. **~50,000 tasks** — `list_tasks()` alone freezes the loop for ~0.9 s/cycle. This is the point where the single asyncio loop blocking on synchronous work becomes user-visible.
4. **~500 projects** — the two N+1 per-project loops in `_schedule()` (`core.py:2228`, `:2256`) contribute 1.16 s/cycle on their own.
5. **Only then** would SQLite write contention matter — and `StaticPool` means writes are already fully serialized through one connection, so contention manifests as latency, not as `SQLITE_BUSY`. Note no `busy_timeout` pragma is set (`engine.py:57-60`), which would matter if the pool were ever widened.

**Your prior was right about the shape and wrong about which item is first.** You predicted SQLite write contention, fd limits, or the loop blocking on something synchronous. Items 2 and 3 are exactly "the loop blocking on something synchronous" — you called that correctly. But the actual first failure is upstream of all three: a request-amplification bug that would break just as decisively against PostgreSQL, and would break against a Rust daemon too, only 20x later.

Subprocess/fd limits never became the binding constraint at any scale tested. 20 concurrent spawns amortise to 10.4 ms each; at 50 concurrent agents you are nowhere near a Windows or Linux fd ceiling.

---

## 3. Blocking-Call Audit

Verified against `CLAUDE.md:38` — *"never sync `subprocess.run()` in production."*

**The git layer honors it completely.** `asyncio.create_subprocess_exec` appears 21 times across 9 files; every `self.git.*` call site in `orchestrator/` uses the `a`-prefixed async variant. The sync half of `src/git/manager.py` is retained for CLI/tests and is not reachable from any `async def` in `src/`.

### Class A — on the event loop, hot/recurring path

| Location | Call | Frequency | Cost |
|---|---|---|---|
| **`src/vault_watcher.py:340`** | `self._scan_tree()` from sync `_detect_changes`, called by `async check()` at `:280` | **every 5 s cycle** | `os.walk` + `os.stat`/file. **591 ms @ 10k files, 2.8 s @ 50k** |
| `src/workspace_spec_watcher.py:747` | `open(stub_path, "w")` in `_write_stub`, from `async _process_changes` (`:702`), no `to_thread` | per changed file | undoes half the fix at `:527` |
| `src/workspace_spec_watcher.py:679` | unconditional `compute_content_hash` per file per scan | every 60 s/project | 107 µs/file warm, 91% of it I/O |
| `src/chat_providers/logged.py:91` | `log_chat_provider_call` → `llm_logger._append` (`:357`): `makedirs` + `open` + `write` | **every LLM call** | writes full prompts/transcripts |
| `src/runtimes/claude_sdk.py:893` | `log_agent_session` → same `_append`, writes **two** files | per agent session | |
| `src/sessions/subprocess.py:209` | `Path(running.log_path).read_text()` in `async def peek` | per live session per tick | reads the **entire** log to return the last 60 lines; file grows unbounded |
| `src/sessions/subprocess.py:195` | `os.path.getmtime()` in `async def last_activity` | per live session per tick | small, but batch it with the above |
| `src/orchestrator/core.py:2047,2051` | `cleanup_old_logs()` (does `shutil.rmtree`) + `flush_analytics()` | hourly, on the loop | |

The vault-watcher finding is the flagship. Its sibling `src/workspace_spec_watcher.py:527` wraps the equivalent scan in `await asyncio.to_thread(...)` with a comment naming the exact hazard — *the scan can take 10+ seconds per cycle, which otherwise blocks the asyncio loop and stalls the Discord heartbeat.* The author diagnosed this precisely and fixed one of the two watchers. Likewise `src/doctor/builtin.py:487` calls `await asyncio.to_thread(llm_logger.cleanup_old_logs)` — the diagnostic path offloads the very function the daemon hot loop calls synchronously. **These are copy-the-adjacent-line fixes.**

Vault-watcher handler fan-out compounds it: `_dispatch` (`:434-451`) awaits handlers serially, and each does a blocking `read_text` — `playbooks/handler.py:191`, `profiles/sync.py:488`, `readme_handler.py:405`, `profiles/mcp_registry.py:637`, `sessions/harness_registry.py:232`, `facts_handler.py:189,194`. A bulk vault edit (a `git pull` in the vault) becomes an unbounded serial read burst on the loop.

### Class B — the CLAUDE.md violation

`src/plugins/loader.py` contains the **only** `subprocess.run` calls lexically inside `async def` in all of `src/`:

| Location | Call |
|---|---|
| `src/plugins/loader.py:75` | `subprocess.run(cmd, ..., timeout=120)` in `async def clone_plugin_repo` — **up to 120 s of frozen loop** |
| `src/plugins/loader.py:81, 92` | `subprocess.run(...)` — same function |
| `src/plugins/loader.py:123, 134, 144` | `subprocess.run(...)` in `async def pull_plugin_repo` |
| `src/plugins/loader.py:68` | `shutil.rmtree(src_dir)` in `async clone_plugin_repo` |
| `src/plugins/loader.py:280` | `pip install` via `subprocess.run`, reached from `registry.py:795, 857` |

Plugin install/update only — but a 120 s timeout means a hung `git clone` freezes the entire daemon for two minutes. Swap for `asyncio.create_subprocess_exec` (the pattern is already used 21 times elsewhere).

### Class C — clean

- **No sync SQLAlchemy anywhere.** Zero hits for `create_engine(` / `sessionmaker(` / `scoped_session` in `src/`.
- **No `time.sleep` in any async code.** All 11 hits are in `cli/`, `setup_wizard.py`, and `main.py:436` (sync restart backoff).
- **No sync HTTP in async** except `chat_providers/ollama.py:69`, already wrapped in `to_thread` at `:80`.
- **No CPU-heavy regex/hashing/compression on the loop.** All `re.search` hits operate on short strings.
- `asyncio.to_thread` used correctly in 8 places.

### Class D — notable

- **`src/file_watcher.py` appears to be dead code** carrying a latent copy of the vault-watcher bug (`_scan_folder:309-345` from `async _check_folder_watch:226`). No instantiation or call site exists outside the module.
- `src/commands/**` and `src/plugins/internal/{files,notes}.py` do blocking `open`/`os.walk`/`os.listdir` inside `async def`. If command handlers are dispatched **in-process by the daemon** — which `CLAUDE.md` states they are, as the single entry point for Discord + MCP + CLI — these are Class A, not Class D. `files.py:1614` does `os.walk(workspace_path)` over a whole repo inside `async _list_tracked_files`.
- `src/event_bus.py:151` calls `inspect.iscoroutinefunction(handler)` **per handler per emit** — reflection on the hot path, cacheable at subscribe time. At 1.03 µs/delivery it doesn't matter yet, but it's free to fix.
- `EventBus.emit` dispatches serially with **no error isolation** (`:148-154`): one slow async handler blocks all subsequent handlers, and one exception skips the rest. That's a correctness/robustness issue, not a performance one, but it's in the same lines.

---

## 4. The Honest Steelman for Rust

I looked for places where Rust genuinely wins. Here they are, assessed against agent-queue's plausible scale.

### 4.1 The strongest case: DB round-trip overhead — and why it still fails

This is the real argument, and it deserves to be stated at full strength.

A Rust daemon using `rusqlite` on the current thread pays ~5 µs per query. Python pays 885 µs. **That is a 173x advantage, and it is not a microbenchmark artifact — it is what the query layer actually costs.** Apply it to the failure mode:

| Scale | Python, as written | **Rust, same bad algorithm** | Python, projection path |
|---|---:|---:|---:|
| 6,000 open tasks | 17.7 s | **0.076 s** | **~0.02 s** |
| 60,000 open tasks | 172 s | **0.75 s** | **0.019 s** |

Rust survives the N+1. That is a true statement and the best card in the deck.

**But the last column is the refutation.** `_projected_promotion_decisions` — already written, already in `src/orchestrator/monitoring.py:183`, already running every cycle in shadow mode — computes the identical answer in **18.6 ms at 100k tasks**. It is *4x faster than the Rust rewrite of the wrong algorithm*, and it costs one config flag rather than a rewrite.

This is the crux of the entire question. **Rust would let you keep the N+1 and not notice.** Fixing the N+1 makes the language irrelevant. Choosing Rust here means paying a rewrite to preserve a bug.

### 4.2 A high-frequency process-table / tmux poller

**Real win if the workload existed.** Polling a process table at 10 Hz across 50 sessions is syscall-bound with per-poll parsing; Rust would cut both.

**The workload does not exist at plausible scale.** The reconciler polls per cycle (5 s), not at high frequency. At 5 s × 50 sessions that's 10 polls/second — trivially within Python's reach. The measured problem in this area isn't poll rate, it's that `sessions/subprocess.py:209` reads *entire log files* to get the last 60 lines. Fix: `seek()` to the tail. That's a two-line change worth more than a language.

### 4.3 Transcript tailing across many concurrent sessions

**Genuinely the best structural fit for Rust — and it does not exist yet.** There is no JSONL tailer in `src/sessions/` or `src/runtimes/`: no `seek`, no offset tracking, no incremental reads. The only extant reader is `src/commands/system_commands.py:322-341`, which reads an entire transcript from byte 0 and `json.loads` every line, on every invocation, only when the `claude_usage` command is called.

Reason it through rather than assume: a Claude session emits on the order of 10–100 JSONL lines/minute. At 50 concurrent sessions that is at most ~85 lines/second, each a few KB. Python's `json.loads` handles ~100 MB/s. **You are asking for roughly 0.3% of one core.** This does not need Rust. If it ever did, `orjson` (a Rust extension, already the right shape of answer) gets 3–6x for one line in `pyproject.toml`.

### 4.4 A search/index layer

**Real win, wrong build-vs-buy.** `ToolRegistry.search_relevant_categories` re-tokenizes 130 tool schemas per query (2.0 ms). A vault-wide full-text search would be worse. But **SQLite FTS5 is already linked into the process** — it is a C extension, gets you BM25 ranking and incremental indexing, and requires zero new toolchain. Writing a search index in Rust to avoid a search index that ships with your existing database is not a trade.

### 4.5 True parallelism past the GIL

**No workload requires it.** The daemon's own CPU work is `_row_to_task` (946 ms at 100k) and `Scheduler.schedule` (79 ms). Both are *avoidable*, not parallelizable-necessary — pre-filtering to READY rows cuts the combined cost **6x** (1,693 ms → 301 ms) for byte-identical output. The actual heavy compute happens inside subprocesses (agent runtimes), which are already true OS-level parallelism outside the GIL entirely. **This is the correct architecture for an orchestrator and it is already the architecture.**

### 4.6 The C-extension answer to each remaining item

| Want | Rust rewrite | Existing answer | Delta |
|---|---|---|---|
| Faster JSON | custom serializer | `orjson` (itself Rust, via PyO3) | 3–6x, one dependency line |
| Faster event loop | custom runtime | `uvloop` (Linux/macOS) | 2–4x on I/O, one line |
| Search/index | custom index | SQLite FTS5, already linked | free |
| Faster DB round-trip | `rusqlite` | share one connection per cycle; drop per-query `BEGIN` | **495 µs of 885 µs recovered in Python** |
| One genuinely hot function | full rewrite | PyO3 extension for that function | surgical, keeps everything else |

**`orjson` deserves a specific note.** It is a Rust library. Adopting it is "using Rust" in the only sense that pays: a narrow, well-maintained, drop-in extension at a boundary that already exists. That is what a good Rust decision looks like here — not a rewrite.

---

## 5. Pricing the Rewrite

### Boundary options

| Approach | What it costs |
|---|---|
| **PyO3 / FFI** | Needs a genuinely hot, pure-CPU, low-data-transfer function. **No such function exists** — the CPU costs measured (`_row_to_task`, `Scheduler.schedule`) are avoidable work, and crossing FFI with 100k task objects would cost more in marshalling than the 946 ms it saves. |
| **Separate binary over IPC** | Adds a serialization boundary and a process to supervise. The very thing being optimized (DB round-trips at ~100 µs) would be *replaced* by IPC round-trips at comparable cost. Net-negative unless the Rust side owns the data. |
| **Sidecar owning the DB** | The only version that actually wins — and it means Rust owns `tasks`, `task_dependencies`, `gates`, `workspaces`, i.e. the schema. That is not "partial." |

### What breaks

- **Alembic** — the entire migration history. `tables.py` is the SQLAlchemy Core source of truth; `CLAUDE.md` mandates a reviewed autogenerated migration for every schema change. A Rust owner of the schema means either reimplementing migrations or maintaining two schema definitions that must never drift.
- **The `DatabaseBackend` protocol** — 21 query mixins composed into `SQLiteDatabaseAdapter` and `PostgreSQLDatabaseAdapter`. The dual-backend support (SQLite default, PostgreSQL supported) doubles the port.
- **~150 auto-exposed CommandHandler commands** — the MCP server derives its surface from Python introspection (`src/mcp_registration.py`). Every command crossing the boundary needs a hand-maintained shim.
- **The plugin system** — `src/plugins/` loads Python modules and calls Python entry points. 4 internal plugins plus external `aq-memory` plus third-party support. Plugins would need to stay Python, meaning the boundary cuts through the extension mechanism.
- **The test suite** — `pytest`, `pytest-asyncio` auto-mode, `pytest-xdist`. Property tests compare incremental recompute against full evaluation (`tests/test_blocked_state.py`). Cross-language equivalence testing is a new discipline.
- **`claude_agent_sdk`, `discord.py`, `fastapi`, `mcp`** — all Python-only. The daemon's entire I/O surface stays Python regardless.

### Toolchain, CI, contributors

Adding `cargo` + `maturin`/`setuptools-rust` means: cross-platform wheel builds (this project runs on Windows — the hardest target), a second dependency auditing surface, longer CI, and debugging that spans two runtimes with two different stack traces.

**And the owner's own stated reason for Python remains correct and is the strongest argument in the file.** Python is where the AI ecosystem lives — LangChain, the Claude Agent SDK, MCP's reference implementation, every provider SDK. `agent-queue` is a platform whose value proposition is that agents and plugins extend it. Every contributor who could write a plugin can write Python. A materially smaller number can write Rust, and approximately none want to write Rust *and* Python to contribute one plugin. **For a self-improving orchestration platform, contributor surface is a load-bearing feature, not a nice-to-have.** Trading it for a 173x improvement on a code path that should not exist is a bad trade at any exchange rate.

---

## 6. Recommendation

**Do not pursue a partial Rust rewrite. Fix the three bottlenecks in Python.** Estimated total: a few days of work, against measured multi-order-of-magnitude wins.

### Tier 1 — the cycle-overrun fixes

1. **Retire the legacy promotion scan.** Set `blocked_state_authoritative = True` (`src/config.py:1158`) after the shadow-mode observation window the design already specifies, then delete `_legacy_promotion_decisions` (`monitoring.py:103-181`). **16.9 s → 0.6 ms at 10k tasks; 172 s → 18.6 ms at 100k.** If the observation window isn't done, at minimum gate the legacy scan behind a sampling rate — running the slow oracle on 100% of tasks every 5 s is not what shadow mode requires.
2. **Stop loading the whole task table.** `core.py:2220` — replace `list_tasks()` with a READY-filtered query (`list_active_tasks` already exists at `task_queries.py:117`). **1,693 ms → 301 ms at 100k, 6x, identical scheduler output.** Verified by running the scheduler both ways.
3. **Offload the vault scan.** `vault_watcher.py:280` — `changes = await asyncio.to_thread(self._detect_changes)`. Copy the pattern from `workspace_spec_watcher.py:527`. **Removes 591 ms–2.8 s of frozen loop per cycle.**

### Tier 2 — round-trip amplification

4. **Batch the two per-project N+1 loops** in `_schedule()` (`core.py:2228`, `:2256`) into `GROUP BY` queries. **574 ms → ~3 ms at 500 projects, each.**
5. **Share one connection per cycle** instead of `engine.begin()` per query. **Recovers 495 µs of every 885 µs round-trip — 56%, in Python.**
6. **Memoize the duplicated `get_task` calls** in `AgentReconciler.reconcile()` (`agent_reconciler.py:55` and `:77` fetch the same rows twice) and the duplicated `get_task_contexts` in `src/prime/sections.py:170` vs `:265`.

### Tier 3 — the blocking-call audit items

7. Offload `llm_logger._append` (`llm_logger.py:357`), `cleanup_old_logs` (`core.py:2047` — copy `doctor/builtin.py:487` verbatim), and `_write_stub` (`workspace_spec_watcher.py:747`).
8. `sessions/subprocess.py:209` — `seek()` to the tail instead of reading whole log files.
9. `workspace_spec_watcher.py:679` — compare mtime/size *before* hashing. Recovers ~97% of an 11 s scan.
10. `plugins/loader.py` — replace 6 `subprocess.run` with `asyncio.create_subprocess_exec` to honor `CLAUDE.md:38`.
11. Consider `orjson` and (on Linux/macOS) `uvloop`. Two dependency lines, no architectural change.

### If you still want Rust in the codebase

The defensible version is: adopt `orjson` (Rust via PyO3, drop-in), and if a future profile ever shows one genuinely hot pure-CPU function with small data transfer, write *that function* as a PyO3 extension. Keep the daemon, the schema, the plugin system, the command surface, and the contributor pool in Python.

---

## 7. Answering the Question As Asked

> **"Does `agent-queue` have a performance problem that a partial Rust rewrite would solve?"**

It has a severe performance problem. A Rust rewrite would *mask* it — buying roughly 20x headroom on a code path that a one-line config flag makes 27,000x faster — at the cost of the migration system, the dual-backend abstraction, the plugin architecture, ~150 auto-exposed commands, the test suite, and the contributor pool.

**Your prior was correct in its conclusion and slightly wrong in its reasoning**, which is worth stating plainly since you asked to be contradicted:

- ✅ **Correct:** the daemon is architecturally I/O-bound; the language is not the lever; the real defects are synchronous blocking on the event loop.
- ⚠️ **Incomplete:** you expected no CPU-bound hot path. There *are* two — `_row_to_task` at 946 ms and `Scheduler.schedule` at 79 ms per cycle at 100k tasks. Both are real Python CPU costs on the 5-second loop. But both exist only because the code loads 100k rows to examine 20k, and both vanish with a `WHERE` clause. **"CPU-bound" and "needs a faster language" are not the same claim** — this is precisely the *"is Python the problem or is this code the problem"* distinction, and it lands firmly on the second.
- ⚠️ **Wrong on ordering:** you named SQLite write contention, fd limits, and loop-blocking as candidates for what breaks first. Loop-blocking is genuinely #3. But #1 is a request-amplification bug that breaks identically on PostgreSQL and would break on Rust too, just later. Write contention and fd limits never became binding at any tested scale.

**The three real bottlenecks, and none of them is the language:**

1. `_legacy_promotion_decisions` — an N+1 scan that shadow mode runs at 100% every 5 s, and that *decides* by default. 16.9 s/cycle at 10k tasks.
2. `list_tasks()` unfiltered in `_schedule()` — 1.76 s and ~1.7 s of frozen event loop per cycle at 100k tasks.
3. Synchronous blocking I/O on the event loop — chiefly the vault watcher's un-offloaded `os.walk`, plus per-LLM-call log writes and whole-file log reads.

The most persuasive evidence is internal to the project: `log_blocked_flips` already went **13.9 s → 0.06 s (230x)** by batching transactions, in Python, without changing a line of language. The incremental blocked-state recompute is **flat at 8.3 ms from 1k to 100k tasks** because someone bounded the working set correctly. **This codebase has already demonstrated, twice, that it knows how to solve its performance problems — and neither solution was Rust.**
