# Supervisor-Agent Phases 3–5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the message delivery engine (P3), cut project chat over to supervisor sessions (P4), and cut plan discovery over to planner task graphs (P5), per `docs/specs/implementation/supervisor-agent.md` §5–§11.

**Architecture:** A new `src/messages/` package holds a pure `MessageDeliveryEngine` that consumes two protocols — the existing DB message mixin and a new `SessionManagerProto` adapter over the session runtime (`orch.session_providers` / `session_spec_builder` / DB session rows). The cascade ticks the engine each cycle behind `messages.enabled`. P4 reroutes Discord chat into `message.send` + supervisor sessions behind `supervisor_agent.enabled` / `legacy_chat`. P5 gates the plan.md pipeline behind `planner.legacy_plan_discovery`.

**Tech Stack:** Python 3.12, SQLAlchemy Core (async), pytest-asyncio (auto), existing session runtime (`src/sessions/`), existing message substrate (P0–P2, already merged).

## Global Constraints

Copied from `docs/specs/implementation/supervisor-agent.md` (the spec) — every task implicitly includes these:

- **The spec is source of truth.** Before implementing, read the spec sections named in the task. If plan and spec conflict, STOP and escalate.
- Delivery policy (spec §5): activity `sleeping` → ensure_started (wake) + nudge; `idle` → nudge, then `mark_delivered(via="nudge")`; `busy` → skip (the UserPromptSubmit hook injects at the next prompt boundary); `absent` + to_kind=task → leave pending (rides into `aq prime`); to_kind=user → mark delivered `via="platform"` and emit `message.sent` for platform adapters/WS.
- `mark_delivered` is CAS on `delivered_at IS NULL` (src/database/queries/message_queries.py:152). Never bypass it; a `False` return means someone else delivered — do not double-emit events.
- Supervisor sessions are named `supervisor-<project_id>`; agent sessions keep their existing `s-<name>` naming.
- All timestamps are Float epoch seconds set in Python, never DB `now()`.
- Config flags already exist (src/config.py:1033–1086): `messages.enabled` (False), `messages.delivery_interval` (5.0), `messages.reply_timeout` (120.0), `messages.transcript_tail_fallback` (True), `messages.max_inject_per_prompt` (10), `supervisor_agent.enabled` (False), `supervisor_agent.legacy_chat` (True), `supervisor_agent.idle_timeout` (900), `planner.legacy_plan_discovery` (True). Do NOT add new flags; do NOT flip defaults except where a task explicitly says so.
- Commands return `{"success": bool, ...}` dicts; all state changes go through `CommandHandler`.
- Async-first: no sync `subprocess.run()` in production code.
- Line length 100, ruff py312. Run `ruff check src tests --fix` before each commit.
- Existing tests must stay green: `tests/test_message_queries.py test_message_commands.py test_api_messages.py test_cli_messages.py test_task_graph.py test_session_spec.py test_session_runtime_units.py test_session_commands.py`.
- Never edit `src/database/tables.py` without an Alembic autogenerate migration. (No schema changes are expected in this plan; if you think you need one, escalate.)

---

## File Structure

```
src/messages/__init__.py          new — package exports
src/messages/session_lens.py      new — SessionManagerProto + SessionLens adapter
src/messages/delivery.py          new — MessageDeliveryEngine
src/profiles/defaults/supervisor/profile.md   new — shipped profile
src/profiles/defaults/planner/profile.md      new — shipped profile
src/profiles/defaults/reviewer/profile.md     new — shipped profile
src/orchestrator/core.py          modify — cascade wiring (~line 2007–2040)
src/commands/surface_commands.py  modify — _cmd_prime renders pending messages (line 169)
src/sessions/default_harnesses/claude.md      modify — UserPromptSubmit hook
src/main.py                       modify — legacy_chat gate + initialize() hardening (lines 96–122)
src/discord/bot.py                modify — on_message route gate (line ~1254)
src/orchestrator/execution.py     modify — legacy_plan_discovery gate (~977–1160)
src/orchestrator/approval.py      modify — legacy_plan_discovery gate (lines 224, 282)
src/orchestrator/git_ops.py       modify — caller gates (lines 487–508)
tests/test_message_delivery.py    new
tests/test_session_lens.py        new
tests/test_supervisor_cutover.py  new
tests/test_planner_cutover.py     new
```

