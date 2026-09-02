# Playbook V2 — Package 7 child plan: Drain, atomic cutover, rollback window, and V1 removal

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:executing-plans`. **Do not** use `superpowers:subagent-driven-development` for commits 1–3: roadmap §7 makes this package serial and operator-led, and §3.9 names three human gates that a fan-out would step over. Commit 4 (removal) has a bounded parallel lane, described in §3.1.

**Roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` §5 "Package 7 — Drain, atomic cutover, rollback window, and V1 removal".
**Spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md`, especially "Migration and cutover" steps 7–9 and "Cutover acceptance".
**Consumes:** Package 6's readiness report and all prior exit gates. **Produces:** V2 as the only execution path, a completed rollback window, deleted V1 runtime code, and read-only historical compatibility.

**Drafting status.** This plan was written against the live tree at `origin/main` `1b835131`, on the supervisor's instruction (task `solid-harbor.55`) to draft child plans in parallel ahead of their packages. Everything in §1 and §2 is a fact about the **current** tree and was verified by reading it.

**Package 0 has partially landed.** `src/commands/principal.py`, `src/commands/authorization.py`, `src/profiles/capabilities.py`, `SecurityConfig.capability_enforcement` (`src/config.py:1137`, default `"audit"`), the `## Capabilities` profile block (`src/profiles/parser.py:165`, `:688-710`), and Alembic revision `3b560dbd527c` are all in `origin/main`. Packages 1–6 are not. §3.8 therefore splits into *observed* symbols (Package 0, cited with live line numbers) and *expected* symbols (Packages 1–6, cited by the module the roadmap assigns). The implementation task **must** begin by reconciling the expected half against the tree and amending this document in its first commit; it must not silently substitute a differently-named symbol, and must not ship a local reimplementation of an earlier package's interface.

---

## 1. What this package is actually for

Packages 0–6 built V2 and proved it behaves. Package 7 is the only package that can break a running fleet, and it is the only one whose hardest problems are *operational* rather than architectural. Four properties of the live tree decide the whole shape of the work.

### 1.1 The drain has no reaper: a `running` V1 row outlives the process that owned it

V1 run state lives in two places that can disagree:

- **Durable:** the `playbook_runs` row (`src/database/tables.py:921-957`), whose `status` is one of `running | paused | completed | failed | timed_out | cancelled`.
- **In-memory:** `PlaybookManager._running: dict[str, asyncio.Task]` (`src/playbooks/manager.py:267`) and `_running_playbook_ids` (`:271`), populated on registration (`:684-691`) and popped on completion (`:706`).

Runs are dispatched fire-and-forget — `asyncio.create_task(_run_pipeline(), ...)` (`src/orchestrator/core.py:1040`) and `asyncio.create_task(_run(), ...)` (`src/orchestrator/core.py:1085`). Nothing reconciles the two on startup. `run_one_cycle` calls `_check_paused_playbook_timeouts` (`src/orchestrator/core.py:2758` → `src/orchestrator/monitoring.py:649` → `src/commands/playbook_commands.py:1765`), which only handles **paused** runs. A daemon restart while a run is executing therefore leaves a `running` row that no code path will ever move to a terminal status.

**Consequence:** the roadmap's outcome "Reach zero active V1 runs" is *unreachable by waiting*. Any fleet with history almost certainly already has orphaned `running` rows. The drain must classify each active row as **live** (its `run_id` is in `PlaybookManager.running_runs()`, so a real coroutine owns it) or **orphaned** (it is not), and must give the operator a terminal write for the orphaned ones. §3.2 makes that classification the core of `DrainStatus`; §5.1 T-2 is the failing test that pins it.

### 1.2 `cancel_playbook_run` does not cancel a live run

`_cmd_cancel_playbook_run` (`src/commands/playbook_commands.py:508-587`) writes `status="cancelled"` and emits `PlaybookRunCancelledEvent`. Its own docstring states the defect:

> This is a DB-record-level cancellation only — it does not signal an in-process `PlaybookRunner` currently executing a node to stop mid-flight; the runner has no mechanism today to notice the row changed underneath it. A live run that gets cancelled will finish its current node and then, on its next persistence write, silently overwrite the `cancelled` status back to `running`.

So the roadmap outcome "Allow operators to wait, resolve, or cancel" is not satisfied by the command that already exists. A drain built on it would report zero active runs and then watch the count go back up — the worst possible failure for a cutover gate, because it is silent.

**Consequence:** commit 1 must ship a cancel that actually cancels. The mechanism is already in the tree and unused: `PlaybookManager._running[run_id]` is the `asyncio.Task`. `Task.cancel()` plus an `await` on the task, followed by the terminal write, is a real cancel with a real ordering guarantee — the row is written *after* the coroutine is gone, so nothing can overwrite it. §3.3 `playbook_v1_run_cancel` specifies it; §5.1 T-3 is the regression test that would fail against today's command.

### 1.3 The subsystem ships paused, so "production traffic" is not a given

`PlaybooksConfig.enabled` defaults to `False` (`src/config.py:857-866`, docstring: "Paused by default during the framework overhaul — see docs/specs/design/feature-pauses.md"), and `CommandHandler` refuses all 22 `PAUSED_PLAYBOOK_COMMANDS` (`src/commands/handler.py:166-193`, checked at `:650`) while it is off. `src/main.py:388` prints the pause in the startup banner.

**Consequence:** the roadmap's "agreed observation period and acceptance metrics" (§10) cannot be defined purely as a wall-clock window, because a fleet may generate zero playbook events for days. §3.5 defines the observation window as **the later of** a wall-clock floor and an event-coverage floor, with a rehearsal (§5.1 T-5, §5.3 T-11) supplying the coverage when live traffic cannot. A window that can be satisfied by silence proves nothing.

### 1.4 "Entry points" is six call sites, and the API is not one of them

The roadmap names five entry points. The live tree has six V1 dispatch/resume sites, and the "API" is not a router:

| # | Site | Symbol | V1 call |
|---|---|---|---|
| 1 | `src/orchestrator/core.py:800` | `Orchestrator._on_playbook_trigger` — pipeline branch | `PipelineRunner` (`:838`, constructed `:950-957`) |
| 2 | `src/orchestrator/core.py:800` | same method — LLM branch | `PlaybookRunner` (`:811`, constructed `:1065`) |
| 3 | `src/orchestrator/assignment_routing.py:434` | `AssignmentRouter._route_batch` | `PlaybookRunner` (`:42` import, `:459` construction) |
| 4 | `src/playbooks/resume_handler.py:179` | `PlaybookResumeHandler._resume_run` | `PlaybookRunner.resume` (`:194`, `:260`) |
| 5 | **`src/workflow_stage_resume_handler.py:195`** | `WorkflowStageResumeHandler._resume_run` | `PlaybookRunner.resume_from_event` (`:210`) |
| 6 | `src/commands/playbook_commands.py` | `_cmd_run_playbook` (`:857`), `_run_pipeline_playbook` (`:977`), `_cmd_dry_run_playbook` (`:1032`), `_cmd_resume_playbook` (`:324`), `check_paused_playbook_timeouts` (`:1765`) | `PlaybookRunner` / `PipelineRunner` at `:375`, `:482`, `:940`, `:980`, `:1090`, `:1794` |

Site 5 is **not in the roadmap's Modify list**. It is a live resume path for workflow-stage playbooks and would keep V1 alive after a switch that only touched the other five. §2 records the addition.

There is **no playbook FastAPI router**: `src/api/routers/` contains only `proposals.py`, and every playbook command reaches HTTP through the generic `/api/execute` surface plus `src/api/codegen.py`'s per-command route generation (`src/api/models/playbook.py` supplies the response DTOs; `RESPONSE_EXCLUDE_NONE` at `codegen.py:70` already special-cases `playbook_graph_view`). Switching site 6 therefore *is* switching the API entry point. §2 records that too, because a reviewer told to check "playbook routes" will otherwise go looking for a file that does not exist.

---

## 2. Live-tree reconciliation — deviations from the roadmap's file list

Roadmap §3 permits a child plan to refine filenames after inspecting the live tree and requires the deviation be documented. Every row was verified against `origin/main` `1b835131`.

