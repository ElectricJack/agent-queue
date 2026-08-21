# Wave 3 — Worktree P3–P6 · Session-Runtime S3 · Work-Graph WG-3/4/5

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the orchestrator's core loop: branches merge through a serialized merge slot, slots survive restarts and get reaped, running sessions stream transcripts over SSE, and gates/explain/outcomes make the work graph operational — then prove it with checkpoint C3 (two real tmux agents, two worktree slots, work landing on origin/main).

**Architecture:** Three independent lanes on three branches (`wave3/worktrees`, `wave3/session-s3`, `wave3/work-graph`), each in a dedicated git worktree at `<parent>/agent-queue-wt/<lane>`, executed by opus subagents, merged sequentially into `main` with the suite green after each. The implementation specs in `docs/specs/implementation/` are the source of truth — this plan fixes task boundaries, interfaces, and tests; the specs own algorithmic detail. No new Alembic migrations are needed (all DDL landed in Wave 0 substrate).

**Tech Stack:** Python 3.12, SQLAlchemy Core (SQLite + PostgreSQL dialect-aware), pytest (+xdist, tmux-marked tests on isolated sockets), FastAPI (SSE via StreamingResponse), tmux.

## Global Constraints

- Do **NOT** flip `work_graph.blocked_state_authoritative` (stays `False`; shadow mode). Spec: work-graph §9 stage 2 is a separate post-observation step.
- Do **NOT** flip `state_machine.enforce` (stays `False`; warn-only). Spec: work-graph §9 stage 4.
- Deferred data migrations P2-4/P2-5/P2-6 (work-graph §9.1) are **out of scope** — they ship with the authoritative flip.
- All commands return `{"success": bool, ...}` dicts; all state changes go through `CommandHandler`.
- Async-first: `GitManager` `a`-prefixed API only; never sync `subprocess.run()` in production code.
- ruff line-length 100, py312. `pytest tests/ -n auto` must be at the Linux baseline (413 pre-existing failures, zero NEW) before any merge.
- tmux-marked tests use per-test sockets (`f"aq-…-{tmp_path.name}"`), never the daemon's `aq` socket.
- Existing `_cmd_task_close` (src/commands/session_commands.py) is the close-task surface. Lane C **extends** it — do not create a parallel `_cmd_close_task`.
- Every new event type is registered in `src/event_schemas.py` before anything emits it.

---

## Lane A — worktree-execution P3–P6 (branch `wave3/worktrees`, opus)

Spec: `docs/specs/implementation/worktree-execution.md` §4–§6, §8–§10. Read it in full before starting.

### Task A1: Merge slot — queries + module

**Files:**
- Create: `src/database/queries/merge_slot_queries.py` (`MergeSlotQueriesMixin`, registered in the Database class beside the other mixins)
- Create: `src/orchestrator/merge_slot.py`
- Test: `tests/test_merge_slot.py`

**Interfaces:**
- Consumes: `merge_slots` table (already in `src/database/tables.py`: `project_id` PK/FK, `holder_task_id`, `acquired_at`, `expires_at`, `updated_at`); `WorktreesConfig.merge_slot_ttl_seconds` (exists, default 600).
- Produces:
  - `async def acquire_merge_slot(db, project_id: str, task_id: str, ttl: float) -> bool`
  - `async def renew_merge_slot(db, project_id: str, task_id: str, ttl: float) -> bool`
  - `async def release_merge_slot(db, project_id: str, task_id: str) -> None`
  - `async def break_expired_merge_slots(db, bus) -> int`

Acquisition must be one atomic conditional UPDATE (seed row via `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`; SQLite path wraps in `BEGIN IMMEDIATE`), per spec §5:

```sql
UPDATE merge_slots
   SET holder_task_id = :task, acquired_at = :now, expires_at = :now + :ttl, updated_at = :now
 WHERE project_id = :project
   AND (holder_task_id IS NULL OR holder_task_id = :task OR expires_at < :now)
```
`rowcount == 1` ⇒ acquired. Re-acquire by the current holder renews (idempotent).