---

### Task 1: `SessionManagerProto` + `SessionLens` adapter

**Spec sections to read first:** supervisor-agent.md §5 (SessionManagerProto), §6 (supervisor session lifecycle), §7 (transcript-tail fallback).

**Files:**
- Create: `src/messages/__init__.py`, `src/messages/session_lens.py`
- Test: `tests/test_session_lens.py`

**Interfaces:**
- Produces (later tasks rely on these exact names):

```python
from typing import Literal, Protocol

Activity = Literal["idle", "busy", "sleeping", "absent"]

class SessionManagerProto(Protocol):
    async def activity(self, *, kind: str, target_id: str, project_id: str | None) -> Activity: ...
    async def ensure_started(self, *, kind: str, target_id: str, project_id: str | None) -> bool: ...
    async def nudge(self, *, kind: str, target_id: str, project_id: str | None, text: str) -> bool: ...
    async def tail_assistant_turn(self, *, kind: str, target_id: str,
                                  project_id: str | None, since: float) -> str | None: ...
```

- `SessionLens(db, providers, spec_builder, harness_registry, config, profiles_loader)` implements the protocol against the real runtime.

**Behavior (from spec §5–§6):**
- `kind` is a message `to_kind`: `"task"`, `"agent"`, `"supervisor"`. (`"user"` never reaches the lens.)
- Target resolution: for `task`/`agent`, look up the live session row for that task/agent in the DB sessions table (the reconciler owns those rows — read-only here). For `supervisor`, session name is `supervisor-<project_id>`.
- `activity()`: no session row / provider `is_running` False → `"absent"` for task/agent, `"sleeping"` for supervisor (supervisor is wake-able on demand; agent sessions are task-launched and must not be spawned by the messenger). Session running and `provider.last_activity` within the busy window (30s) → `"busy"`; otherwise `"idle"`.
- `ensure_started()`: only valid for `kind="supervisor"`. If already running → True. Otherwise build a spec via `SessionSpecBuilder.build_named_spec` (src/sessions/spec.py:198 — study its full signature and its existing tests before calling; it currently has no production caller) using the `supervisor` profile (Task 6 ships it; until then tests use fakes), `work_dir` = the project's vault or workspace dir per spec §6, `session_id=str(uuid.uuid4())` (dashed — `claude --session-id` rejects bare hex), and start via the provider registry. For other kinds return False.
- `nudge()`: resolve the handle, call `provider.nudge(handle, text)`; `NotSubmitted` / `CapabilityUnsupported` → return False (delivery engine falls back to leaving the message pending).
- `tail_assistant_turn()`: use `orch.transcript_watcher` reader state / transcript files to return the last assistant turn newer than `since`, else None. Read `src/sessions/transcripts/` to find the reading API; do not spawn a second watcher.

**Steps:**

- [ ] **Step 1: Write failing tests** in `tests/test_session_lens.py` using `FakeProvider` (src/sessions — it already has `nudge` recording at :203, `swallow_next_nudge()`, per-session `activity`) and an in-memory DB fixture (crib from `tests/test_session_runtime_units.py`). Cover: absent task → `"absent"`; running+recent activity → `"busy"`; running+stale → `"idle"`; supervisor with no session → `"sleeping"`; `nudge` returning False on `NotSubmitted`; `ensure_started` refuses non-supervisor kinds.
- [ ] **Step 2: Run tests, verify they fail** (`pytest tests/test_session_lens.py -v` → import errors / failures).
- [ ] **Step 3: Implement** `src/messages/session_lens.py` per the behavior block above. `src/messages/__init__.py` exports `SessionManagerProto`, `Activity`, `SessionLens`.
- [ ] **Step 4: Run tests, verify pass.** Also run `pytest tests/test_session_spec.py tests/test_session_runtime_units.py -q` (no regressions).
- [ ] **Step 5: Commit** `feat(messages): SessionManagerProto and SessionLens adapter`