| Roadmap says | Live tree | Decision |
|---|---|---|
| Modify `src/orchestrator/core.py` | `_on_playbook_trigger` (`:800-1116`) is **two** entry points in one method: a `kind == "pipeline"` branch (`:837-1050`) and an LLM branch (`:1052-1110`) | Both switch together in commit 2. The switch reads one selector (§3.4) at the **top** of `_on_playbook_trigger`, before either branch, so there is exactly one decision point |
| Modify `src/orchestrator/assignment_routing.py` | Imports `PlaybookRunner` **and** `_parse_json_from_text` (`:42`). The second is a private helper re-exported from `src/playbooks/runner_context.py` and used by `validate_assignment_response` (`:85`) — a pure parser with no V1 execution semantics | Move `_parse_json_from_text` to a surviving module in commit 4 rather than deleting it. §3.6 names `src/playbooks/expressions.py` (Package 2) as the destination and requires the import to be updated in the same commit |
| Modify `src/playbooks/handler.py` | `handler.py` is **vault-change plumbing** — `PLAYBOOK_PATTERNS` (`:66`), `derive_playbook_scope` (`:73`), `on_playbook_changed` (`:139`), `register_playbook_handlers` (`:348`). It contains no runner reference and no execution path | **No change in commit 2.** It is authoring/compilation plumbing that V2 keeps. Recorded so a reviewer does not look for a switch here. Commit 5 updates its module docstring only |
| Modify `src/playbooks/resume_handler.py` | Matches: `_resume_run` (`:179`) calls `PlaybookRunner.resume` (`:260`) | Switch in commit 2 |
| (not listed) | **`src/workflow_stage_resume_handler.py:195`** `_resume_run` calls `PlaybookRunner.resume_from_event` (`:210`) | **Added to the Modify list** (§1.4). Missing it leaves a live V1 resume path after cutover |
| Modify `src/commands/playbook_commands.py` | Six V1 call sites (§1.4 row 6) plus `PAUSED_PLAYBOOK_COMMANDS` membership in `src/commands/handler.py:166-193` | Switch all six in commit 2; add the new drain commands to `PAUSED_PLAYBOOK_COMMANDS` **except** the drain/status readers, which must work while `playbooks.enabled=false` so an operator can drain a paused fleet (§3.3) |
| Modify "`src/api/models/playbook.py` and playbook routes" | `src/api/models/playbook.py` exists (391 lines, `PlaybookSummary`/`PlaybookRunSummary`/`InspectPlaybookRunResponse`, …). **There are no playbook routes** — `src/api/routers/` holds only `proposals.py`; playbook commands are auto-exposed through `/api/execute` and `src/api/codegen.py` | Add DTOs to `src/api/models/playbook.py`; regenerate `openapi.json` and both clients. No router file is created or deleted. §11 pins the regeneration commands |
| Modify "dashboard graph routing" | `dashboard/src/pages/PlaybookDetail.tsx:20` owns the tab list; V1 graph components live in `dashboard/src/pages/playbook-graph/` (6 files + `__tests__`). Package 5's plan §6.5 states Package 7 deletes the `graph` tab and that directory and renames `semantic` → `graph` | Carried verbatim as commit 4 work; §3.6 lists the files |
| Modify "operator documentation, metrics, and alerts" | **No playbook metrics exist.** `src/metrics/sampler.py` `_COUNTED_EVENTS = ("task.nudged", "session.killed", "merge.succeeded")` (`:57`); `collect()` (`:326`) emits agents/tasks/machine/daemon/stall/merges only. There are no alerts subsystem and no playbook series | Package 7 **creates** the four counters it needs (§3.5, §10) by extending `_COUNTED_EVENTS` and `collect()`. The bus events they count already exist: `src/notifications/events.py:386` `PlaybookRunFailedEvent`, `:450` `PlaybookRunTimedOutEvent`, `:476` `PlaybookRunCancelledEvent`, plus Package 0's `capability.denied` counter (pkg0 §3.6) |
| (not listed) | Package 0's plan §3.6 names a `capability.denied` **counter**. The landed code has only a `logger.warning("capability_denied ...")` at `src/commands/handler.py:912` (and `capability_denied_shadow` at `:902`). `CAPABILITY_DENIED = "capability_denied"` (`src/commands/authorization.py:34`) is a reason string, not an event name; nothing is emitted on the bus and nothing counts it | Acceptance measure 4 (§3.5) cannot be measured today. §10 makes Package 7 emit `capability.denied` on the EventBus at both `handler.py` call sites and count it in `src/metrics/sampler.py::_COUNTED_EVENTS`. Recorded here because a reviewer reading Package 0's plan will expect the counter to already exist |
| Modify "tests that currently instantiate V1 runners" | 30 files, ~1,100 tests. `tests/test_playbook_runner.py` alone is 9,619 lines / 385 tests | §3.6 gives every file one of three dispositions (delete / port / retarget-to-historical-read). A blanket "update related tests" is exactly what roadmap §9 forbids |
| Delete `src/playbooks/pipeline_compiler.py` | Imported by `src/playbooks/compiler.py:109` and `:157` (`compile_pipeline`), which is the **surviving** V2-era compiler entry | Deletion requires editing `compiler.py`'s two lazy imports in the same commit. §3.6 pins it |
| Delete `src/playbooks/token_tracker.py` | Imported by `src/playbooks/runner.py:49` **and** `src/playbooks/runner_transitions.py:30` (`_estimate_tokens`) — both die with it | Clean delete, no survivor depends on it. Verified: `grep -rn "token_tracker" src/ --include=*.py` returns only those two plus its own docstring |
| (not listed) | `src/playbooks/__init__.py:11` imports `PlaybookRunner` and lists it in `__all__` (`:31`) | Added to the Modify list. This is the public import surface; leaving it is an `ImportError` on every `import src.playbooks` |
| (not listed) | Six `src/playbooks/` modules are neither on the roadmap's delete list nor obviously V2: `state_machine.py`, `services.py`, `run_task.py`, `conditions.py`, `routing.py`, `assignment_compiler.py` | §3.6 gives each an explicit disposition. Three survive unchanged, one survives with a deletion inside it, two die with V1 |
| (not listed, and the largest omission) | **`src/playbooks/routing.py` is task-admission code, not playbook code.** `requires_routing_gate` (`:100`) is called *inside the task-creation transaction* by `src/commands/task_commands.py:1473` and `src/task_graph/creator.py:385`, and `uses_default_triage` (`:127`) by `src/orchestrator/triage.py:24`. Both answer their question by walking the **V1 compiled action graph** — `pb.to_dict()`, `graph["pipeline_rules"]`, `node["action"]["command"]`, `node["action"]["args"]` (`_rule_actions`, `:67-95`) | Deleting V1 without reimplementing these against the V2 artifact silently stops attaching routing gates to unrouted tasks and stops triage recovery — a correctness regression **outside** the playbook subsystem, with no playbook test to catch it. §3.7 makes this its own commit-3 task (T-9/T-10) with `tests/test_routing_admission.py` and `tests/test_graph_routing_admission.py` as the ratchet |
| (not listed) | `tests/conftest.py:237` `DEFAULT_PIPELINE_PATH` and `tests/conftest.py:322` `PipelineEngine` — the V1 dispatch harness. (Package 6's plan cites `:306`; the live line is `:322`) | Deleted in commit 4 with the shadow-parity V1 arm, per Package 6 §14 |
| (no storage change implied) | The rollback window needs a durable `switched_at`, and the drain needs a durable audit of who closed V1 admission | **One additive Alembic revision** creating `playbook_cutover_events` (§6). Append-only; `downgrade` drops it |
| Verification: "full Python test suite: `pytest -q`" | CLAUDE.md forbids casual full runs; `aq test` is the gated wrapper, and CI's `default` matrix suite is `pytest tests/ -n auto` (`.github/workflows/tests.yml:25`) | Expressed as `aq test tests/ --aq-all-markers` once, at the exit gate only, plus the three CI matrix suites. §11 gives the exact commands |
| Verification: "`npm test -- --run`" | `dashboard/package.json` `test` is already `vitest run` | `npm test` from `dashboard/` |

### 2.1 Two naming reconciliations

- **`src/playbooks/cutover.py` vs `src/playbooks/migration.py`.** Package 6 owns `migration.py` (inventory and readiness — read-only, no schema work). Package 7's module is operational state: admission, drain, switch, window. Two different lifetimes and two different blast radii, so two modules. `cutover.py` **imports** `migration.py` (for `MigrationInventory.blocking()` and the cutover report) and never the reverse; §5.1 T-1 asserts the one-way dependency with an import test, because a cycle here would make the readiness report depend on operational state and vice versa.
- **"Rollback window" vs "rollback boundary".** The roadmap uses both. In this plan, **window** is the operational period during which `playbooks.runtime` can be flipped back to `v1`; **boundary** is the roadmap's per-package statement about what a revert costs. §13 keeps them separate.

---

## 3. Locked interfaces

Roadmap §7 says "Package 7 is intentionally serial and operator-led". That constrains *ordering*, not *specification*: the commits below are still written by different tasks at different times, and §3.9's human gates sit between them, so the shapes they exchange have to be fixed in advance or the operator gate in the middle becomes a design meeting. Everything in §3 is **locked**: a task may add fields, and must not rename or contradict.

### 3.1 Task map and where parallelism is allowed

| Commit | Task group | May start when | Serial because |
|---|---|---|---|
| 1 `ops: block new v1 runs and expose drain controls` | **A — Drain** | Package 6 exit gate | Nothing else may start: the switch needs the drain's zero |
| 2 `feat: switch all playbook entry points to v2` | **B — Switch** | **Gate G1** signed (§3.9) | The switch is the release |
| 3 `ops: complete playbook v2 rollback observation window` | **C — Window**, **D4 — routing-admission port** | Commit 2 deployed | Wall-clock and operator-owned. D4 lands here, not in commit 4 (§3.7) |
| 4 `refactor: remove v1 playbook execution runtime` | **D1 — src deletion**, **D2 — test disposition**, **D3 — dashboard** | **Gate G3** signed (§3.9) | D1/D2/D3 may run as three parallel branches merged in one PR |
| 5 `docs: declare playbook v2 the sole runtime` | **E — Docs** | Commit 4 | — |

D1, D2 and D3 are the only genuinely parallel lane in this package, and only because §3.6's removal manifest is exhaustive: each branch owns a disjoint file set and a disjoint verification command. D4 is deliberately **not** in that lane: it is a behaviour-preserving port of task-admission policy (§3.7), and doing it while V1 still exists means a regression is recoverable by the rollback switch instead of by a revert.

### 3.2 Drain types — `src/playbooks/cutover.py`

```python
V1RunOwnership = Literal["live", "orphaned"]
#: "live"     — run_id is in PlaybookManager.running_runs(); a coroutine owns it.
#: "orphaned" — the row says running/paused but no coroutine does; the process
#:              that started it is gone (§1.1).  Only a terminal write clears it.

V1RunOption = Literal["wait", "resolve", "cancel"]
#: "wait"    — offered only for `live`; the operator lets it finish.
#: "resolve" — offered only for `paused`; routes to the existing
#:             `resume_playbook` command with the human's input.
#: "cancel"  — offered always; `playbook_v1_run_cancel` (§3.3).

@dataclass(frozen=True, slots=True)
class V1RunSummary:
    run_id: str
    playbook_id: str
    playbook_version: int
    status: str                      # "running" | "paused" (the two non-terminal states)
    current_node: str | None
    started_at: float
    age_seconds: float               # now - started_at, at snapshot time
    paused_at: float | None
    waiting_for_event: str | None
    event_id: str | None
    project_id: str | None           # parsed from trigger_event JSON; None when absent
    ownership: V1RunOwnership
    options: tuple[V1RunOption, ...] # never empty; "cancel" is always present

@dataclass(frozen=True, slots=True)
class DrainStatus:
    generated_at: float
    admission: Literal["open", "closed"]     # §3.4
    closed_at: float | None
    closed_by: str | None
    active: tuple[V1RunSummary, ...]         # sorted by started_at ascending
    live_count: int
    orphaned_count: int
    oldest_age_seconds: float | None
    drained: bool                            # admission == "closed" and not active

    def to_dict(self) -> dict: ...           # stable key order; API DTO and CLI both render this
```

Entry point, read-only:

```python
async def drain_status(
    *,
    db,                                   # DatabaseBackend
    manager,                              # PlaybookManager | None; None -> every active row is "orphaned"
    config,                               # AppConfig, for `admission`
    clock: Callable[[], float] = time.time,
) -> DrainStatus: ...
```

**`manager=None` means orphaned, not unknown.** A caller with no manager (a CLI against a stopped daemon, a test) cannot prove a coroutine exists, and the safe answer for a cutover gate is the one that requires an operator decision. The opposite default would let `drained` go true because nobody was looking.

**Ownership is computed once per snapshot**, from `set(manager.running_runs())` (`src/playbooks/manager.py:626`), not per row — otherwise a run completing mid-scan can appear in neither set.

### 3.3 Command surface (locked names)

New mixin `src/commands/playbook_cutover_commands.py`, mixed into `CommandHandler`'s bases (`src/commands/handler.py:311`) immediately after `PlaybookCommandsMixin` (`:322`). Tool category `playbooks` in `src/tools/definitions.py:_TOOL_CATEGORIES`, which gives each a kebab-case CLI form under `aq playbook`. Every command returns the repo's `{"success": bool, ...}` dict and `{"success": False, "error": "..."}` on bad input.

| Command | CLI | Writes? | Paused by `playbooks.enabled=false`? | Purpose |
|---|---|---|---|---|
| `playbook_v1_drain_status` | `aq playbook v1-drain-status [--json]` | no | **no** | `DrainStatus.to_dict()` |
| `playbook_v1_admission_close` | `aq playbook v1-admission-close --reason TEXT` | yes | **no** | Sets `playbooks.v1_admission: closed`; appends a `v1_admission_closed` cutover event |
| `playbook_v1_admission_open` | `aq playbook v1-admission-open --reason TEXT` | yes | **no** | The inverse; only legal while `playbooks.runtime == "v1"` |
| `playbook_v1_run_cancel` | `aq playbook v1-run-cancel --run-id ID --reason TEXT` | yes | **no** | A cancel that actually cancels (§3.3.1) |
| `playbook_cutover_switch` | `aq playbook cutover-switch --to v1\|v2 --reason TEXT` | yes | **no** | Sets `playbooks.runtime`; appends `switched_to_v2` / `rolled_back_to_v1` |
| `playbook_cutover_window_status` | `aq playbook cutover-window-status [--json]` | no | **no** | §3.5's acceptance table, measured (§10) |
| `playbook_cutover_window_close` | `aq playbook cutover-window-close --reason TEXT` | yes | **no** | Refuses unless every §3.5 window-close gate passes; appends `rollback_window_closed` |

**None of the seven is added to `PAUSED_PLAYBOOK_COMMANDS`** (`src/commands/handler.py:166-193`). This is deliberate and is the one place this package widens a surface: `playbooks.enabled` defaults to `False` (§1.3), and a fleet that paused the subsystem with runs still in `running` must still be able to see and clear them. Draining is exactly the operation you need when the subsystem is off. §5.1 T-4 asserts all seven answer with `playbooks.enabled=false`.

**All seven are operator-only.** Required AQ capability `aq_commands: playbook_v1_*` / `playbook_cutover_*`; none may appear in any shipped agent profile's capability set (`src/profiles/defaults/`). §5.1 T-6 asserts that, mirroring Package 6 §3.6's T-3.

#### 3.3.1 `playbook_v1_run_cancel` — the ordering that makes it a real cancel

```
1. row = await db.get_playbook_run(run_id)          # 404 -> error
2. if row.status in TERMINAL_STATUSES: error        # src/playbooks/state_machine.py
3. task = manager._running.get(run_id)              # None for `orphaned`
4. if task is not None:
       task.cancel()
       await asyncio.wait_for(
           asyncio.gather(task, return_exceptions=True), timeout=CANCEL_JOIN_TIMEOUT
       )                                            # CANCEL_JOIN_TIMEOUT = 30.0
       # TimeoutError -> return success=False, error="run did not stop within 30s",
       # leaving the row untouched.  A half-cancelled run must not be reported drained.
5. await db.update_playbook_run(run_id, status="cancelled",
                                completed_at=now, error=f"cancelled during v1 drain: {reason}")
6. await sync_playbook_run_task(...); emit PlaybookRunCancelledEvent
```

Step 4 **before** step 5 is the whole fix for §1.2: the coroutine is gone before the terminal row is written, so there is no later persistence write to overwrite it. §5.1 T-3 is the test — it starts a run that writes its status in a `finally`, cancels it, and asserts the row is still `cancelled` after the task has been awaited. That test fails against today's `_cmd_cancel_playbook_run`.

`_cmd_cancel_playbook_run` is **not** modified: it stays as the general-purpose run cancel, and commit 4 replaces its body with a call into the V2 engine's cancellation. `playbook_v1_run_cancel` is a drain-scoped command that is deleted in commit 4 together with the rest of the drain surface (§3.6).

### 3.4 The switch — one selector, six call sites

```python
# src/playbooks/cutover.py
PlaybookRuntime = Literal["v1", "v2"]

def playbook_runtime(config) -> PlaybookRuntime:
    """The runtime every entry point must consult.  Read per dispatch, never cached."""

def v1_admission_closed(config) -> bool:
    """True when new V1 runs are refused.  Independent of `playbook_runtime`."""
```

Two config fields on the existing `PlaybooksConfig` (`src/config.py:857`):

```python
runtime: str = "v1"            # "v1" | "v2"        — flipped by the operator at G2
v1_admission: str = "open"     # "open" | "closed"  — flipped by the operator at G1
```

`PlaybooksConfig.validate()` rejects any other value **and** rejects the one incoherent pair: `runtime="v2"` with `v1_admission="open"`. Once V2 owns dispatch, "V1 admission open" describes nothing and would let a rollback silently start new V1 runs against artifacts nobody reviewed.

Each of §1.4's six sites gets the same two-line preamble, at the top of the function, before any V1 import:

```python
if playbook_runtime(self.config) == "v2":
    return await <v2 path>(...)
if v1_admission_closed(self.config):
    logger.info("v1 admission closed — refusing %s for playbook '%s'", <what>, playbook_id)
    return <the site's "did nothing" value>
```

Three properties this shape buys, each with a test in §5.2:

- **One decision point per site.** `_on_playbook_trigger` reads the selector once at `src/orchestrator/core.py:800`, above the `kind == "pipeline"` fork — so its two branches (§1.4 rows 1–2) can never disagree.
- **Admission and runtime are independent.** Draining happens while `runtime == "v1"`. Rollback flips `runtime` back without reopening admission, which is what "do not permit new V1 authoring during the window" means operationally.
- **Resume is asymmetric.** Sites 4 and 5 resume *existing* runs. A V1 run that is still paused when the switch happens must still be resumable — otherwise the switch strands it and `drained` was a lie. So sites 4 and 5 branch on **the run's own artifact**, not on the global selector: a row with a V2 artifact hash resumes through `PlaybookEngine.resume`, a row without one resumes through V1 **regardless of `runtime`**. §5.2 T-8 pins it. This is the single exception to "one coordinated switch", and it exists because the roadmap forbids translating an in-flight V1 run into V2 state.

### 3.5 Cutover acceptance thresholds (roadmap §10)

Roadmap §10 requires this package to define concrete thresholds. Two honest constraints shape them: there is no production V2 baseline, and the fleet may be idle (§1.3). So every latency gate is anchored to a **V1 baseline recorded during the drain** (commit 1, T-5) rather than to a number invented here, and every correctness gate is absolute.

`playbook_cutover_window_status` measures each row and returns `{measure, source, observed, gate, pass}`.

| # | Measure | Source | Gate at switch (G2) | Gate at window close (G3) |
|---:|---|---|---|---|
| 1 | Shadow rule-selection agreement | Package 6 `tests/fixtures/playbooks/v2/parity-report.json` | `unexplained == 0` over the whole corpus | unchanged; report re-run and re-committed |
| 2 | Command-argument agreement after canonicalisation | same | `unexplained == 0` | unchanged |
| 3 | Unexplained terminal-outcome differences | same | `0` | `0` new during the window |
| 4 | Authorization denials by command and profile | Package 0 `capability.denied` counter, grouped | `0` for any enabled playbook | `0` over the window |
| 5 | Duplicate receipt / snapshot-version conflicts | Package 3 `RunRepository.commit_boundary` conflict counter | `0` in rehearsal | `≤ 1 per 10,000 boundaries`, each with a written explanation |
| 6 | Event→run dispatch latency, p95 | `playbook.dispatch_ms` (§10) | `≤ 2 × v1_baseline.dispatch_p95` **and** `≤ 2000 ms` | `≤ 1.25 ×` baseline **and** `≤ 1000 ms` |
| 7 | Wait-resume latency, p95 | `playbook.resume_ms` (§10) | `≤ 5000 ms` from the causing event | `≤ 5000 ms` |
| 8 | LLM budget failures | receipts with outcome `budget_exceeded` | `≤ 2 %` of LLM steps | `≤ 1 %` |
| 9 | Structured-output failures | receipts with outcome `output_invalid` | `≤ 2 %` of LLM steps | `≤ 1 %` |
| 10 | Agent-task orphan rate | agent-task steps with no terminal receipt after 2 × the step timeout | `0` | `0` |
| 11 | Agent-task cancellation rate | receipts with outcome `cancelled` | reported, no gate | reported, no gate |
| 12 | Graph API latency, p95 | `playbook_v2_graph` against the largest enabled artifact | `≤ 300 ms` | `≤ 300 ms` |
| 13 | Dashboard semantic-tab time-to-interactive | manual scenario review (§11) | `≤ 1500 ms` | — |
| 14 | Pending-event count | `playbook_pending_events` | `0` | `≤ 5` |
| 15 | Pending-event maximum age | same | `< 1 h` | `< 24 h` |
| 16 | Active V1 runs | `DrainStatus.active` | `0` — hard | `0` |

**Observation window.** `playbook_cutover_window_close` refuses until **all three** hold:

- **Wall clock:** ≥ 72 h since the `switched_to_v2` cutover event.
- **Coverage:** every enabled playbook has dispatched ≥ 1 V2 run since the switch. On an idle fleet this is satisfied by the §5.3 T-11 rehearsal, which dispatches one synthetic event per enabled playbook against the live daemon and is recorded as a `window_coverage_rehearsal` cutover event — the rehearsal is named in the close reason, so a window closed on synthetic traffic says so.
- **Volume:** ≥ 200 V2 runs total, rehearsal runs included.

Wall clock alone is refused because an idle fleet reaches 72 h having proved nothing; coverage alone is refused because one run per playbook does not exercise retry, wait, or budget paths. `--force` does not exist on `playbook_cutover_window_close`: an operator who wants to close early edits `playbooks.runtime` themselves and owns it, and the cutover-events table records that they did not use the gate.

### 3.6 Removal manifest (locked)

Commit 4 deletes exactly this and nothing else. Anything discovered mid-commit that is not on the list gets a row added in the same commit, with a sentence saying why.

**D1 — `src/` deletions**

| Path | Note |
|---|---|
| `src/playbooks/pipeline_compiler.py` | Requires editing the two lazy imports at `src/playbooks/compiler.py:109`, `:157` in the same commit |
| `src/playbooks/pipeline_runner.py` | — |
| `src/playbooks/runner.py` | 93 KB, the bulk of V1 |
| `src/playbooks/runner_context.py` | `_parse_json_from_text` moves out first — see D1-b |
| `src/playbooks/runner_events.py` | The `PlaybookRun*Event` classes it emits live in `src/notifications/events.py` and **survive**; V2 emits the same event names so dashboard WS subscribers are unaffected |
| `src/playbooks/runner_transitions.py` | — |
| `src/playbooks/token_tracker.py` | Only importers are `runner.py:49` and `runner_transitions.py:30`; both die with it |
| `src/playbooks/conditions.py` | Only importers are `routing.py:11` and `orchestrator/core.py:119`; both are edited in D4 / D1-c |
| `src/playbooks/assignment_compiler.py` | Only importers are `compiler.py:114`, `:164`. **Conditional:** delete only if Package 2's compiler has replaced `compiler.py`'s `kind` dispatch. If `compiler.py` survives with a V1 `kind: assignment-routing` branch, that branch is what gets deleted and this file with it; if it does not survive, the file is already gone. Reconcile in §3.8 |

**D1-b — moves, not deletions**

| Symbol | From | To | Why |
|---|---|---|---|
| `_parse_json_from_text` | `src/playbooks/runner_context.py` (re-exported by `runner.py:39`, imported by `src/orchestrator/assignment_routing.py:42`) | `src/playbooks/expressions.py` (Package 2), renamed `parse_json_from_text` | Pure parser used by `validate_assignment_response` (`assignment_routing.py:85`), which survives cutover. Deleting it breaks assignment-response validation |

**D1-c — edits inside surviving files**

| File | Edit |
|---|---|
| `src/playbooks/__init__.py` | Drop the `PlaybookRunner` import (`:11`) and its `__all__` entry (`:31`). Leaving it is an `ImportError` on every `import src.playbooks` |
| `src/playbooks/routing.py` | Delete `is_deprecated_default_assignment_entry` (`:20`) and `_DEPRECATED_DEFAULT_ASSIGNMENT_RULES` (`:14`) — they describe cached V1 rule entries. Rewrite `_selected_pipelines` / `_rule_actions` per D4 |
| `src/orchestrator/core.py` | Delete the whole `kind == "pipeline"` branch (`:837-1050`) and the LLM branch's V1 tail (`:1052-1110`); delete the `eval_pipeline_when` import (`:119`) and the `is_deprecated_default_assignment_entry` import (`:894`) |
| `src/orchestrator/assignment_routing.py` | Repoint the `:42` import at D1-b's new home |
| `src/playbooks/state_machine.py` | Survives. Update the `:140` docstring reference to `PlaybookRunner` |
| `src/playbooks/services.py`, `src/playbooks/run_task.py` | Survive unchanged; both are used by non-V1 callers (`orchestrator/core.py:536`, `:120`) |
| `src/commands/playbook_commands.py` | Delete the six V1 call sites' V1 arms, leaving the V2 arms commit 2 added; delete `check_paused_playbook_timeouts`'s V1 timeout path (`:1794-1830`) |
| `src/commands/handler.py` | Remove drain commands from any list commit 1 added; leave `PAUSED_PLAYBOOK_COMMANDS` otherwise intact |
| `src/config.py` | Delete `PlaybooksConfig.runtime`, `PlaybooksConfig.v1_admission`, `PlaybooksConfig.v2_api`, `PlaybooksConfig.v2_activation_writes` (Package 5 §8); delete `SecurityConfig.capability_enforcement`'s `off`/`audit` modes, keeping enforcement unconditional (Package 0 §3.6) |
| `src/profiles/capabilities.py` | Delete `CapabilityPolicy.derived_from_legacy` and the legacy `## Tools` adapter (Package 0 §3.6) |
| `src/profiles/parser.py` | Delete `## Tools` block parsing; `## Capabilities` becomes the only shape |
| `src/metrics/sampler.py` | Keep the counters §10 adds; delete nothing |

**D2 — test dispositions.** Every one of the 30 files, no "update related tests":

| File | Tests | Disposition |
|---|---:|---|
| `tests/test_playbook_runner.py` | 385 | **Delete.** It tests `PlaybookRunner` internals. Its *behavioural* coverage is Package 4's executor suites' job; D2 opens a checklist issue enumerating any behaviour it pins that Package 4 does not, filed before deletion (§14) |
| `tests/test_pipeline_runner.py` | 12 | Delete |
| `tests/test_pipeline_compiler.py` | 16 | Delete |
| `tests/test_pipeline_dispatch.py` | 3 | Delete |
| `tests/test_pipeline_when_comparators.py` | 10 | Delete with `conditions.py` |
| `tests/test_playbook_composition.py` | 29 | Delete |
| `tests/test_playbook_node_trace_fields.py` | 6 | Delete |
| `tests/test_playbook_run_bus_events.py` | 3 | **Port.** Event names survive; retarget at the V2 engine |
| `tests/test_playbook_run_events.py` | 35 | **Port.** Same reason |
| `tests/test_playbook_state_machine.py` | 51 | **Keep.** `state_machine.py` survives; strip only the V1 runner imports |
| `tests/test_playbook_paused_notification.py` | 21 | **Port** to V2 wait steps |
| `tests/test_playbook_resume_handler.py` | 21 | **Port**; keep the paused-V1-run cases as historical-read tests (§3.4's resume asymmetry) |
| `tests/test_workflow_stage_resume.py` | 39 | **Port**; same |
| `tests/test_human_in_the_loop.py` | 36 | **Port** to V2 wait + gate steps |
| `tests/test_dry_run_playbook.py` | 27 | **Port** to `ExecutionMode.dry_run` |
| `tests/test_playbook_health.py` | 46 | **Keep**; `health.py` is run metrics and survives |
| `tests/test_playbook_commands.py` | 74 | **Keep**; one V1 reference to strip |
| `tests/test_playbook_commands_enabled_paths.py` | 88 | **Port**; it is the `playbooks.enabled` gate suite |
| `tests/test_reflection_e2e.py` | 48 | **Port** — reflection is a playbook |
| `tests/test_reflection_stale_contradiction.py` | 43 | **Port** |
| `tests/test_triage_lifecycle.py` | 21 | **Port**; depends on D4 |
| `tests/test_routing_admission.py` | 16 | **Port**; the D4 ratchet |
| `tests/test_graph_routing_admission.py` | 2 | **Port**; the D4 ratchet |
| `tests/test_review_pipeline_rules.py` | 9 | **Port** to the reviewed artifact |
| `tests/test_default_pipeline.py` | 4 | **Port** |
| `tests/test_default_pipeline_spec_and_proposal.py` | 5 | **Port** |
| `tests/test_control_plane_e2e.py` | 2 | **Port** |
| `tests/test_orphan_workflow_scenarios.py` | 37 | **Keep**; one incidental reference |
| `tests/test_database_modular.py` | 132 | **Keep**; `playbook_runs` rows stay readable |
| `tests/conftest.py` | — | Delete `DEFAULT_PIPELINE_PATH` (`:237`) and `PipelineEngine` (`:322`) with the parity V1 arm |

Plus, from Package 6 §14: delete `tests/fixtures/playbooks/v1/default-pipeline.md` and the V1 arm of `tests/test_playbook_shadow_parity.py`. That fixture is the **last executable V1 graph in the tree**; §5.4 T-14's repository scan asserts it is gone rather than merely unreferenced.

**D3 — dashboard.** Per Package 5 §6.5: delete `dashboard/src/pages/playbook-graph/` (`PlaybookGraphCanvas.tsx`, `PlaybookGraphView.tsx`, `PlaybookNodeInspector.tsx`, `PlaybookStepNode.tsx`, `layout.ts`, `types.ts`, `__tests__/`); delete the `graph` tab from `dashboard/src/pages/PlaybookDetail.tsx:20` and rename the `semantic` tab id to `graph`; delete the `playbook_graph_view` hook and its `RESPONSE_EXCLUDE_NONE` entry (`src/api/codegen.py:70`) together with `src/playbooks/graph_view.py`, `src/playbooks/graph.py`, `_cmd_show_playbook_graph` (`playbook_commands.py:788`) and `_cmd_playbook_graph_view` (`:1158`).

**D4 — routing admission.** Not a deletion. See §3.7; it lands in commit 3, before D1 removes the graph it currently reads.

### 3.7 Routing admission must be ported before V1 is deleted

`requires_routing_gate` runs *inside the task-creation transaction* (`src/task_graph/creator.py:388-395`, `src/commands/task_commands.py:1476-1485`) and decides whether an unrouted task gets a `routing` gate. It answers by walking the V1 compiled graph (`src/playbooks/routing.py:67-95`). `uses_default_triage` (`:127`) does the same for triage recovery (`src/orchestrator/triage.py:24`). Neither is playbook *execution*; both are admission policy that happens to read the execution artifact.

The port keeps both signatures and both call sites byte-identical and replaces only the walk:

```python
# src/playbooks/routing.py, after D4
def requires_routing_gate(manager, task, event_extra=None) -> bool: ...   # unchanged signature
def uses_default_triage(manager, project_id: str) -> bool: ...            # unchanged signature

# New private helper, replacing _selected_pipelines + _rule_actions:
def _artifact_command_effects(manager, event, *, match_filter=True) -> Iterator[CommandEffect]:
    """Yield the command effects an enabled V2 activation would execute for `event`.

    Reads the *active artifact* through `ArtifactStore.load` and the compiled
    rule guards through the V2 condition evaluator.  It does not dispatch, does
    not create a run, and must not touch `PlaybookEngine`: this runs inside an
    open write transaction, so anything that takes a second connection or awaits
    an executor deadlocks task creation.
    """
```

Three constraints, each with a test in §5.3:

- **No engine, no second connection.** T-9's failing assertion: a fake `ArtifactStore` that records calls, and a `Database` wrapper that raises if a second connection is opened during `requires_routing_gate`.
- **Same answers.** T-10 replays `tests/test_routing_admission.py`'s 16 cases and `tests/test_graph_routing_admission.py`'s 2 against the reviewed `default-pipeline` V2 artifact and asserts the identical boolean per case. That is what makes the port provable rather than plausible.
- **Fail closed on a missing artifact.** If no activation exists or the artifact fails to load, `requires_routing_gate` returns **`True`** (attach the gate) and `uses_default_triage` returns **`False`** (do not invent triage). A spurious routing gate is a human deciding a routing question that did not need deciding; a missing one is an unrouted task running silently. The existing code already reasons this way at `src/commands/task_commands.py:1477-1480`'s except-branch comment; the port keeps that asymmetry explicit.

### 3.8 Symbols this package imports from Packages 0–6

**Reconciliation is the implementation task's first commit.** If a symbol below is named differently in the live tree when Package 7 starts, amend this table and every reference in §5 in the same commit; do not silently substitute, and do not ship a local reimplementation of an earlier package's interface.

**Observed — Package 0, already in `origin/main`.** These are live line numbers, not expectations.

| Symbol | Live location | Package 7 uses it for |
|---|---|---|
| `CapabilityPolicy`, `DENY_ALL`, `classify_capability`, `capability_policy_for` | `src/profiles/capabilities.py:123`, `:217`, `:75`, `:231` | Operator-only capability on the seven §3.3 commands |
| `CapabilityPolicy.derived_from_legacy` | `src/profiles/capabilities.py:138` — its own comment already says "**Removal package: Package 7**" | D1-c removal |
| The legacy adapter (`capability_policy_for`, `src/profiles/capabilities.py:231-317`, and its `AGENT_COMMAND_FALLBACK` at `:224`) | `src/profiles/capabilities.py:224`, `:231-317` | D1-c removal |
| `## Tools` parsing and `## Capabilities` validation | `src/profiles/parser.py:143`, `:165`, `:688-710` | D1-c removes `## Tools`; `## Capabilities` becomes the only shape |
| `security.capability_enforcement`, `CAPABILITY_ENFORCEMENT_MODES` | `src/config.py:1137`, `:1108` | D1-c removes `off`/`audit`; enforcement becomes unconditional |
| `ExecutionPrincipal`, `principal_context`, `check_delegation` | `src/commands/principal.py:82`, `:162`, `:179` | Operator-only authorization |
| `authorize_command`, `AuthzDecision`, `denial_result` | `src/commands/authorization.py:132`, `:89`, `:184` | Same |
| Denial logging at dispatch | `src/commands/handler.py:900-923` | §10 turns it into a bus event + counter for acceptance measure 4 |
| Alembic revision `3b560dbd527c` (`profile_capability_namespaces`) | `migrations/versions/3b560dbd527c_profile_capability_namespaces.py` | §6's revision chains after the then-current head, not this one specifically |

**Expected — Packages 1–6, not yet in the tree.**

| Symbol | Owning package | Module the roadmap assigns | Package 7 uses it for |
|---|---|---|---|
| `CommandContractRegistry` registry-wide fingerprint | 1 | `src/commands/contracts/registry.py` | `playbook_cutover_window_status` staleness check |
| `PlaybookDefinition`, condition evaluator | 2 | `src/playbooks/definition.py`, `expressions.py` | D4's artifact walk; D1-b's `parse_json_from_text` home |
| `ArtifactRef`, `ArtifactStore.load` | 3 | `src/playbooks/artifact_store.py` | D4; window status |
| `ActivationHealth`, activation repository | 3 | `src/playbooks/activation.py` | Switch precondition; window status |
| `playbook_pending_events` table + queries | 3 | `src/database/queries/playbook_run_queries.py` | Acceptance measures 14–15 |
| `RunRepository.commit_boundary` conflict counter | 3 | `src/playbooks/run_state.py` | Acceptance measure 5 |
| Receipt outcomes `budget_exceeded`, `output_invalid`, `cancelled` | 3, 4 | `src/playbooks/receipts.py` | Acceptance measures 8–11 |
| `PlaybookEngine.dispatch_event` / `run_rule` / `resume`, `ExecutionMode` | 4 | `src/playbooks/engine.py` | The V2 arm of all six entry points |
| `playbook_v2_graph`, `playbook_activation_health`, `playbook_run_overlay` | 5 | `src/commands/playbook_v2_commands.py` | Acceptance measure 12; §5.4 dashboard rename |
| `playbooks.v2_api`, `playbooks.v2_activation_writes` | 5 | `src/config.py::PlaybooksConfig` | D1-c removal |
| `MigrationInventory.blocking()`, `playbook_cutover_report` | 6 | `src/playbooks/migration.py`, `src/commands/playbook_migration_commands.py` | G1 precondition; window status |
| `parity-report.json`, `EXPECTED_DIFFERENCES` | 6 | `tests/fixtures/playbooks/v2/` | Acceptance measures 1–3 |
| `playbooks.v2.migration.shadow_compare`, shadow-parity V1 arm | 6 | — | D2 removal (Package 6 §14) |

### 3.9 Human coordination points

Roadmap §7 makes this package operator-led. Three gates, each a real `gate_create` row (`src/commands/gate_commands.py:20`, `gate_type="human"`, `GATE_TYPES` at `src/database/tables.py:248`) so the pause is visible in the same place as every other human gate, and each recorded as a cutover event (§6).

| Gate | Sits before | Who | Preconditions the command refuses without | Recorded as |
|---|---|---|---|---|
| **G1 — Drain sign-off** | Commit 2 (the switch commit) | The named release operator | `playbook_cutover_report --json` → `blocking_reasons: []` **and** `rollback_ready: true` (Package 6 §13); `playbook_v1_drain_status` → `drained: true`, `active: []`; every enabled playbook has an activation with `ActivationHealth.ready` and the current contract fingerprint; pending events `0` | `drain_completed` |
| **G2 — Switch authorization** | The deploy of commit 2 | Two people: the change author and the release operator, distinct | G1 signed; `playbook_cutover_switch --to v2` refuses unless G1's gate row is `resolved`. The **commit** only makes V2 reachable; the **operator** flips `playbooks.runtime` | `switched_to_v2` |
| **G3 — Rollback-window closure** | Commit 4 (the deletion commit) | The named release operator | `playbook_cutover_window_close` refuses unless every §3.5 window-close gate passes and all three observation-window conditions hold | `rollback_window_closed` |

Two rules that make the gates load-bearing rather than ceremonial:

- **No command in this package writes a cutover event for a gate it did not verify.** `playbook_cutover_window_close` recomputes §3.5 itself; it does not read a cached verdict. §5.3 T-12's failing assertion is a window-close call with one measure over its gate, expected to be refused with that measure named.
- **G2 is the only gate a human can bypass**, by editing `~/.agent-queue/config.yaml` directly. That is intentional — an operator must be able to roll back at 3am without a gate row — and it is why `playbook_cutover_switch` writes the cutover event *and* why `playbook_cutover_window_status` reads `playbooks.runtime` from live config rather than from the event log. A hand-edited rollback shows up as a runtime/event-log disagreement, which the window status reports as `blocking_reasons: ["runtime flipped outside the cutover command"]`.

---

## 4. Security analysis

Package 7 removes the last permissive paths in the system and adds one new class of privileged operation. Five boundaries change.

### 4.1 The drain surface is a privileged write that must work while the subsystem is paused

§3.3 exempts all seven commands from `PAUSED_PLAYBOOK_COMMANDS`. That is a real widening: with `playbooks.enabled=false`, `playbook_v1_run_cancel` can terminate runs and `playbook_cutover_switch` can change which runtime the whole fleet uses, on a daemon where every other playbook command refuses.

Compensations, all asserted in §5.1 T-6:

- Every one of the seven requires an operator capability (`aq_commands: playbook_v1_*` / `playbook_cutover_*`) that appears in **no** shipped profile under `src/profiles/defaults/`. An agent session cannot reach them even with a valid `ExecutionPrincipal`.
- Every one takes a mandatory `--reason` of ≥ 10 characters, stored verbatim in the cutover-events row. A drain with no stated reason is refused, not defaulted.
- Every write appends to `playbook_cutover_events` (§6) before returning success. The table is append-only: there is no delete command and no update path.

### 4.2 Cancel gains the power to stop work mid-flight

Today `_cmd_cancel_playbook_run` cannot interrupt a coroutine (§1.2). §3.3.1 gives `playbook_v1_run_cancel` `asyncio.Task.cancel()`. That is new authority over live execution, and cancellation lands at an arbitrary `await` — which for a V1 pipeline node can be *between* the command dispatch and the run-row update.

The mitigation is that V1 commands are dispatched through `CommandHandler.execute()`, which is the same idempotency boundary every other caller uses; a cancelled run's already-dispatched command has already committed and will not be re-dispatched, because the run is terminal. The residual risk — a run cancelled after `ensure_task` committed but before the row recorded it — is **accepted and documented**: the operator sees the created task and the cancelled run, which is the truthful picture. §5.1 T-3 asserts the terminal row wins; it does not assert the node was atomic, because it was never atomic.

### 4.3 The rollback switch is the highest-privilege operation in the package

`playbook_cutover_switch --to v1` re-enables an execution runtime that the fleet has decided to remove. Three constraints:

- It is legal **only while `playbook_cutover_events` has no `rollback_window_closed` row.** After G3, the command returns `{"success": False, "error": "rollback window closed at <ts>; rollback now requires a forward change"}` — matching the roadmap's rollback boundary exactly.
- It **never** touches artifacts, activations, or any V2 table. §5.2 T-7's assertion: a switch to `v1` and back leaves `ArtifactStore` bytes, activation rows and receipt rows byte-identical.
- It cannot reopen V1 admission. `runtime="v1"` with `v1_admission="closed"` is the *supported* rollback state: existing V1 runs resume, no new ones start. `PlaybooksConfig.validate()` (§3.4) rejects the reverse pair.

### 4.4 Removing `capability_enforcement`'s grace modes is a one-way privilege reduction

Package 0 shipped `security.capability_enforcement` with `off`/`audit`/`enforce` and `CapabilityPolicy.derived_from_legacy` so an un-migrated fleet would not be stranded. D1-c deletes the flag, the two grace modes, the `derived_from_legacy` field, the legacy adapter, and `## Tools` parsing.

The pre-condition is Package 6's fleet-readiness gate, which requires every shipped and project profile to carry an explicit `## Capabilities` block. §5.4 T-13's failing assertion is a profile fixture with only `## Tools`, expected to fail parsing with a message naming the migration — **not** to be silently adapted. A profile that still has only `## Tools` after this commit denies everything, which is the correct direction to fail, but it is a fleet outage if it is a surprise; that is why it is gated on G3 and why §5.5's docs commit updates the profile authoring guide.

### 4.5 What still reads authoring content after this package

The roadmap's final outcome is "Confirm no code path reads embedded JSON actions from Markdown." §5.4 T-14 is that confirmation, and it must cover four shapes, not one:

1. `compile_pipeline`'s fenced-JSON extraction (`src/playbooks/pipeline_compiler.py`) — deleted in D1.
2. `_rule_actions`'s walk of `node["action"]["command"]` (`src/playbooks/routing.py:67-95`) — replaced in D4.
3. `tests/fixtures/playbooks/v1/default-pipeline.md` (Package 6) — deleted in D2.
4. `src/prompts/example_playbooks/` and `src/prompts/default_rules/` — 16 files referenced by **no** Python code (Package 6 §2). They are inert sample content a user could still copy into a vault, where the V2 compiler would reject them. Package 6 documented them and explicitly did **not** carry their disposition; Package 7 does **not** delete them either, because deleting user-copyable examples is a product decision, not a cutover step. T-14's scan therefore allowlists these two directories **by path, with the allowlist asserted to be exactly those two entries** — so a new unscanned directory fails the test rather than joining the allowlist silently. §14 keeps the disposition open.

### 4.6 Residual risks accepted

- **A hand-edited config bypasses G2.** By design (§3.9). Detected, not prevented: `playbook_cutover_window_status` reports the runtime/event-log disagreement.
- **`drain_status` is a snapshot.** A run can start between the snapshot and the operator reading it. `v1_admission="closed"` is what actually prevents that, and G1 requires admission closed *before* the drain is read. The snapshot alone is never the gate.
- **Cancellation of a paused run resolves nothing upstream.** A paused V1 run cancelled during the drain leaves whatever asked the human still unanswered. The drain reports `waiting_for_event` and `current_node` per run so the operator can see what they are abandoning; it does not try to notify the waiter.

---

## 5. Tasks

Every task is red/green: the failing assertion is named before the implementation step that satisfies it. Per-task verification commands are in §11; the command listed under each task is the focused one to run while iterating.

### 5.1 Commit 1 — `ops: block new v1 runs and expose drain controls`

**T-1 — module boundary.** *Red:* `tests/test_playbook_cutover.py::test_cutover_does_not_import_engine_or_create_cycle` imports `src.playbooks.cutover` with `src.playbooks.migration` already imported and asserts (a) `cutover` imports `migration`, (b) `migration` does not import `cutover`, (c) `cutover` does not import `src.playbooks.engine` at module scope. Fails: the module does not exist. *Green:* create `src/playbooks/cutover.py` with §3.2's types and nothing else.

**T-2 — ownership classification.** *Red:* `test_drain_status_marks_unowned_running_rows_orphaned` inserts three `playbook_runs` rows (`running` with its id in `manager.running_runs()`, `running` without, `paused` without) and asserts `ownership == ("live", "orphaned", "orphaned")`, `live_count == 1`, `orphaned_count == 2`, and `drained is False`. A companion, `test_drain_status_without_manager_marks_everything_orphaned`, passes `manager=None` and asserts all three are `orphaned` — the §3.2 safe default. *Green:* implement `drain_status`.

**T-3 — a cancel that actually cancels.** *Red:* `test_v1_run_cancel_survives_the_runners_final_write` registers a coroutine in `manager._running` that, in a `finally`, calls `db.update_playbook_run(run_id, status="running")` — reproducing §1.2 — then calls `playbook_v1_run_cancel` and asserts the row reads `cancelled` **after** the task has finished. Against today's `_cmd_cancel_playbook_run` this fails with `status == "running"`. A second case, `test_v1_run_cancel_refuses_when_the_task_will_not_stop`, uses a coroutine that swallows `CancelledError` and asserts the command returns `success=False` with the row untouched. *Green:* implement §3.3.1's ordering with `CANCEL_JOIN_TIMEOUT = 30.0`.

**T-4 — the drain works on a paused subsystem.** *Red:* `test_drain_commands_answer_while_playbooks_disabled` builds a `CommandHandler` with `playbooks.enabled=False` and asserts each of the seven §3.3 commands returns something other than `PLAYBOOKS_PAUSED_ERROR` (`src/commands/handler.py:650`), while `list_playbooks` still returns it. *Green:* add the mixin; do **not** add the names to `PAUSED_PLAYBOOK_COMMANDS`.

**T-5 — admission close, the V1 baseline, and the drain rehearsal.** *Red:* `test_admission_close_refuses_without_reason` (reason shorter than 10 chars → `success=False`); `test_admission_close_blocks_new_v1_dispatch` closes admission and asserts `_on_playbook_trigger` creates no `playbook_runs` row and logs at INFO; `test_drain_rehearsal_reaches_zero` seeds two live and three orphaned runs, closes admission, cancels all five, and asserts `drained is True` with a `drain_completed` cutover event carrying the recorded V1 latency baseline (`dispatch_p95`, `resume_p95`, computed from the drained rows' `started_at`/`completed_at`). *Green:* implement `playbook_v1_admission_close`/`_open`, the §3.4 admission preamble at the six sites (runtime branch comes in commit 2), and the baseline record.

**T-6 — operator-only.** *Red:* `test_cutover_commands_are_in_no_shipped_profile` walks `src/profiles/defaults/*/profile.md`, parses each `## Capabilities` block, and asserts no `aq_commands` entry matches `playbook_v1_*` or `playbook_cutover_*`. *Green:* nothing to implement if true; the test is the ratchet against a later profile edit.

*Iterate with:* `aq test tests/test_playbook_cutover.py -x`

### 5.2 Commit 2 — `feat: switch all playbook entry points to v2` — **after Gate G1**

**T-7 — one selector, six sites, nothing else moves.** *Red:* `tests/test_playbook_cutover_switch.py::test_every_v1_entry_point_consults_the_selector` is a parametrised test over the six §1.4 sites. Each case sets `playbooks.runtime="v2"`, patches the V1 symbol the site imports (`PlaybookRunner`, `PipelineRunner`) with a `MagicMock` that raises on call, drives the site, and asserts the V2 engine was called and the V1 mock was not. Six parameters, six failures before the change. A second case, `test_switch_round_trip_leaves_v2_state_byte_identical` (§4.3), snapshots artifact bytes, activation rows and receipt rows, switches `v2 → v1 → v2`, and asserts equality. *Green:* add `playbook_runtime()`, the two config fields with `validate()`, and the §3.4 preamble at each site.

**T-8 — resume follows the run, not the switch.** *Red:* `test_paused_v1_run_resumes_through_v1_after_the_switch` creates a `paused` row with no V2 artifact hash, sets `runtime="v2"`, fires the resume cause, and asserts the V1 resume path ran and the run reached a terminal status. `test_paused_v2_run_resumes_through_the_engine_when_runtime_is_v1` is the mirror. Both fail against a naive global switch, which is the point: a global switch strands paused runs and makes `drained` retroactively false. *Green:* branch sites 4 and 5 on the run's artifact hash per §3.4.

*Iterate with:* `aq test tests/test_playbook_cutover_switch.py -x`

### 5.3 Commit 3 — `ops: complete playbook v2 rollback observation window` (and the routing-admission port)

**T-9 — routing admission reads the artifact, not the engine.** *Red:* `tests/test_routing_admission_v2.py::test_requires_routing_gate_opens_no_second_connection` calls `requires_routing_gate` inside an open write transaction with a `Database` wrapper that raises on a second connection and an `ArtifactStore` double that records `load` calls, and asserts exactly one `load` and zero extra connections. Fails today: `_selected_pipelines` walks the manager's in-memory compiled graphs and would pass trivially, so the test also asserts `ArtifactStore.load` was called *at all* — that assertion is what fails first. *Green:* implement `_artifact_command_effects` per §3.7.

**T-10 — the port gives the same answers.** *Red:* `test_routing_admission_parity_with_v1_cases` replays every case in `tests/test_routing_admission.py` (16) and `tests/test_graph_routing_admission.py` (2) against the reviewed `default-pipeline` V2 artifact from `tests/fixtures/playbooks/v2/default-pipeline/artifact.json` and asserts the identical boolean per case, by name. Two negative cases pin §3.7's fail-closed rule: no activation → `requires_routing_gate is True`, `uses_default_triage is False`; unloadable artifact → same. *Green:* finish the port; delete `is_deprecated_default_assignment_entry` only in commit 4 (D1-c).

**T-11 — window coverage rehearsal.** *Red:* `tests/test_playbook_cutover_window.py::test_window_close_refuses_without_coverage` sets the wall clock past 72 h and the volume past 200 but leaves one enabled playbook with zero V2 runs, and asserts refusal naming that playbook. `test_rehearsal_dispatches_one_event_per_enabled_playbook` runs the rehearsal against a live-ish daemon fixture and asserts one `window_coverage_rehearsal` cutover event listing every enabled playbook id. *Green:* implement `playbook_cutover_window_status` measurement (§10) and the rehearsal.

**T-12 — the close gate recomputes, it does not trust.** *Red:* `test_window_close_refuses_on_a_single_failing_measure` is parametrised over all sixteen §3.5 rows: for each, it plants a stored `window_status` result that says `pass: true` while the underlying source says otherwise, and asserts `playbook_cutover_window_close` refuses and names that measure. Sixteen parameters. *Green:* make `playbook_cutover_window_close` recompute from source; delete any caching.

*Iterate with:* `aq test tests/test_routing_admission_v2.py tests/test_playbook_cutover_window.py -x`

### 5.4 Commit 4 — `refactor: remove v1 playbook execution runtime` — **after Gate G3**

Three parallel branches (§3.1), one PR.

**T-13 — D1: the `src/` deletion.** *Red:* `tests/test_v1_removal.py::test_v1_execution_modules_are_gone` asserts `importlib.util.find_spec` returns `None` for each D1 path (nine at drafting; `assignment_compiler.py` is conditional per §3.6, and the test reads the manifest rather than a hardcoded list), and `test_playbooks_package_imports_without_the_runner` does `import src.playbooks` and asserts `"PlaybookRunner" not in src.playbooks.__all__`. `test_legacy_tools_block_is_rejected` loads a profile fixture with only `## Tools` and asserts a parse error naming `## Capabilities` (§4.4). *Green:* execute D1, D1-b and D1-c exactly as §3.6 lists them.

**T-14 — D1: the repository scan.** *Red:* `test_no_module_imports_a_v1_execution_module` walks every `.py` under `src/`, `tests/` and `scripts/` with `ast.parse` (not `grep` — a string in a docstring is not an import) and asserts no `Import`/`ImportFrom` names any D1 module. `test_no_code_path_parses_embedded_json_actions` covers §4.5's four shapes: it asserts the two source shapes are gone, the V1 parity fixture is gone, and the example/rules allowlist is **exactly** `{"src/prompts/example_playbooks", "src/prompts/default_rules"}`. *Green:* fix what the scan finds; add manifest rows for anything not already listed.

**T-15 — D2: historical V1 runs stay readable.** *Red:* `test_historical_v1_run_is_still_inspectable` inserts a completed `playbook_runs` row with a V1 `pinned_graph` and no artifact hash, then calls `list_playbook_runs`, `inspect_playbook_run` and the run-list API DTO, asserting each returns the row with its node trace and conversation history intact and **no** attempt to load an artifact. *Green:* keep `src/api/models/playbook.py`'s existing DTOs and `_format_playbook_run_summary` (`playbook_commands.py:177`); make `inspect_playbook_run` tolerate a missing artifact hash rather than requiring one.

**T-16 — D3: the dashboard rename.** *Red:* `dashboard/src/pages/__tests__/PlaybookDetail.test.tsx::renders exactly three tabs` asserts the tab ids are `["source", "graph", "runs"]` and that the `graph` tab renders the semantic canvas (a `data-testid` only `PlaybookSemanticGraphView` emits). Fails while four tabs exist. *Green:* execute D3.

*Iterate with:* `aq test tests/test_v1_removal.py -x` and, from `dashboard/`, `npm test -- --run src/pages/__tests__/PlaybookDetail.test.tsx`

### 5.5 Commit 5 — `docs: declare playbook v2 the sole runtime`

**T-17 — the docs say one runtime exists.** Update, in one commit:

| File | Edit |
|---|---|
| `docs/specs/design/playbooks.md` | Replace the `class PlaybookRunner` execution-model section (`:369`) with the V2 engine and executors; keep a short "Historical V1 runs" subsection describing the read-only path |
| `docs/specs/design/agent-coordination.md:343` | Repoint the `[[playbooks#6. Execution Model\|PlaybookRunner]]` wikilink at the engine section |
| `profile.md:34` | Replace the `PlaybookRunner — graph walker with conversation history` codebase-map line |
| `CLAUDE.md` | Quick Reference "Playbooks:" line lists `runner` — replace the module list with the V2 set; add the `playbooks.runtime` removal to the config notes |
| `docs/guides/architecture.md` | Playbook subsystem paragraph |
| New: `docs/guides/playbook-v2-cutover-runbook.md` | The operator runbook: the seven §3.3 commands, the three §3.9 gates, the §3.5 table, and the rollback procedure with its expiry |

**T-18 — the docs stay true.** *Red:* `tests/test_docs_reference_live_symbols.py::test_playbook_docs_name_no_deleted_module` greps the six files above for every D1 module name and the string `PlaybookRunner`, allowing them **only** inside a section headed `## Historical V1 runs`. Fails before T-17. *Green:* land T-17.

*Iterate with:* `aq test tests/test_docs_reference_live_symbols.py -x`

---

## 6. Storage — Alembic

One additive revision, in commit 1. `src/database/tables.py` is edited in the same commit (CLAUDE.md: "Never edit `tables.py` without generating a migration").

```python
# src/database/tables.py
CUTOVER_EVENT_KINDS = (
    "v1_admission_closed",
    "v1_admission_reopened",
    "drain_completed",
    "switched_to_v2",
    "rolled_back_to_v1",
    "window_coverage_rehearsal",
    "rollback_window_closed",
)

playbook_cutover_events = Table(
    "playbook_cutover_events",
    metadata,
    Column("event_id", Text, primary_key=True),          # uuid4 hex
    Column("kind", Text, nullable=False),
    Column("at", Float, nullable=False),
    Column("actor", Text, nullable=False),               # operator identity from ExecutionPrincipal
    Column("reason", Text, nullable=False),              # >= 10 chars, enforced in the command
    Column("detail", Text, nullable=False, server_default="'{}'"),   # JSON blob
    CheckConstraint(
        "kind IN (" + ", ".join(f"'{k}'" for k in CUTOVER_EVENT_KINDS) + ")",
        name="ck_playbook_cutover_events_kind",
    ),
    Index("idx_playbook_cutover_events_kind_at", "kind", "at"),
)
```

```python
# migrations/versions/<rev>_playbook_cutover_events.py
"""playbook cutover events

Playbook V2 Package 7 §6.  One additive, append-only audit table.  No data
migration and no backfill: a fleet that has not begun the cutover has no
events, and "no events" is the correct description of that state.

Revision ID: <generated>
Revises: <the head at the time; `3b560dbd527c` at drafting>
"""

def upgrade() -> None:
    op.create_table(
        "playbook_cutover_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default="'{}'"),
        sa.CheckConstraint(
            "kind IN ('v1_admission_closed', 'v1_admission_reopened', 'drain_completed', "
            "'switched_to_v2', 'rolled_back_to_v1', 'window_coverage_rehearsal', "
            "'rollback_window_closed')",
            name="ck_playbook_cutover_events_kind",
        ),
    )
    op.create_index(
        "idx_playbook_cutover_events_kind_at",
        "playbook_cutover_events",
        ["kind", "at"],
    )


def downgrade() -> None:
    op.drop_index("idx_playbook_cutover_events_kind_at", table_name="playbook_cutover_events")
    op.drop_table("playbook_cutover_events")
```

### SQLite and PostgreSQL

- **`CREATE TABLE` only.** No column is added to an existing table, so there is no rewrite, no long lock on PostgreSQL, and no SQLite table rebuild. `batch_alter_table` is not needed in either direction.
- **`kind` is a `Text` + `CheckConstraint`, not a PostgreSQL enum.** Every status column in `tables.py` is a string with a check (`ck_playbook_runs_status`, `src/database/tables.py:955`); an enum type would need `CREATE TYPE`/`DROP TYPE` in both directions and a separate migration to add a value. `CUTOVER_EVENT_KINDS` is closed here for the same reason Package 6's `REASON_CODES` is: operator tooling switches on it.
- **`at` is `Float`** (epoch seconds), matching `playbook_runs.started_at` (`:938`) and every other timestamp in this schema. Not `DateTime`: mixing the two across a join is where SQLite/PostgreSQL divergence actually shows up.
- **`detail` is a JSON string in `Text`**, matching `playbook_runs.trigger_event` / `pinned_graph`. No `JSONB`: the codebase does not use it anywhere and it would make the SQLite path a different shape.
- **Downgrade is exercised**, not assumed: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` on SQLite, then the same against PostgreSQL on `:5533` (project convention: PostgreSQL is production).
- **Reverting Package 7's code needs no downgrade.** The table is append-only and read by nothing outside `src/playbooks/cutover.py`; a reverted fleet leaves rows nobody reads. That is what keeps commit 1 independently revertible (§13).

**Commit 4 does not drop this table.** The audit of who switched the fleet and when outlives the code that wrote it — that is the point of an audit table — and `playbook_cutover_window_status`'s "runtime flipped outside the cutover command" detection (§3.9) reads it. Only the *commands* go.

---

## 7. Commit sequence

Matching roadmap §5's Package 7 sequence exactly, with the gates interleaved:

1. `ops: block new v1 runs and expose drain controls` — §5.1, §6. One PR.
2. **Gate G1 — drain sign-off** (§3.9). Human.
3. `feat: switch all playbook entry points to v2` — §5.2. One PR. Deploying it is **Gate G2**.
4. `ops: complete playbook v2 rollback observation window` — §5.3, including the D4 routing-admission port. One PR. Merging it does not close the window; `playbook_cutover_window_close` does.
5. **Gate G3 — rollback-window closure** (§3.9). Human.
6. `refactor: remove v1 playbook execution runtime` — §5.4. One PR containing the D1/D2/D3 branches.
7. `docs: declare playbook v2 the sole runtime` — §5.5. One PR.

Commits 1, 3 and 4 each stand alone and each pass the full suite on their own; that is what makes the two human gates real pauses rather than points in a merge queue.

---

## 8. Fixture data

### 8.1 `tests/fixtures/playbooks/cutover/drain-mixed.json`

The three-row corpus T-2 and T-5 both use. Realistic rows, not placeholders — `trigger_event` is a real bus payload and `pinned_graph` is a real V1 graph fragment.

```json
{
  "manager_running": ["e1f0a1c2d3e4f5a6b7c8d9e0f1a2b3c4"],
  "rows": [
    {
      "run_id": "e1f0a1c2d3e4f5a6b7c8d9e0f1a2b3c4",
      "playbook_id": "default-pipeline",
      "playbook_version": 7,
      "status": "running",
      "current_node": "per-task-review-ensure",
      "started_at": 1788400012.418,
      "completed_at": null,
      "paused_at": null,
      "waiting_for_event": null,
      "event_id": "evt-01J8Q0F6TASKCOMPLETED0001",
      "trigger_event": "{\"type\": \"task.completed\", \"event_id\": \"evt-01J8Q0F6TASKCOMPLETED0001\", \"project_id\": \"agent-queue\", \"task_id\": \"solid-harbor.31\"}",
      "pinned_graph": "{\"kind\": \"pipeline\", \"nodes\": {\"per-task-review-ensure\": {\"entry\": true, \"action\": {\"command\": \"ensure_task\", \"args\": {\"project_id\": \"{{event.project_id}}\", \"dedup_key\": \"review:task:{{event.task_id}}\"}}}}}",
      "expected_ownership": "live",
      "expected_options": ["wait", "cancel"]
    },
    {
      "run_id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
      "playbook_id": "default-pipeline",
      "playbook_version": 6,
      "status": "running",
      "current_node": "per-branch-final-review-gate",
      "started_at": 1788313611.902,
      "completed_at": null,
      "paused_at": null,
      "waiting_for_event": null,
      "event_id": "evt-01J8N4W2TASKCOMPLETED0002",
      "trigger_event": "{\"type\": \"task.completed\", \"event_id\": \"evt-01J8N4W2TASKCOMPLETED0002\", \"project_id\": \"agent-queue\", \"task_id\": \"sound-horizon.77\"}",
      "pinned_graph": "{\"kind\": \"pipeline\", \"nodes\": {}}",
      "expected_ownership": "orphaned",
      "expected_options": ["cancel"],
      "note": "started 24h before the newest row and its version is stale — the shape a daemon restart leaves behind (§1.1)"
    },
    {
      "run_id": "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
      "playbook_id": "memory-consolidation",
      "playbook_version": 3,
      "status": "paused",
      "current_node": "confirm-consolidation",
      "started_at": 1788396000.0,
      "completed_at": null,
      "paused_at": 1788396144.51,
      "waiting_for_event": "human.review.completed",
      "event_id": "evt-01J8Q0A1MEMORYCONSOLIDATE",
      "trigger_event": "{\"type\": \"memory.consolidation.requested\", \"event_id\": \"evt-01J8Q0A1MEMORYCONSOLIDATE\", \"project_id\": \"agent-queue\"}",
      "pinned_graph": "{\"kind\": \"\", \"nodes\": {}}",
      "expected_ownership": "orphaned",
      "expected_options": ["resolve", "cancel"],
      "note": "a paused run has no coroutine by definition, so `orphaned` here is not an anomaly — §3.2's ownership answers 'is a coroutine holding it', not 'is it healthy'"
    }
  ]
}
```

The third row is the one that makes the fixture worth having: it pins the distinction between *paused* (a legitimate state with a `resolve` option) and *orphaned running* (a state nothing will ever clear). A drain that conflates them either abandons human-gated work or waits forever on a dead run.

### 8.2 `tests/fixtures/playbooks/cutover/window-measures.json`

T-12's sixteen parameters: one object per §3.5 row, each with a `source_value` that fails its gate and the exact refusal substring expected.

```json
[
  {"measure": 4, "name": "authorization_denials",
   "source_value": {"by_command": {"ensure_task": {"reviewer": 2}}, "total": 2},
   "gate": 0, "expect_refusal_contains": "authorization denials: 2 (ensure_task/reviewer)"},
  {"measure": 6, "name": "dispatch_latency_p95_ms",
   "source_value": 2410.0, "baseline": 640.0,
   "gate": "<= 1.25 x baseline and <= 1000", "expect_refusal_contains": "dispatch latency p95 2410ms"},
  {"measure": 16, "name": "active_v1_runs",
   "source_value": 1,
   "gate": 0, "expect_refusal_contains": "1 active v1 run"}
]
```

(The committed file carries all sixteen; three are shown.)

### 8.3 `tests/fixtures/profiles/legacy_tools_only/profile.md`

T-13's §4.4 assertion. A profile with the pre-Package-0 shape and nothing else:

````markdown
# Legacy Reviewer

## Config
```json
{"harness": "claude", "intelligence_class": "standard"}
```

## Role
Review the diff on the task's branch and either approve it or reopen it with feedback.

## Tools
```json
{"allowed": ["Read", "Grep", "Glob", "reopen_with_feedback", "get_task"]}
```
````

After commit 4 this file must fail to parse with a message naming `## Capabilities`. Before commit 4 it parses and yields `derived_from_legacy=True`. Both assertions live in the same test so the change of behaviour is visible in one place.

---

## 9. API request and response examples

Every command reaches HTTP through `/api/execute` and the generated per-command routes (§2). Response DTOs are added to `src/api/models/playbook.py`: `PlaybookDrainStatusResponse`, `PlaybookCutoverEventResponse`, `PlaybookCutoverWindowStatusResponse`.

### 9.1 `POST /api/execute` — `playbook_v1_drain_status`, mid-drain

```json
{"command": "playbook_v1_drain_status", "args": {}}
```

```json
{
  "success": true,
  "generated_at": 1788400200.117,
  "admission": "closed",
  "closed_at": 1788400020.004,
  "closed_by": "operator:jack",
  "live_count": 1,
  "orphaned_count": 2,
  "oldest_age_seconds": 86588.2,
  "drained": false,
  "active": [
    {"run_id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7", "playbook_id": "default-pipeline",
     "playbook_version": 6, "status": "running", "current_node": "per-branch-final-review-gate",
     "started_at": 1788313611.902, "age_seconds": 86588.2, "paused_at": null,
     "waiting_for_event": null, "event_id": "evt-01J8N4W2TASKCOMPLETED0002",
     "project_id": "agent-queue", "ownership": "orphaned", "options": ["cancel"]},
    {"run_id": "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8", "playbook_id": "memory-consolidation",
     "playbook_version": 3, "status": "paused", "current_node": "confirm-consolidation",
     "started_at": 1788396000.0, "age_seconds": 4200.1, "paused_at": 1788396144.51,
     "waiting_for_event": "human.review.completed", "event_id": "evt-01J8Q0A1MEMORYCONSOLIDATE",
     "project_id": "agent-queue", "ownership": "orphaned", "options": ["resolve", "cancel"]},
    {"run_id": "e1f0a1c2d3e4f5a6b7c8d9e0f1a2b3c4", "playbook_id": "default-pipeline",
     "playbook_version": 7, "status": "running", "current_node": "per-task-review-ensure",
     "started_at": 1788400012.418, "age_seconds": 187.7, "paused_at": null,
     "waiting_for_event": null, "event_id": "evt-01J8Q0F6TASKCOMPLETED0001",
     "project_id": "agent-queue", "ownership": "live", "options": ["wait", "cancel"]}
  ]
}
```

### 9.2 `playbook_cutover_switch` — refused after the window closed (§4.3)

```json
{"command": "playbook_cutover_switch", "args": {"to": "v1", "reason": "investigating a routing regression"}}
```

```json
{
  "success": false,
  "error": "rollback window closed at 1788746400.0 by operator:jack; rollback now requires a forward change, not a runtime switch",
  "closed_event_id": "9f2c1b7e4a0d43c8b6e5f4a3d2c1b0a9"
}
```

### 9.3 `playbook_cutover_window_close` — refused, naming every failing measure

```json
{"command": "playbook_cutover_window_close", "args": {"reason": "72h elapsed, metrics look fine"}}
```

```json
{
  "success": false,
  "error": "window cannot close: 3 blocking condition(s)",
  "blocking_reasons": [
    "coverage: playbook 'coding-reflection' has dispatched 0 v2 runs since the switch",
    "measure 6 dispatch latency p95 2410ms exceeds gate (<= 1.25 x baseline 640ms, <= 1000ms)",
    "measure 14 pending events: 9 (gate <= 5)"
  ],
  "elapsed_seconds": 262800.0,
  "v2_run_count": 214
}
```

### 9.4 `playbook_v1_admission_close` — reason too short

```json
{"command": "playbook_v1_admission_close", "args": {"reason": "drain"}}
```

```json
{"success": false, "error": "reason must be at least 10 characters — it is the audit record for closing v1 admission"}
```

---

## 10. Observability and operator failure behavior

### 10.1 What this package adds to the metrics sampler

`src/metrics/sampler.py` has no playbook series today (§2). Package 7 adds four counters and two latency series — the minimum §3.5 needs, and no more, because every sample costs a row per second per series.

```python
# src/metrics/sampler.py
_COUNTED_EVENTS = (
    "task.nudged", "session.killed", "merge.succeeded",
    "capability.denied",            # NEW — §3.5 measure 4; emitted by handler.py (§10.2)
    "playbook.run.failed",          # NEW — existing PlaybookRunFailedEvent    (events.py:386)
    "playbook.run.timed_out",       # NEW — existing PlaybookRunTimedOutEvent  (events.py:450)
    "playbook.run.cancelled",       # NEW — existing PlaybookRunCancelledEvent (events.py:476)
)
```

`collect()` (`sampler.py:326`) gains one block, sourced from the same trailing-hour windows `_rate()` already maintains:

```python
"playbooks": {
    "denials_per_hour":   self._rate("capability.denied"),
    "failures_per_hour":  self._rate("playbook.run.failed"),
    "timeouts_per_hour":  self._rate("playbook.run.timed_out"),
    "cancels_per_hour":   self._rate("playbook.run.cancelled"),
    "dispatch_p95_ms":    self._slow["playbook_dispatch_p95_ms"],   # slow tier
    "resume_p95_ms":      self._slow["playbook_resume_p95_ms"],     # slow tier
    "active_v1_runs":     self._slow["playbook_active_v1_runs"],    # slow tier
},
```

The two latency percentiles and the V1 count go on the **slow tier** (`_collect_slow`, `sampler.py:389`, cached between ticks at `slow_interval_seconds`, default 5 s) because each is a range scan over receipts or `playbook_runs`. Putting them on the per-second tier would make the sampler's own cost scale with run history, which is exactly what the slow tier exists to prevent.

`active_v1_runs` deliberately survives commit 4: after V1 removal it reads zero forever, and a non-zero reading means a database was restored from before the cutover. That is worth one integer.

### 10.2 Making the denial counter exist

Both denial sites in `src/commands/handler.py` (`:902` shadow, `:912` hard) currently log and nothing else. Package 7 adds, at each, an emit on the same bus every other counted event uses:

```python
bus = getattr(self.orchestrator, "bus", None)   # the established access pattern — handler.py:997
if bus is not None:
    await bus.emit("capability.denied", {
        "command": name,
        "principal_kind": principal.kind.value,
        "profile_id": principal.profile_id,
        "namespace": decision.namespace,
        "shadow": decision.shadow,
        "fingerprint": principal.policy.fingerprint(),
    })
```

The emit is wrapped the way `command.invoked` already is at `src/commands/handler.py:991-1018` — any failure swallowed, "so a broken bus never breaks" a command. A denial that fails to publish must still deny.

No session id, no argument values: the payload is the grouping key §3.5 measure 4 needs, and nothing that could carry a secret. `shadow` is carried so the counter can separate "would have denied" from "did deny" without a second event name — measure 4 gates on `shadow=false` only.

### 10.3 Correlation fields

Roadmap §10 requires artifact hash, rule id, run id, step id, attempt, principal/profile id and contract fingerprint to be visible where applicable. The tree already has the carrier: `CorrelationContext(run_id=...)` (`src/orchestrator/core.py:996`, `:1074`). Package 7 extends it at the six switched sites to carry `artifact_sha256` and `rule_id` alongside `run_id`, so every log line a V2 run emits is joinable to the exact artifact that produced it. Step id, attempt and contract fingerprint are receipt fields (Package 3) and are not duplicated into log context.

### 10.4 Operator failure behavior

| Failure | What the operator sees | What the system does |
|---|---|---|
| Drain will not reach zero (a run keeps restarting) | `drain_status.live_count` stays > 0 across snapshots with changing `run_id`s | Nothing automatic. The signal that admission is not actually closed is the changing id; the runbook (§5.5) says to check `v1_admission` first |
| `playbook_v1_run_cancel` times out | `success=false`, "run did not stop within 30s", row untouched | Refuses rather than lying. Repeated failure means a coroutine is blocked in a non-cancellable call; the runbook's escalation is a daemon restart, after which the row is `orphaned` and cancellable |
| Switch deployed, V2 dispatch failing | `playbooks.failures_per_hour` climbs; `playbook_cutover_window_status` measure 3 goes non-zero | No auto-rollback. Rollback is `playbook_cutover_switch --to v1`, a human decision, because an automatic flip mid-run would produce exactly the mixed-runtime state §3.4 is built to avoid |
| Runtime flipped by hand | `window_status.blocking_reasons` contains "runtime flipped outside the cutover command" | Window cannot close until an operator records the switch through the command (§3.9) |
| A profile still has only `## Tools` after commit 4 | Profile load fails with a message naming `## Capabilities` | Fails closed; the affected profile dispatches nothing. §4.4 |

---

## 11. Verification

### 11.1 Per-package required commands (roadmap §5, reconciled per §2)

| Roadmap requirement | Command | Expected |
|---|---|---|
| full Python test suite | `aq test tests/ --aq-all-markers` — **once, at the exit gate**, per CLAUDE.md's testing rules | green |
| `ruff check .` with repository exclusions | `ruff check .` | clean |
| generated-client checks | `./scripts/regenerate-api-client.sh --offline && ./scripts/regenerate-ts-client.sh --offline && git diff --exit-code openapi.json packages/aq-client dashboard/src/api` | no diff |
| | `aq test tests/test_api_client_contract.py` | green |
| dashboard unit | from `dashboard/`: `npm test` | green |
| dashboard lint | from `dashboard/`: `npm run lint` | clean |
| dashboard typecheck | from `dashboard/`: `npm run typecheck` | clean |
| dashboard build | from `dashboard/`: `npm run build` | succeeds |
| SQLite migration check | `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` against a scratch SQLite file | clean both ways |
| PostgreSQL migration check | the same against `postgresql+asyncpg://…@localhost:5533/…` | clean both ways |
| cutover rehearsal with synthetic active V1 runs and pending V2 events | `aq test tests/test_playbook_cutover.py -k rehearsal` and the §11.3 live rehearsal | green; `drained: true` |
| rollback rehearsal before the window closes | §11.3 step 6 | V2 state byte-identical after `v2 → v1 → v2` |
| performance checks (compile, graph load, dispatch, receipt writes, resume) at production-like graph sizes | `aq test tests/perf/test_playbook_v2_perf.py -m perf` against the §11.2 corpus | every §3.5 latency gate met |
| repository search proving V1 imports and embedded JSON-action parsing are gone | `aq test tests/test_v1_removal.py` | green (AST-based, §5.4 T-14) |

### 11.2 The performance corpus

"Production-like graph sizes" is not a number the roadmap gives, so this plan fixes one: the largest enabled artifact in the fleet, plus a synthetic upper bound at **5× the default pipeline's rule count and 10× its node count** — `default-pipeline` today has 5 rules and 17 nodes in the one JSON block at `src/prompts/default_playbooks/default-pipeline.md:50-233`, so the synthetic case is 25 rules / 170 nodes. Both live in `tests/fixtures/playbooks/cutover/perf-corpus/`. Measure 12's 300 ms gate is asserted against the synthetic case, not the real one, so the gate does not quietly weaken as the shipped pipeline shrinks.

### 11.3 The live rehearsal (commit 1 T-5 and commit 3 T-11, run against a real daemon)

Not a unit test. `scripts/e2e-env.sh --reset` then, by hand:

1. `aq playbook v1-drain-status --json` → baseline, admission `open`.
2. Start two pipeline runs and one LLM run; kill the daemon mid-run; restart. → `orphaned_count == 3`, confirming §1.1 on a real process boundary.
3. `aq playbook v1-admission-close --reason "cutover rehearsal <date>"`.
4. Fire the trigger events again → no new `playbook_runs` rows.
5. `aq playbook v1-run-cancel --run-id … --reason "…"` for each → `drained: true`.
6. `aq playbook cutover-switch --to v2 --reason "…"`, dispatch one event per enabled playbook, then `--to v1`, then `--to v2`; diff artifact bytes, activation rows and receipt rows before and after → identical (§4.3).
7. **Pending V2 events.** Disable one enabled playbook's activation, fire its trigger twice → two rows in `playbook_pending_events`. Re-enable, `playbook_pending_event_action --action dispatch` (Package 5) → both dispatch, `pending` returns to 0, and no event was lost across the runtime flips in step 6. This is the half of the roadmap's "cutover rehearsal with synthetic active V1 runs **and pending V2 events**" that the drain steps do not cover.
8. `aq playbook cutover-window-status --json` → every measure present with a source, none `null`.

A measure that reads `null` here is a measure that cannot gate anything, and it fails the rehearsal.

### 11.4 Area suite, once, before closing

`aq test tests/test_playbook*.py tests/test_pipeline*.py tests/test_routing_admission*.py tests/test_graph_routing_admission.py tests/test_v1_removal.py tests/test_reflection*.py tests/test_triage_lifecycle.py tests/test_workflow_stage_resume.py tests/test_human_in_the_loop.py`

---

## 12. Mapping to the package exit gate

Roadmap Package 7's exit gate: *"V2 is the only system that can compile, activate, dispatch, execute, resume, and visualize current playbooks. Historical V1 runs remain readable, and all temporary dual-runtime mechanisms have been removed."*

| Exit-gate clause | Where this plan satisfies it | Evidence |
|---|---|---|
| V2 is the only system that can **compile** | D1 deletes `pipeline_compiler.py` and `assignment_compiler.py` and their `compiler.py` dispatch (§3.6) | T-13, T-14 |
| …**activate** | Package 5's `playbook_activate` is the only writer; D1-c deletes `playbooks.v2_activation_writes` so it is no longer flagged | T-13 |
| …**dispatch** | §3.4's selector at all six §1.4 sites, then D1 deletes the V1 arms | T-7, T-13 |
| …**execute** | D1 deletes `runner.py`, `pipeline_runner.py`, `runner_context.py`, `runner_events.py`, `runner_transitions.py`, `token_tracker.py` | T-13, T-14 |
| …**resume** | Sites 4 and 5 switched (§3.4), with the artifact-keyed asymmetry that lets paused V1 runs finish | T-8 |
| …**visualize** | D3 deletes `playbook-graph/`, `graph_view.py`, `graph.py` and the two graph commands; the semantic tab becomes `graph` | T-16 |
| Historical V1 runs remain readable | `playbook_runs` and its DTOs untouched; `inspect_playbook_run` tolerates a missing artifact hash | T-15 |
| All temporary dual-runtime mechanisms removed | §3.6 D1-c's flag list: `playbooks.runtime`, `playbooks.v1_admission`, `playbooks.v2_api`, `playbooks.v2_activation_writes`, `security.capability_enforcement`'s grace modes, `derived_from_legacy`, the legacy adapter, `## Tools` | T-13 |
| (roadmap outcome) Zero active V1 runs at cutover | G1 refuses without `drained: true` | T-2, T-3, T-5, §11.3 |
| (roadmap outcome) No in-flight V1 run translated into V2 state | §3.4's resume asymmetry; the drain only ever *terminates* a V1 run | T-8 |
| (roadmap outcome) No code path reads embedded JSON actions | §4.5's four shapes | T-14 |
| (roadmap outcome) Rollback window completed | §3.5's three-condition window; §3.9 G3 | T-11, T-12 |

Milestone **M7 — Cut over** is claimed at G2 with `drained: true` and every enabled playbook on a ready activation. Milestone **M8 — Cleanup complete** is claimed when §11.1 is green end to end and T-14 passes.

---

## 13. Rollback boundary

The roadmap's boundary: *"Before the observation window closes, revert the coordinated entry-point switch only; do not delete V2 artifacts or data. After V1 deletion, rollback requires a new forward change and is no longer an operational toggle."*

- **Commit 1 is independently revertible.** Reverting the code leaves one unused, append-only table; no downgrade is required (§6).
- **Commit 3 is independently revertible** *except* for D4. Reverting the routing-admission port restores a walk over V1 compiled graphs, which is correct only while V1 still exists — that is, only before commit 4. After commit 4 the port is load-bearing and reverting it breaks task admission. The commit message must say so.
- **Commit 2 is the switch, and its revert is `playbook_cutover_switch --to v1`, not `git revert`.** Reverting the code would also remove the selector that a rolled-back fleet needs to *stay* on V1 deliberately. The operational toggle is the rollback; the code revert is not.
- **Commit 4 is the one-way door.** After it merges, `playbook_cutover_switch --to v1` refuses (§4.3) because the modules it would select no longer exist. Rollback from here is a forward change — restoring V1 from history and re-reviewing it — which is exactly what the roadmap says it becomes.
- **No commit in this package deletes a V2 artifact, activation, receipt, snapshot, or wait row.** §5.2 T-7 asserts it for the switch round trip; nothing else in the package writes to those tables at all.

---

## 14. Open items for the roadmap owner

- **`tests/test_playbook_runner.py`'s 385 tests.** D2 deletes the file. Package 4's executor suites are *supposed* to cover the behaviour, but nobody has diffed the two. The disposition table (§3.6) requires D2 to file a checklist issue enumerating any behaviour this file pins that Package 4 does not, **before** deleting it. If that diff turns out to be large, deletion should be split from commit 4 into its own reviewed PR.
- **`src/prompts/example_playbooks/` and `src/prompts/default_rules/`** (16 files referenced by no Python code). Package 6 documented them; Package 7 allowlists them in T-14's scan and does not delete them (§4.5). Their disposition — delete, convert to V2 prose, or wire into an install path — is still open and belongs to whoever owns shipped sample content.
- **`src/doctor/integration_checks.py:46-53`'s hardcoded `_review_dedup_key`.** Package 6 §14 raised it for Package 7. This plan does **not** carry it: it is a doctor check reading a dedup key, not a cutover step, and changing it during the switch adds a variable to the one release that can least afford one. It should be filed as follow-up work against the reviewed artifact, after M8.
- **Behavioural parity for LLM playbooks.** Package 6 gives `memory-consolidation` and `coding-reflection` structural parity only. §3.5's acceptance table gates them on run counts and failure rates, not on output equivalence. If output equivalence matters, it needs recorded-transcript replay, which no package currently owns.
- **`capability.denied` as a bus event** (§10.2) is a Package 0 surface that Package 7 is completing. If Package 0's owner would rather ship it there, this plan's §10.2 becomes a reconciliation note instead of an implementation step.