- [ ] **Step 1:** Write failing tests in `tests/test_merge_slot.py`: (a) two concurrent `acquire_merge_slot` calls for the same project — exactly one returns True (use `asyncio.gather` on two tasks against one db); (b) holder re-acquire returns True and extends `expires_at`; (c) non-holder acquire against a live lease returns False; (d) expired lease is stealable; (e) `release_merge_slot` is idempotent (double release, release-by-non-holder is a no-op); (f) `break_expired_merge_slots` clears only expired leases and returns the count; (g) slot state survives a Database reopen (restart persistence).
- [ ] **Step 2:** Run `pytest tests/test_merge_slot.py -v` — all fail (module missing).
- [ ] **Step 3:** Implement the mixin (dialect-aware) and the four module functions per spec §5.
- [ ] **Step 4:** `pytest tests/test_merge_slot.py -v` — all pass.
- [ ] **Step 5:** Commit: `feat(worktrees): merge slot lease (P3) — atomic acquire/renew/release/break`

### Task A2: `_phase_integrate` + verify-skip + merge events

**Files:**
- Modify: `src/orchestrator/git_ops.py` (add `_phase_integrate`; wire into `_run_completion_pipeline` for worktree-mode tasks; `_phase_verify` skips auto-merge-to-default when the task's workspace is a slot)
- Modify: `src/event_schemas.py` (register `merge.started`, `merge.succeeded`, `merge.conflict`)
- Test: extend `tests/test_merge_slot.py` or the existing git-ops test file (follow its stub pattern)

**Interfaces:**
- Consumes: Task A1's four functions; existing `_run_completion_pipeline` / `_phase_verify` structure; `GitManager` async API; design §4.3 conflict handling.
- Produces: `async def _phase_integrate(self, ctx) -> ...` following the existing phase signature in git_ops.py. On conflict: task → BLOCKED with `rejection_reason` + conflicting files in task meta; emit `merge.conflict`; **never force-push**. Slot release in `finally`.

- [ ] **Step 1:** Read `_run_completion_pipeline` and `_phase_verify` in `src/orchestrator/git_ops.py` and the existing tests that stub git phases; write failing tests: (a) worktree-mode completion acquires the merge slot, rebases, pushes, merges per policy, releases the slot, emits `merge.started`/`merge.succeeded`; (b) merge-slot contention: second task's pipeline waits/retries (assert it does not run integrate while held); (c) injected rebase conflict ⇒ task BLOCKED, `rejection_reason` set, conflicting files recorded, `merge.conflict` emitted, slot released, no force-push; (d) exclusive-clone tasks keep today's `_phase_verify` auto-merge behavior (regression guard).
- [ ] **Step 2:** Run new tests — fail.
- [ ] **Step 3:** Implement `_phase_integrate` per spec §6.2/§6.5 and design §4.3; register the three merge events with payload schemas (task_id, project_id, workspace_id, branch, target_branch; conflict adds files list).
- [ ] **Step 4:** New tests pass; run the full git-ops + worktree test files.
- [ ] **Step 5:** Commit: `feat(worktrees): _phase_integrate under the merge slot (P3); merge.* events`

### Task A3: Reaper, adoption, branch pruning (P4)

**Files:**
- Modify: `src/orchestrator/worktree_manager.py` (add `adopt_existing`, `reap_slot`, `prune_branches`, `AdoptReport` dataclass)
- Modify: `src/orchestrator/core.py` (`_reap_worktree_slots` body: rate-limited sweep calling `break_expired_merge_slots` + `reap_slot` on retired slots + `prune_branches`; `_recover_stale_state` WORKTREE branch → adoption semantics)
- Modify: `src/git/` GitManager if missing helpers (`alist_merged_branches`, `adelete_local_branch` — add only if absent)
- Test: `tests/test_worktree_reaper.py`

**Interfaces:**
- Consumes: `WorktreeSlotManager` P1 methods (`ensure_slots`, sentinel I/O, `ensure_git_exclude`); `src/sessions/proctable.py` (liveness via `/proc` env/cwd scan — reuse, don't reimplement); Task A1's `break_expired_merge_slots`.
- Produces:
  - `async def adopt_existing(self, project) -> AdoptReport` (fields: `adopted: list[str]`, `repaired: list[str]`, `pruned: list[str]`)
  - `async def reap_slot(self, slot_ws, *, reason: str) -> bool` — returns False (no-op) when liveness is confirmed **or unknowable** (`/proc` scan failure ⇒ skip, never reap on unknown)
  - `async def prune_branches(self, base_ws, *, default_branch: str) -> list[str]`
  - `worktree.reaped` event emission (schema already registered)

- [ ] **Step 1:** Write failing tests in `tests/test_worktree_reaper.py` (bare-origin + clone fixture, copy the pattern from `tests/test_worktree_prepare.py`): (a) `reap_slot` removes a retired slot's worktree dir + row and emits `worktree.reaped`; (b) liveness guard: a live process with `AQ_TASK_ID` env or cwd inside the slot blocks reaping; (c) proc-scan failure ⇒ skip (returns False, nothing deleted); (d) `prune_branches` deletes merged `aq/*` local branches, keeps unmerged, honors `retain_failed_days`; (e) `adopt_existing` on boot: registered worktree with matching sentinel → adopted; stale git registration without dir → pruned; missing exclude block → repaired; (f) `_recover_stale_state` no longer deletes slot dirs/branches for IN_PROGRESS tasks.
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Implement per spec §6.2/§6.4/§6.5. `_reap_worktree_slots` in core.py: flag-gated (`worktrees.enabled`), rate-limited (only sweep every N cycles like the other slow cascade steps — follow the existing rate-limit pattern in core.py).
- [ ] **Step 4:** Tests pass; run `tests/test_worktree_manager.py tests/test_worktree_prepare.py tests/test_orchestrator.py` for regressions.
- [ ] **Step 5:** Commit: `feat(worktrees): slot reaper, boot adoption, branch pruning (P4)`

### Task A4: Surface — doctor, reap, list annotations (P5)

**Files:**
- Modify: `src/commands/worktree_commands.py` (`_cmd_workspace_doctor`, `_cmd_workspace_reap`)
- Modify: `src/commands/agent_commands.py::_cmd_list_workspaces` (annotate `role` base/slot, `slot_index`, `mode`, `branch`, `dirty`)
- Modify: `src/tools/definitions.py` (register the two new commands)
- Test: `tests/test_workspace_commands.py` (extend or create following existing command-test patterns)

**Interfaces:**
- Consumes: A3's `reap_slot`/`adopt_existing`; `list_slots_for_base`, `find_worktree_base` queries.
- Produces: `workspace_doctor` returns `{"success": True, "findings": [{"kind": str, "workspace_id": str, "detail": str}, ...]}`; `workspace_reap` takes `{"workspace_id": ...}` or `{"all_retired": True}`, refuses live slots with a per-slot reason.

- [ ] **Step 1:** Failing tests: doctor finding matrix (dirty slot, stale registration, missing exclude, redundant clone under worktree mode); reap refusal on a live slot; reap success on retired; `list_workspaces` rows carry the new annotations.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement per spec §6.8. **Step 4:** Pass. 
- [ ] **Step 5:** Commit: `feat(worktrees): workspace doctor/reap commands, list annotations (P5)`

### Task A5: Flip `worktrees.enabled` default (P6, partial)

**Files:**
- Modify: `src/config.py` (`WorktreesConfig.enabled` default → `True`)
- Test: adjust any test asserting the old default

Legacy `_cleanup_worktree_workspace` removal stays deferred (spec P6 burn-in) — do **not** delete it.

- [ ] **Step 1:** Flip the default; grep tests for `worktrees.enabled` assumptions; run `pytest tests/ -n auto` and fix only failures caused by the flip.
- [ ] **Step 2:** Commit: `feat(worktrees): worktree mode on by default (P6)`

---

## Lane B — session-runtime S3 (branch `wave3/session-s3`, opus)

Spec: `docs/specs/implementation/session-runtime.md` §3.6–§3.8, §5, §8. Read in full.

### Task B1: Transcript readers

**Files:**
- Create: `src/sessions/transcripts/__init__.py`, `base.py`, `claude.py`
- Create: `tests/fixtures/transcripts/claude/` (2–3 real-shaped Claude JSONL fixtures: a user turn, assistant turns with `usage`, tool_use/tool_result, a summary line to ignore)
- Test: `tests/test_transcript_readers.py`

**Interfaces:**
- Produces (spec §3.7 — exact signatures):

```python
@dataclass(frozen=True)
class TranscriptEntry:
    uuid: str
    parent_uuid: str | None
    type: str          # user|assistant|system|tool_use|tool_result
    text: str
    model: str | None
    usage: dict | None
    ts: float

class TranscriptReader(ABC):
    harness: ClassVar[str]
    def __init__(self, base_dir: Path | None = None): ...   # default Path.home(); override for tests + C3
    def resolve_path(self, work_dir: str, session_key: str | None) -> Path | None: ...
    async def read_new(self, path: Path, offset: int) -> tuple[list[TranscriptEntry], int]: ...
    def infer_activity(self, tail: list[TranscriptEntry]) -> str: ...  # "in-turn" | "idle"

def resolve_reader(harness: str, base_dir: Path | None = None) -> TranscriptReader | None: ...
```

`ClaudeTranscriptReader`: slugs `work_dir` (`/` and `.` → `-`) → `<base_dir>/.claude/projects/<slug>/`; picks `<session_key>.jsonl` when known else newest mtime; `read_new` is offset-based incremental (byte offset, tolerate a trailing partial line by returning it unconsumed).

The `base_dir` override is a deliberate extension of the spec: it makes readers testable and lets checkpoint C3 point a stub agent's fake transcript dir at tmp. Default behavior is exactly the spec's.

- [ ] **Step 1:** Failing tests: slug resolution (incl. dots in path); session_key pick vs newest-mtime fallback; missing dir ⇒ `resolve_path` None; incremental `read_new` across two appends with a partial trailing line; usage fields parsed; `infer_activity` returns "in-turn" when the tail ends in an assistant/tool_use entry newer than N seconds, "idle" after a completed turn (follow spec §3.7's definition).
- [ ] **Step 2:** Run — fail. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5:** Commit: `feat(sessions): transcript readers — base ABC + Claude JSONL (S3)`

### Task B2: TranscriptWatcher

**Files:**
- Create: `src/sessions/transcripts/watcher.py`
- Modify: `src/sessions/reconciler.py` or `src/orchestrator/core.py` — wire the watcher tick into the session reconciler cadence (spec §3.6; poll every `sessions.transcript_poll_seconds`, default 2)
- Modify: `src/database/queries/` token ledger — add `record_token_usage(project_id, agent_or_session_id, task_id, model, input_tokens, output_tokens)` **only if** no equivalent exists (grep `token_ledger` writers first; reuse if present)
- Test: `tests/test_transcript_watcher.py`

**Interfaces:**
- Consumes: B1 readers; session rows (`harness`, `work_dir`, `session_key`); EventBus; `token_ledger` table (has `model`/`input_tokens`/`output_tokens` from substrate).
- Produces: `class TranscriptWatcher` with `async def tick(self) -> None`; per live session: new entries ⇒ `bus.emit("notify.task_message", ...)` with `stream_id=session_id`; usage deltas ⇒ ledger; "in-turn" ⇒ `touch_session_activity()` + agent `last_heartbeat`; unresolvable path ⇒ one-shot `session.transcript_missing` event then peek-diff fallback (register the event schema).

- [ ] **Step 1:** Failing tests (fixture JSONL + FakeProvider sessions, no tmux): new-entry emission with correct stream_id; offset persistence across ticks (no duplicate emissions); usage delta lands one ledger row per assistant entry with usage; in-turn tick touches session activity; missing transcript emits `session.transcript_missing` exactly once then falls back.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement per §3.6/§3.7; register `session.transcript_missing` in event_schemas.py. **Step 4:** Pass + run `tests/test_session_reconciler.py` for regressions.
- [ ] **Step 5:** Commit: `feat(sessions): TranscriptWatcher — poll, events, ledger, activity (S3)`

### Task B3: SSE stream endpoint + `session logs` upgrade

**Files:**
- Create: `src/api/sessions.py` (router: `GET /api/sessions/{session_id}/stream`)
- Modify: `src/api/app.py::create_app()` (register router beside execute/health routers)
- Modify: `src/commands/session_commands.py::_cmd_session_logs` (transcript-sourced when a reader resolves; keep peek-diff fallback with `source: "peek"`; transcript path labels `source: "transcript"`)
- Test: `tests/test_session_stream_api.py`

**Interfaces:**
- Consumes: B1/B2; FastAPI `StreamingResponse`; ASGI test client (httpx) pattern from existing API tests.
- Produces: SSE stream — replay of normalized history (each `TranscriptEntry` as `data:` JSON), then live tail via watcher subscription, `: heartbeat` comment every 15 s, peek-diff fallback events when no transcript resolves; 404 for unknown session.

- [ ] **Step 1:** Failing tests via ASGI client: replay yields all fixture entries in order; entries appended after connect arrive (tail); unknown session ⇒ 404; no-transcript session yields peek-fallback frames; `session_logs` command returns transcript-sourced lines with `source: "transcript"` and falls back cleanly.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5:** Commit: `feat(sessions): SSE transcript stream + transcript-backed session logs (S3)`

### Task B4: Hook template payloads

**Files:**
- Modify: `src/sessions/spec.py` / `src/sessions/default_harnesses/claude.md` — fill the SessionStart / PreCompact / UserPromptSubmit hook payloads (spec §3.8; plumbing already renders hook_files to work_dir)
- Test: extend `tests/test_session_spec.py`

- [ ] **Step 1:** Read spec §3.8 hook requirements; write failing tests asserting rendered hook files contain the specified payloads. **Step 2:** Implement. **Step 3:** Pass.
- [ ] **Step 4:** Commit: `feat(sessions): harness hook payloads (S3)`

---

## Lane C — work-graph WG-3/4/5 (branch `wave3/work-graph`, opus)

Spec: `docs/specs/implementation/work-graph.md` §4, §6.2–§6.3, §7–§11. Read in full. **Global constraints apply hard here: no authoritative flip, no enforce flip, no P2-4/5/6 migrations.**

### Task C1: Gate queries + gate commands

**Files:**
- Create: `src/database/queries/gate_queries.py` (`GateQueriesMixin`, register in Database)
- Modify: `src/commands/gate_commands.py` (fill the empty mixin: `_cmd_gate_create`, `_cmd_gate_list`, `_cmd_gate_show`, `_cmd_gate_resolve`)
- Modify: `src/event_schemas.py` (`gate.created`, `gate.resolved`, `gate.expired`)
- Test: `tests/test_gate_queries.py`, extend `tests/test_work_graph_commands.py`

**Interfaces:**
- Consumes: `gates` + `task_gates` tables (already in tables.py); `recompute_blocked` from `blocked_state.py`.
- Produces (spec §4.3):
  - `create_gate(project_id, gate_type, title, *, question="", await_id=None, timeout_at=None, waiter_task_ids=()) -> str`
  - `resolve_gate(gate_id, *, resolved_by, resolution="") -> set[str]` (idempotent; returns task ids whose blocked state flipped)
  - `expire_open_gates(now) -> list[str]`
  - `list_gates(project_id=None, status=None, gate_type=None)`, `get_gates_for_task(task_id)`, `list_open_gates_by_type(gate_type)`

- [ ] **Step 1:** Failing tests: create+waiters rows; resolve is idempotent and returns flipped waiters (recompute called); expire only past-timeout open gates; list/filter combinations; command layer: create/list/show/resolve happy paths + resolve of unknown gate ⇒ `success: False`.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement; register the three gate events. **Step 4:** Pass.
- [ ] **Step 5:** Commit: `feat(work-graph): gate queries + gate commands (WG-3)`

### Task C2: `_sweep_gates` cascade + event-gate subscription

**Files:**
- Modify: `src/orchestrator/core.py::_sweep_gates` (fill the stub at cascade step 2b — **before** `_check_defined_tasks`)
- Modify: `src/orchestrator/approval.py` (extract `_poll_pr_merged(pr_url) -> bool | None` from `_check_pr_status`; both callers use it)
- Test: extend `tests/test_work_graph_cascade.py`

**Interfaces:**
- Consumes: C1 queries; `WorkGraphConfig.gate_sweep_interval_seconds` (exists, default 30; ≤0 disables); persisted `events` rows for event-gates (watermark = gate creation time); EventBus for immediate event-gate resolution.
- Produces: sweep resolving `timer`, `task`, `pr-merged`/`ci-run` (via `_poll_pr_merged`, fake `gh` in tests), and `event` gates; expiry of overdue gates; `gate.resolved`/`gate.expired`/`task.unblocked` audit writes; a `start()`-time bus subscription resolving open event-gates on matching `event_type`.

- [ ] **Step 1:** Failing tests: timer gate resolves when clock passes; task gate resolves on dep completion; event gate resolves both live (bus emit) and via sweep backstop (persisted row after gate creation); pr-merged with stubbed poller; timeout expiry; a resolved gate unblocks its waiter in the **same cycle** (sweep ordering before `_check_defined_tasks`); interval ≤0 disables.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement per §6.2; respect the sweep interval via the core.py rate-limit pattern. **Step 4:** Pass.
- [ ] **Step 5:** Commit: `feat(work-graph): gates sweep cascade + event-gate subscription (WG-3)`

### Task C3: Explain + ready frontier (WG-4)

**Files:**
- Create: `src/explain.py`
- Modify: `src/database/queries/task_queries.py` (`get_ready_frontier(project_id, *, labels=None, any_label=None)`)
- Modify: `src/orchestrator/monitoring.py` (cache `self._last_scheduler_state` snapshot each tick)
- Modify: `src/orchestrator/core.py::_describe_task_blocker` (thin wrapper over explain reasons)
- Modify: `src/commands/task_commands.py` (`_cmd_explain_task`, `_cmd_project_ready`)
- Test: `tests/test_explain.py`

**Interfaces:**
- Produces (spec §6.3):

```python
class Reason(TypedDict):
    code: str        # blocked_dependency|blocked_gate|no_idle_agent|workspace_locked|budget_exhausted|rate_limited
    detail: str
    ref: str | None  # task id / gate id / workspace id

def build_capacity_reasons(task, state, workspace_counts, idle_by_project) -> list[Reason]: ...
```
  - `explain_task` returns `{"success": True, "reasons": [Reason, ...]}` (graph reasons first, then capacity); cross-project deps name the other project in `detail`.
  - `project_ready` returns `{"success": True, "ready": [...], "withheld": [{"task_id", "reasons"}]}`; frontier excludes `hold:*`-labeled tasks.

- [ ] **Step 1:** Failing tests: one golden per reason code (fixture graphs/states); `hold:*` exclusion; cross-project dep naming; explain works between ticks via the cached scheduler state; `_describe_task_blocker` returns `reasons[0]` formatting.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5:** Commit: `feat(work-graph): explain + ready frontier (WG-4)`

### Task C4: Events replay + bus emitters (WG-4)

**Files:**
- Modify: `src/database/queries/event_queries.py::get_recent_events` (add `after_id: int | None = None` — ASC + `id > :after` when set)
- Modify: `src/api/websocket.py::WebSocketManager.handle` (parse `after_seq`; page replay then bridge to live; live frames carry `seq: null`)
- Modify: `src/event_schemas.py` + the flip-set return sites of `transition_task`/recompute callers in core.py: emit `task.blocked` / `task.unblocked` on the bus (spec §9.1 note #263 — we choose to **add the emitters**, since gate/explain/replay all reference them; audit writes already exist)
- Test: extend `tests/test_work_graph_cascade.py` + a websocket replay test beside existing websocket tests

- [ ] **Step 1:** Failing tests: `after_id` pagination is gapless and ordered; replay-then-live over the websocket yields no duplicates; blocked-flip emits `task.blocked`/`task.unblocked` on the bus with registered schemas (registry invariant test: every emitted type has a schema).
- [ ] **Step 2:** Run — fail. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5:** Commit: `feat(work-graph): event replay (after_seq) + task.blocked/unblocked emitters (WG-4)`

### Task C5: Outcomes, failure_class policy, hierarchical ids, group progress (WG-5)

**Files:**
- Modify: `src/orchestrator/execution.py` FAILED branch (~lines 1364–1392): read `failure_class` task meta; `"hard"` ⇒ `transition_task(..., BLOCKED, context="hard_failure")` skipping retry; else existing retry path
- Modify: `src/commands/session_commands.py::_cmd_task_close` — **verify** it already writes `outcome`, `failure_class`, `work_outcome`, `work_commit`, `work_branch`, `verification`, `close_notes` meta keys; add any missing key (do not create a second close command)
- Modify: `src/task_names.py::generate_task_id(parent_id: str | None = None)` — child ids `f"{parent_id}.{n}"`, sibling ordinal = max+1, depth cap 3 (at cap: fresh root id + `discovered-from` edge + warning)
- Modify: `src/database/queries/task_queries.py` (`get_group_progress(parent_id) -> dict` — done/ready/blocked/in_progress counts + Kahn waves over blocking edges among children; computed, never stored). Also `transition_task`: add `force: bool = False` and optional `event` parameter; behavior **unchanged** while `enforce=False` (warn-only) — only plumb the parameters and raise-path behind the flag.
- Test: `tests/test_work_graph_outcomes.py`

- [ ] **Step 1:** Failing tests: hard failure ⇒ BLOCKED without retry; soft/absent failure_class ⇒ retry path untouched; child id generation (ordinals, gaps, depth cap fallback with discovered-from edge); `get_group_progress` counts + wave computation on a small fixture graph; `transition_task(force=True)` bypasses validation when `enforce=True` (set flag in-test only), warn-only default unchanged; `InvalidTransition` carries `from_status`/`to_status`.
- [ ] **Step 2:** Run — fail. **Step 3:** Implement. **Step 4:** Pass + run `tests/test_session_commands.py` (close-path regression).
- [ ] **Step 5:** Commit: `feat(work-graph): failure_class policy, hierarchical ids, group progress, enforcement plumbing (WG-5)`

---

## Phase D — Integration (main session, after all lanes complete)

### Task D1: Sequential merges

- [ ] Merge order: `wave3/work-graph` → `wave3/session-s3` → `wave3/worktrees` (worktrees last: A5's default flip is the riskiest suite-wide change). After each merge: `pytest tests/ -n auto` vs the Linux baseline (413 pre-existing failures; **zero NEW**). Conflict surface is expected to be near-zero (disjoint files; shared touchpoints: `event_schemas.py`, `core.py` cascade — resolve by keeping both lanes' additions).
- [ ] Commit each merge; do not push without asking.

### Task D2: Adversarial review

- [ ] Dispatch one opus code-reviewer subagent per lane (3, parallel) reviewing the merged result against its spec: correctness, spec fidelity, missed edge cases, test honesty (do the tests actually pin the behavior?), and the global constraints (no authoritative/enforce flips, no force-push paths, "unknown is not dead" style liveness conservatism in the reaper).
- [ ] Fix accepted findings in the main session; re-run affected suites.

### Task D3: Checkpoint C3 — end-to-end on real tmux

**Files:**
- Create: `tests/test_checkpoint_c3.py` (`pytestmark = pytest.mark.tmux`, POSIX-guarded, per-test socket)

The C2 pattern (real Orchestrator, bare origin, stub-harness vault markdown, `_NullRuntimeFactory`) extends to prove the *whole* Wave-3 surface. Unlike C2, `_run_completion_pipeline` is **NOT stubbed** — integration is real, with merge policy `local` so work lands on the bare origin's `main`.

Scenario:
- [ ] **Two tasks, two slots, serialized merges:** seed two READY tasks on one project (cap 2), two stub tmux agents launch into slots 0 and 1 on branches `aq/tA`/`aq/tB`. Each stub, on nudge, writes a file and commits (the test drives the commit via `_git` in the slot — the stub agent only needs to exist for session realism). Close both via `task_close`. Assert: both `_phase_integrate` runs acquired the merge slot (never concurrently — record acquire order), both branches merged, origin `main` contains both files, slots released and reapable.
- [ ] **Gate wiring:** create task B with a `task`-type gate on task A; assert B stays blocked (shadow projection recorded) until A completes and the sweep resolves the gate same-cycle.
- [ ] **Transcript + SSE:** stub agent (or the test) appends Claude-shaped JSONL under `tmp/.claude/projects/<slug>/`; reader constructed with `base_dir=tmp`; assert `session_logs` returns `source: "transcript"` lines and the SSE endpoint replays them via ASGI client.
- [ ] **Restart adoption:** after task A completes but before reap, construct a second Orchestrator on the same db/config (simulated restart); assert `adopt_existing` adopts the remaining live slot and the reaper then reaps the retired one; tmux session for B survives adoption (C2's instance-token semantics).
- [ ] **Explain:** while B is gate-blocked, `explain_task(B)` returns a `blocked_gate` reason naming the gate.
- [ ] Run C3 three times sequentially + once under `-n auto` with the rest of the tmux-marked tests; then full suite vs baseline.
- [ ] Commit: `test(e2e): checkpoint C3 — two tmux agents, two slots, serialized merge, gates, transcripts`

Manual (not automatable, offer to the user): run the daemon for real with `worktrees.enabled` + `sessions.enabled`, queue a task against a real repo with the real `claude` harness, watch `aq session logs --follow` / the SSE stream, and see the PR/merge land.

---

## Self-review notes

- Spec coverage: worktree P3 (A1/A2), P4 (A3), P5 (A4), P6-partial (A5, legacy deletion deferred per spec burn-in); session S3 §3.7 (B1), §3.6 (B2), §3.8 (B3/B4); work-graph WG-3 (C1/C2), WG-4 (C3/C4), WG-5 (C5). Deferred by design: authoritative flip + P2-4/5/6, enforce flip, S4 readers (codex/gemini), supervisor delivery engine.
- Known overlap resolved: close-task surface is the existing `_cmd_task_close` (C5 verifies/extends).
- `event_schemas.py` is touched by all three lanes — additive registrations only; merge conflicts resolved by union.
- `base_dir` on TranscriptReader is a deliberate, minimal deviation from spec for testability (default = spec behavior).