---

### Task 2: `MessageDeliveryEngine`

**Spec sections to read first:** supervisor-agent.md §5 (engine + policy table), §7 (reply timeout & transcript tail), §11.1 (open questions — resolutions below are binding).

**Files:**
- Create: `src/messages/delivery.py`
- Test: `tests/test_message_delivery.py`

**Interfaces:**
- Produces:

```python
class MessageDeliveryEngine:
    def __init__(self, db, sessions: SessionManagerProto, config, bus=None): ...
    async def run_delivery_pass(self) -> dict:   # {"success": True, "delivered": n, "skipped_busy": n, "parked": n}
    async def check_reply_timeouts(self) -> int  # messages resolved via transcript tail
```

- Consumes: `SessionManagerProto` (Task 1), `MessageQueriesMixin` (create/get_pending/mark_delivered/archive — src/database/queries/message_queries.py), event bus `emit` for `message.delivered` / `message.sent` (schemas already registered, see src/events schema ids 474/485/490).

**Binding resolutions of spec §11.1 open questions** (present these to the reviewer as decided, not open):
1. **Message scoping:** `get_pending_messages(to_kind, to_id)` stays project-unscoped — recipient ids are globally unique (`task-…`, agent ids, project-scoped supervisor id is `<project_id>` itself with `to_kind="supervisor"`). The engine derives `project_id` from each message row for lens calls. No query change.
2. **mark_delivered on archived rows:** add `messages.c.archived_at.is_(None)` to the CAS predicate in `mark_delivered` (message_queries.py:152) so an archived message can never be claimed. Update `tests/test_message_queries.py` accordingly (add one test; don't rewrite existing ones).
3. **db access:** the engine only calls public mixin methods on `db` — never `db._engine`, never new transactions.

**Delivery pass algorithm (per policy in Global Constraints):**
1. Enumerate distinct pending recipients: `SELECT` via a new small mixin method `get_pending_recipients()` → `list[tuple[to_kind, to_id, project_id]]` (add to `MessageQueriesMixin` with a test), or iterate `get_pending_messages` per recipient discovered from `list_messages` — prefer the new method; keep it read-only.
2. For each recipient: `kind == "user"` → for each pending message `mark_delivered(via="platform")`; if claimed, emit `message.sent`. Otherwise call `sessions.activity(...)`:
   - `busy` → skip recipient (count `skipped_busy`).
   - `sleeping` → `ensure_started`; if started, treat as `idle`.
   - `idle` → render batch (up to `messages.max_inject_per_prompt`, priority order from `get_pending_messages`) as one nudge text block (format per spec §5: sender, subject, body, `msg-<id>`), `sessions.nudge(...)`; on True → `mark_delivered(via="nudge")` each + emit `message.delivered`; on False → leave pending.
   - `absent` → leave pending (rides into prime). Messages pending longer than 24h (`park_after`, module constant `PARK_AFTER_SECONDS = 86400`) to a task/agent recipient are re-addressed to the user: create a user-addressed copy referencing the original and archive the original (spec §7 "park stale"); count `parked`.
3. `check_reply_timeouts()`: when `messages.transcript_tail_fallback` and a delivered-but-unreplied message older than `messages.reply_timeout` exists whose recipient session produced an assistant turn after delivery, capture `tail_assistant_turn(since=delivered_at)` and create the reply message with `via="transcript_tail"`, `reply_to_id` set, thread preserved (spec §7).

**Steps:**

- [ ] **Step 1: Write failing tests** with a `FakeSessionManager` (dict-driven activity, records nudges, scripted tail) and the real DB fixture from `tests/test_message_queries.py`. Cover: idle→nudge+delivered+event; busy→skipped, still pending; sleeping supervisor→ensure_started then nudge; absent task→pending untouched; user→delivered `via="platform"` + `message.sent`; nudge False→still pending; CAS race (pre-mark one message)→no double event; park after 24h; transcript-tail reply creation; archived rows unclaimable by `mark_delivered`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `delivery.py`, `get_pending_recipients()` mixin method, and the `mark_delivered` predicate change.
- [ ] **Step 4: Run** `pytest tests/test_message_delivery.py tests/test_message_queries.py -v` → all pass.
- [ ] **Step 5: Commit** `feat(messages): MessageDeliveryEngine with policy, parking, transcript-tail fallback`

---

### Task 3: Cascade wiring + hook + prime rendering

**Spec sections to read first:** supervisor-agent.md §5 (cascade integration), §8 (prompt-boundary inject + prime).

**Files:**
- Modify: `src/orchestrator/core.py` (`run_one_cycle`, insert after step 4b `_check_failed_blocked_tasks` at ~line 2007, before Phase-3 housekeeping ~2040; a placeholder comment sits at :2119)
- Modify: `src/commands/surface_commands.py` (`_cmd_prime` at :169)
- Modify: `src/sessions/default_harnesses/claude.md` (hook JSON)
- Test: extend `tests/test_message_delivery.py` (cascade unit) and the prime tests (find them: `grep -rl _cmd_prime tests/`)

**Behavior:**
- Orchestrator constructs `SessionLens` + `MessageDeliveryEngine` in `__init__` near the session-runtime block (core.py:343–383), unconditionally (like the rest — nothing runs unless enabled).
- In `run_one_cycle`: if `config.messages.enabled`, and at least `messages.delivery_interval` seconds since the last pass (track `self._last_delivery_pass: float`), `await self.message_delivery.run_delivery_pass()` then `check_reply_timeouts()`. Wrap in try/except with `logger.exception` — a delivery failure must never break the cascade.
- Harness hook: add to the claude harness `hook_files` JSON a `UserPromptSubmit` hook running `aq inbox --inject` (the CLI flag exists — src/cli/messages.py:240/245). Study the existing hook file structure under `src/sessions/` (the `.aq/hooks/claude.json` template and how `hook_files` maps) and the existing `SessionStart` hook for the correct JSON shape. The hook must be a no-op (exit 0, no output) when there are no pending messages or the daemon is unreachable — never block the agent's prompt.
- Prime: `_cmd_prime` output gains a `messages` section — pending messages for the priming task/agent, rendered like the inject format, and marked delivered `via="prime"` (CAS) after rendering. Only when `config.messages.enabled`.

**Steps:**

- [ ] **Step 1: Failing tests** — cascade: enabled+interval elapsed → engine called once; disabled → never; engine raising → cycle continues. Prime: pending message appears in prime payload and is marked delivered `via="prime"`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the three integration points.
- [ ] **Step 4: Run** the new tests + `pytest tests/test_message_commands.py tests/test_session_spec.py -q`.
- [ ] **Step 5: Commit** `feat(messages): cascade delivery pass, UserPromptSubmit inject hook, prime inbox`

---

### Task 4: `aq chat` live mode

**Spec sections to read first:** supervisor-agent.md §8 (chat UX); `src/cli/messages.py` (`aq chat --once` poll mode at :401).

**Files:**
- Modify: `src/cli/messages.py`
- Test: extend `tests/test_cli_messages.py`

**Behavior:** `aq chat` without `--once` runs a live loop: send the user's line via `message.send` (to_kind `supervisor`, to_id = project id), then stream replies. Prefer subscribing to the daemon's event stream (`/ws/events` or the SSE endpoint — check `src/api/` for which exists post-S3) filtered to `message.*` for this thread; fall back to the existing `list_messages(since=…)` poll if the stream is unavailable. Ctrl-C exits cleanly.

**Steps:**

- [ ] **Step 1: Failing tests** — live loop sends and prints a scripted reply (mock the client/stream); fallback poll path; clean exit.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_cli_messages.py -v`.
- [ ] **Step 5: Commit** `feat(cli): aq chat live mode over event stream with poll fallback`

---

### Task 5: P3 integration test — checkpoint C-P3

**Files:**
- Create: `tests/test_delivery_integration.py`

**Behavior:** end-to-end through the real Orchestrator cascade with `FakeProvider` sessions and messages enabled in test config: user→supervisor message wakes (fake) supervisor session and is delivered via nudge; task→task message to a busy session stays pending then delivers via prime; reply flows back and `aq message list` (command layer) shows the thread.

- [ ] **Step 1: Write the test** (this is the verification task; it may pass partially from prior tasks — drive out any integration gaps it finds).
- [ ] **Step 2: Run full message+session suite:** `pytest tests/test_message_queries.py tests/test_message_commands.py tests/test_message_delivery.py tests/test_session_lens.py tests/test_delivery_integration.py tests/test_api_messages.py tests/test_cli_messages.py -q` → all pass.
- [ ] **Step 3: Commit** `test(messages): P3 end-to-end delivery integration`

---

### Task 6: Shipped profiles (P4 start)

**Spec sections to read first:** supervisor-agent.md §6 (profile contents), §9 row for profiles.

**Files:**
- Create: `src/profiles/defaults/supervisor/profile.md`, `src/profiles/defaults/planner/profile.md`, `src/profiles/defaults/reviewer/profile.md`
- Modify: profile seeding (find how `src/sessions/default_harnesses/` gets seeded into the vault — mirror that mechanism for `src/profiles/defaults/` in `src/profiles/sync.py` or the vault bootstrap; study before writing)
- Test: extend the profile-sync tests (`grep -rl seed tests/ | grep -i profile`)

**Behavior:** three markdown profiles with frontmatter + `## Config` JSON per the existing profile format (see `vault/agent-types/*/profile.md` shape and `src/profiles/parser.py`). Supervisor: `{"harness": "claude", "runtime": "claude_sdk"}`-style config per spec §6 with `idle_timeout` honored from `supervisor_agent.idle_timeout`; body text = role instructions from spec §6 (copy the spec's prose, don't invent). Seeding is copy-if-absent (never clobber operator edits), same rule as harness seeding.

- [ ] **Step 1: Failing test** — fresh vault seeds all three; existing edited file untouched on reseed.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** profiles + seeding.
- [ ] **Step 4: Run** profile/sync tests.
- [ ] **Step 5: Commit** `feat(profiles): shipped supervisor/planner/reviewer default profiles with seeding`

---

### Task 7: Chat routing cutover (P4)

**Spec sections to read first:** supervisor-agent.md §9 rows 1–3 (main.py:84–97 gate, discord/bot.py on_message, plugins core.py:412 invoke_llm fallback), §10.

**Files:**
- Modify: `src/main.py` (:96–122), `src/discord/bot.py` (on_message → `self.agent.chat` at :1254; auth-retry twin at :2274), `src/runtimes/supervisor.py` (`initialize()` hardening)
- Test: `tests/test_supervisor_cutover.py`

**Behavior:**
- **Bug fix (required):** `Supervisor.initialize()` currently lets chat-provider construction exceptions escape (genai raises ValueError on missing key → daemon crash), while main.py:105 clearly intends a non-fatal False. Wrap provider creation in try/except, log the error, return False.
- main.py: when `config.supervisor_agent.enabled and not config.supervisor_agent.legacy_chat`, skip `shared_supervisor.initialize()` chat wiring for chat purposes (supervisor-platform *tasks* still need it — read §9 row 1 carefully and gate only the chat path, not the runtime registration).
- discord/bot.py: when `supervisor_agent.enabled` and not `legacy_chat`, `on_message` routes to `message.send` (`_cmd_message_send`, to_kind `supervisor`, to_id = resolved project id from `_channel_to_project`) instead of `self.agent.chat`. Replies reach Discord via the `message.sent` event → messaging adapter (verify the adapter subscribes; if not, wire the subscription in the Discord adapter). Apply the same gate to the auth-retry twin at :2274.
- plugins `core.py:412` `invoke_llm` fallback stays on the legacy supervisor (spec says it keeps working) — no change, but add a test proving it still resolves.
- **Do not flip `legacy_chat` default in this task.**

- [ ] **Step 1: Failing tests** — initialize() returns False (no crash) on provider error; flags off → legacy path; flags on → `_cmd_message_send` called with supervisor recipient; reply event reaches the adapter send path.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_supervisor_cutover.py -v` plus existing discord/supervisor tests (`grep -rl "on_message\|Supervisor" tests/ | head`).
- [ ] **Step 5: Commit** `feat(supervisor): route project chat to supervisor sessions behind flags; harden initialize()`

---

### Task 8: Planner cutover gates (P5)

**Spec sections to read first:** supervisor-agent.md §9 rows 4–6 (execution.py:977–1160, approval.py:224/:282, git_ops.py:487–508), §11 P5 checklist.

**Files:**
- Modify: `src/orchestrator/execution.py` (~:977–1160 — condition :1034, `break_plan_into_tasks` :1083), `src/orchestrator/approval.py` (`_phase_plan_discover` :224, `_phase_plan_generate` :282), `src/orchestrator/git_ops.py` (callers :487–508)
- Test: `tests/test_planner_cutover.py`

**Behavior:**
- Every legacy plan-discovery entry point checks `config.planner.legacy_plan_discovery`. True (default) → today's behavior, byte-for-byte. False → the legacy region is skipped and plan work arrives as a planner task graph instead: the discovery trigger creates a task via `_cmd_create_task_graph` (src/commands/task_commands.py:1192) targeting the `planner` profile, per spec §9. Read the spec's exact wording for what replaces each site; if the spec leaves a replacement undefined, the new path for that site is "skip + log at info" — do not invent behavior.
- **Drain:** before the new path can matter, in-flight `AWAITING_PLAN_APPROVAL` tasks must finish on the legacy path regardless of flag — gate on task state, not just config, so flipping the flag mid-flight strands nothing (spec §11 P5).
- **Do not flip `legacy_plan_discovery` default** — the flip is Task 9's call.

- [ ] **Step 1: Failing tests** — flag True → legacy functions invoked (spy); flag False → planner task-graph creation invoked, legacy region skipped; in-flight AWAITING_PLAN_APPROVAL task with flag False still completes via legacy.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_planner_cutover.py -q` + `pytest tests/test_orchestrator.py -q` (or the closest execution/approval suites).
- [ ] **Step 5: Commit** `feat(planner): gate legacy plan discovery behind planner.legacy_plan_discovery with drain`

---

### Task 9: Docs + rollout notes (defaults stay conservative)

**Files:**
- Modify: `docs/specs/implementation/supervisor-agent.md` (§11 checklist ticks, §11.1 resolutions recorded), `CLAUDE.md` if the Quick Reference gains `src/messages/`
- No default flips: `messages.enabled`, `supervisor_agent.enabled` remain False; `legacy_chat`, `legacy_plan_discovery` remain True. Flipping is an operational decision made after a live E2E, not in this branch. Record that explicitly in §11.

- [ ] **Step 1: Update the spec checklist + record §11.1 resolutions (from Task 2) in the spec.**
- [ ] **Step 2: Run full suite:** `pytest tests/ -n auto` → green.
- [ ] **Step 3: Commit** `docs(supervisor): P3-P5 checklist, §11.1 resolutions, rollout notes`

---

## Self-Review Notes

- Spec coverage: §5 engine+lens (T1–T2), cascade+hook+prime §5/§8 (T3), chat UX §8 (T4), C-P3 §12 (T5), profiles §6 (T6), routing §9/§10 (T7), planner §9/§11 (T8), docs §11 (T9).
- Deviation from spec §11 declared: default flag flips deferred to post-merge live validation (recorded in T9). If the human wants the flips in-branch, do them as a follow-up commit after C-P3-style live test.
- Type consistency: `SessionManagerProto` names are identical in T1 (producer) and T2/T3 (consumers); `via` values used: `nudge`, `prime`, `platform`, `transcript_tail`.
