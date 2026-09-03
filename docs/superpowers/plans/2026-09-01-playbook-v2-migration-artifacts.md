# Playbook V2 — Package 6 child plan: Rebuild, review, and migration readiness

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:executing-plans` (or `superpowers:subagent-driven-development` when running the five commits as parallel tasks). Every task below is red/green/refactor: the failing assertion is named before the implementation step that satisfies it.

**Roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` §5 "Package 6 — Playbook rebuild, review, and migration readiness".
**Spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md`, especially "Compatibility, rebuild, and failure behavior", "Steady-state operations", "Migration and cutover" steps 5–6, and "Cutover acceptance".
**Consumes:** Packages 0–5. **Produces:** human-reviewed V2 artifacts, a complete inventory, shadow-parity evidence, pending-event policy, and a signed cutover report.

**Drafting status.** This plan was written against the live tree at `origin/main` `c7ba28d7`, *before* Packages 0–5 exist, on the supervisor's instruction to draft child plans in parallel. Every symbol this package imports from an earlier package is listed in **§3.8** with the package that owns it. The implementation task **must** begin by reconciling §3.8 against the tree and amending this document in its first commit. Nothing else in this plan depends on guessing what Packages 0–5 chose.

---

## 1. What this package is actually for

Packages 0–5 build a V2 system that nothing yet runs. Package 6 is the only package whose deliverable is *evidence*: proof that every playbook this fleet actually executes has one reviewed V2 artifact that behaves the way its V1 predecessor behaved, or differs only in ways a human wrote down and accepted.

Three properties of the live tree shape the whole package:

### 1.1 Exactly one shipped source contains an embedded action graph

`src/prompts/default_playbooks/default-pipeline.md:41-224` is a single ```` ```json ```` block holding a five-rule action graph — `rules[].nodes[].command/args/output/on_success/on_failure/for_each`. It is parsed by `compile_pipeline` (`src/playbooks/pipeline_compiler.py`), never by an LLM. This is the one file the roadmap's "remove embedded JSON action blocks" outcome is really about.

The other three shipped sources are already prose:

- `src/prompts/default_playbooks/default-assignment-routing.md` (25 lines) — pure prose plus frontmatter; compiled by `src/playbooks/assignment_compiler.py` into a fixed one-node LLM graph.
- `src/prompts/default_playbooks/memory-consolidation.md` (135 lines) — prose steps. It *has* ```` ```json ```` fences at `:67` and `:128`, but they are **step output-shape examples** ("End the step with: `{"targets": [...]}`"), not action graphs.
- `src/prompts/default_agent_type_playbooks/claude-opus/reflection.md` (120 lines) — prose, no fences.

**Consequence:** the shipped-source scan (§5.2 T-4) must classify fences, not count them. A naive `grep -c '```json'` fails `memory-consolidation.md` and would push the package into rewriting a file that needs no rewrite.

### 1.2 There are four shipped playbooks, not three

The roadmap's Package 6 "Modify" list names three files under `src/prompts/default_playbooks/`. It omits `src/prompts/default_agent_type_playbooks/claude-opus/reflection.md`, which `src/vault.py:1763` `ensure_default_agent_type_playbooks` copies into `vault/agent-types/claude-opus/playbooks/` on **every** startup and which `tests/test_default_agent_type_playbooks.py` pins. An enabled playbook installed on every host is in scope for "every enabled playbook has one reviewed V2 artifact" whether or not the roadmap listed it. §2 records the addition.

That file also carries a live authority conflict that Package 0 turns into a hard error: its frontmatter says `id: coding-reflection`, `scope: agent-type:coding`, but it installs under `agent-types/claude-opus/`, and `derive_playbook_scope` (`src/playbooks/handler.py:73`) returns `("agent_type", "claude-opus")` for that path. Under Package 0's server-authoritative merge the path wins and the frontmatter `scope:` becomes a discarded claim. This is a **documented human decision** in the sense of the roadmap's "resolve every compiler question" outcome — §5.2 T-6 records it.

### 1.3 V1's run identity is per (playbook, event); V2's is per rule

`src/orchestrator/core.py:830-1044` builds one `PipelineRunner` per matching rule, then **overwrites every runner's `run_id` with the first one's** (`:955`) and writes a single `playbook_runs` row. The `uq_playbook_runs_pb_event` partial unique index (`src/database/tables.py:922-929`) on `(playbook_id, event_id)` enforces that. `_run_pipeline` (`:988`) runs the rules sequentially and **aborts the remaining rules on the first non-completed result** (`:1001-1004`).

V2 requires "one matching event may create multiple rule runs, but each run executes exactly one rule" (roadmap §2). So for the default pipeline — where `task.completed` matches both `per-task-review` and `per-branch-final-review` — V1 and V2 will *always* disagree on run count, run identity, and failure blast radius. That is not a parity defect; it is the intended semantic change. The parity harness must carry a closed, reviewed registry of such differences (§3.5), because a harness that reports them as failures gets muted, and a harness that reports nothing proves nothing.

---

## 2. Live-tree reconciliation — deviations from the roadmap's file list

Roadmap §3 permits a child plan to refine filenames after inspecting the live tree and requires the deviation be documented. Every row was verified against `origin/main` at `c7ba28d7`.

| Roadmap says | Live tree | Decision |
|---|---|---|
| Create `tests/playbooks/test_migration_inventory.py` | **`tests/playbooks/` does not exist.** All suites are flat `tests/test_*.py` (same deviation Package 0's plan recorded at its §2) | `tests/test_playbook_migration_inventory.py` |
| Create `tests/playbooks/test_default_playbook_v2_artifacts.py` | as above | `tests/test_default_playbook_v2_artifacts.py` |
| Verify `ruff check src/playbooks/migration.py tests/playbooks` | as above | `ruff check src/playbooks/migration.py tests/test_playbook_migration_inventory.py tests/test_default_playbook_v2_artifacts.py tests/test_shipped_playbook_sources.py tests/test_playbook_shadow_parity.py tests/test_playbook_contract_release_check.py` |
| Modify three files under `src/prompts/default_playbooks/` | A **fourth** shipped playbook exists: `src/prompts/default_agent_type_playbooks/claude-opus/reflection.md`, installed by `src/vault.py:1763` on every startup, pinned by `tests/test_default_agent_type_playbooks.py` | Add it to the modify list (§1.2) |
| (not listed) | `src/prompts/example_playbooks/` (10 files) and `src/prompts/default_rules/` (6 files) are referenced by **no Python code** — `grep -rn "example_playbooks\|default_rules" src/ --include=*.py` is empty. They are dead sample content that a user can still copy into a vault | Neither is "shipped" in the activation sense. §5.2 T-4 defines the scan corpus as *installed* sources only, and T-6 gives the two directories one explicit disposition (documented as examples, excluded from the corpus by an allowlist constant, not by silence) |
| Modify "health … API surfaces" | `src/playbooks/health.py` is **run metrics** — `compute_node_metrics`, `compute_failure_analysis`, `compute_duration_metrics`, percentiles. It has no notion of `ActivationHealth` | No change to `src/playbooks/health.py`. The activation-health surface Package 6 consumes is the one Packages 3/5 create. Recorded so a reviewer does not look for a change here |
| Modify "migration API surfaces" | No migration API exists | Package 6 **creates** `playbook_migration_inventory`, `playbook_migration_acknowledge`, `playbook_release_check`, `playbook_cutover_report`, and three `playbook_pending_event_*` commands in a new `src/commands/playbook_migration_commands.py` mixin, registered like every other mixin on `CommandHandler` (§3.6) |
| Modify "release checks and CI configuration" | `.github/workflows/tests.yml` has exactly three matrix suites, all `pytest`. **There is no lint job and no release-check script.** `scripts/` holds e2e and codegen helpers only | The release check ships as a **pytest suite** (`tests/test_playbook_contract_release_check.py`) plus a `playbook_release_check` command. CI needs no new job — the existing `default` suite runs it. §5.5 T-15 also adds a doctor check so the same assertion is available on a live daemon |
| (not listed) | `src/doctor/integration_checks.py:46-53` `_review_dedup_key` hardcodes `f"review:task:{task_id}"` and its docstring says it is "kept in lockstep with `src/prompts/default_playbooks/default-pipeline.md`" | Add `src/doctor/integration_checks.py` to the modify list. Rewriting the pipeline source without updating this check silently disarms the unreviewed-PR alarm. T-5 pins the dedup key with a test that reads it from the reviewed artifact, not from prose |
| (not listed) | `tests/conftest.py:237` `DEFAULT_PIPELINE_PATH` and `tests/conftest.py:306` `PipelineEngine` are the existing V1 dispatch harness (rule selection via `_eval_pipeline_when`, `event.task` hydration, `event_id` dedup) used by `test_review_pipeline_rules.py`, `test_review_pipeline_e2e.py`, `test_default_pipeline_spec_and_proposal.py` | Reuse `PipelineEngine` verbatim as the **V1 arm** of the shadow harness (§3.5). Do not write a second V1 walker |
| (not listed) | `src/playbooks/routing.py:20` `is_deprecated_default_assignment_entry` suppresses two rule entries (`task-created-routing`, `worker-filed-triage`) that only exist in *cached* compiled artifacts, not in the current source | The inventory must not report these as missing rules. §3.2 `InventoryEntry.reasons` carries `superseded_rule` for them |
| (no storage change implied) | Acknowledged-disabled playbooks need somewhere durable to live | **One additive Alembic revision** creating `playbook_migration_acks` (§6). New table only; `downgrade` drops it; reverting Package 6 code needs no downgrade, preserving the roadmap's rollback boundary |

Two naming reconciliations:

- **`src/playbooks/migration.py` vs `src/database/hierarchy_migration.py`.** The repo already has a module named `*_migration.py` that means "Alembic data migration". `src/playbooks/migration.py` means something different — V1→V2 *inventory and readiness*, no schema work. Keep the roadmap's name (it is the locked module-map entry) but the module docstring must say so explicitly, and no Alembic revision may import it.
- **"Compiler questions".** The spec's compile flow (step 3) has the compiler agent emit a proposal; the roadmap's outcome says "resolve every compiler question through a documented human decision". The live V1 compiler has no question channel — `CompilationResult` (`src/playbooks/compiler.py:57`) carries `errors` and `structured_errors`, not questions. Package 2 owns the question model. Package 6 consumes it as `CompileQuestion` (§3.8) and stores each resolution in the fixture's `review.md` front-matter (§3.4).

---

## 3. Locked interfaces for this package's parallel tasks

Roadmap §7 allows Package 6's read-only project inventory to start before Package 5 lands. In practice the five commits split into work that can run in parallel once these shapes are fixed. Everything in §3 is **locked**: a parallel task may add fields, and must not rename or contradict.

### 3.1 Parallelism map

| Task group | Commit | May start when | Blocked by |
|---|---|---|---|
| **A — Inventory** (`src/playbooks/migration.py`, §5.1) | 1 | Package 4 exit gate | Nothing from Package 5. Read-only: it never writes an activation |
| **B — Prose sources** (§5.2) | 2 | Package 2 exit gate (needs the compiler to accept prose-only sources) | Independent of A |
| **C — Reviewed fixtures** (§5.3) | 3 | Package 5 exit gate — reviewed **activation** requires the diff/health UI | B |
| **D — Shadow parity** (§5.4) | 4 | Package 4 exit gate (`ExecutionMode.shadow`) | B for the rewritten pipeline; can be developed against the current source first |
| **E — Release check + cutover report** (§5.5) | 5 | A + C | — |

A, B and D can proceed concurrently on separate branches. C is the only one that must wait for Package 5, which is exactly the constraint roadmap §7 states.

### 3.2 Inventory types — `src/playbooks/migration.py`

```python
PlaybookDisposition = Literal["ready", "question_required", "invalid", "disabled"]

@dataclass(frozen=True, slots=True)
class MigrationReason:
    code: str                  # from REASON_CODES, below — closed set
    message: str               # operator-facing, one sentence, no stack traces
    source_line: int | None    # 1-based line in the authoring Markdown, when known

@dataclass(frozen=True, slots=True)
class SourceRef:
    vault_rel_path: str            # "system/playbooks/default-pipeline.md"
    bundled_rel_path: str | None   # "src/prompts/default_playbooks/default-pipeline.md", or None when project-authored
    source_sha256: str             # "sha256:<hex>" of the raw Markdown bytes

@dataclass(frozen=True, slots=True)
class InventoryEntry:
    playbook_id: str
    scope: str                     # "system" | "supervisor" | "agent_type" | "project" — derive_playbook_scope()[0]
    scope_identifier: str | None
    source: SourceRef
    v1_kind: str                   # CompiledPlaybook.kind: "" | "pipeline" | "assignment-routing"
    v1_version: int | None         # None when never compiled
    v1_enabled: bool
    disposition: PlaybookDisposition
    reasons: tuple[MigrationReason, ...]
    artifact: ArtifactRef | None           # Package 3; None until a V2 artifact exists
    activation_health: str | None          # ActivationHealth value; None when no activation row
    has_embedded_action_block: bool
    acknowledged_by: str | None            # set only when disposition == "disabled"
    acknowledged_at: float | None

@dataclass(frozen=True, slots=True)
class MigrationInventory:
    generated_at: float
    contract_fingerprint: str              # the registry-wide fingerprint at scan time
    entries: tuple[InventoryEntry, ...]

    def by_disposition(self, d: PlaybookDisposition) -> tuple[InventoryEntry, ...]: ...
    def blocking(self) -> tuple[InventoryEntry, ...]:
        """Entries that block cutover: everything not `ready`, minus acknowledged `disabled`."""
    def to_dict(self) -> dict: ...         # stable key order; the API DTO and the CLI both render this
```

`REASON_CODES` is a **closed frozenset**. A code not in it is a programming error (`ValueError` at construction), because operator tooling and the cutover report both switch on these:

```
source_unreadable        duplicate_playbook_id      scope_conflict
embedded_action_block    compile_question           schema_violation
unknown_command          unknown_event              unknown_profile
capability_not_declared  binding_unassigned         nested_loop_rejected
stale_contract           superseded_rule            operator_disabled
```

Entry point, read-only and LLM-free:

```python
async def build_inventory(
    *,
    vault_root: str,
    store: CompiledPlaybookStore,
    contract_registry: CommandContractRegistry,     # Package 1
    activation_repo: PlaybookActivationRepository | None = None,   # Package 3; None -> activation_health is None
    ack_repo: MigrationAckRepository | None = None,                # §6
) -> MigrationInventory: ...
```

**Read-only is a hard invariant, not a style preference.** `build_inventory` may not compile, activate, write a file, or emit an event. T-2's test asserts it with a `Database` wrapper that raises on any write and a `CompiledPlaybookStore` whose `save`/`delete` raise. This is what lets task group A run before Package 5.

**Source enumeration** reuses the live discovery path rather than re-deriving it: walk `vault_root` with the three `PLAYBOOK_PATTERNS` globs (`src/playbooks/handler.py:66`) and classify each hit with `derive_playbook_scope` (`:73`) — the same pair `PlaybookManager.reconcile_compilations` (`src/playbooks/manager.py:1324-1355`) uses. A playbook present in `store.list_all()` but with no source file is `invalid` / `source_unreadable`; a source file with no compiled artifact is `question_required` at worst, never silently dropped.

### 3.3 Disposition rules (locked)

Exactly one disposition per entry, evaluated in this order — first match wins:

1. `invalid` — the source cannot be read, the frontmatter has no `id`, two sources claim the same `id`, the stored artifact fails strict schema validation, or the V2 validator reports an error that no human decision can resolve (`schema_violation`, `unknown_command`, `unknown_event`, `unknown_profile`, `binding_unassigned`, `nested_loop_rejected`, `duplicate_playbook_id`, `source_unreadable`).
2. `disabled` — an acknowledgement row exists in `playbook_migration_acks` whose `source_sha256` matches the current source, **or** the source frontmatter says `enabled: false`. Carries `operator_disabled`.
3. `question_required` — the artifact compiles but at least one `CompileQuestion` is unresolved, the source still holds an embedded action block, the frontmatter scope conflicts with the path scope, or the artifact's `compiled_against` fingerprints no longer match the registry (`compile_question`, `embedded_action_block`, `scope_conflict`, `stale_contract`).
4. `ready` — a validated V2 artifact exists, `compiled_against` matches the current registry, and every referenced profile and capability resolves.

An `enabled: false` frontmatter playbook is `disabled` **without** an acknowledgement row: the author already made the decision. The acknowledgement (§6) is required only for playbooks that are *enabled but cannot be migrated* — the roadmap's "require an explicit acknowledgement for intentionally disabled playbooks". `blocking()` therefore returns `question_required` + `invalid` + any `disabled` entry whose ack is missing **and** whose source does not say `enabled: false`.

**Acknowledgement invalidation.** An ack is keyed by `(playbook_id, scope, scope_identifier, source_sha256)`. Editing the Markdown changes `source_sha256` and the ack stops matching, so the playbook returns to `question_required`. An operator cannot acknowledge a playbook once and have the waiver survive a rewrite.

### 3.4 Reviewed-artifact fixture layout (locked)

```
tests/fixtures/playbooks/v2/<playbook_id>/
    source.md          # byte-exact copy of the authoring Markdown reviewed at approval time
    artifact.json      # canonical artifact bytes as ArtifactStore.put() would write them
    artifact.sha256    # single line, "sha256:<64 hex>", no trailing spaces
    review.md          # the human decision record
```

`<playbook_id>` is the **frontmatter id**, so the four directories are `default-pipeline/`, `default-assignment-routing/`, `memory-consolidation/`, `coding-reflection/` (not `reflection`). `tests/fixtures/` already exists (`formulas/`, `task_graphs/`, `transcripts/`); `playbooks/` is new.

Three properties make the fixtures useful rather than decorative:

- **No LLM in CI.** `artifact.json` is a *recorded* compiler output. Tests never recompile it; they load it through the Package 2 strict model and the Package 1 registry and assert it still validates. A compiler change that alters output is caught by the release check (§5.5), not by a nondeterministic re-run.
- **Byte-exact canonicality.** `sha256(artifact.json bytes) == artifact.sha256`, and `ArtifactStore.put(load(artifact.json)).artifact_sha256` returns the same value. Round-tripping through the model must be a no-op — that is the whole point of canonical bytes. A fixture that fails this means the canonicaliser is not canonical.
- **`source.md` is pinned separately from the live source.** `sha256(source.md) == artifact.compiled_from.source_digest`, and a separate assertion compares `source.md` against the live shipped file. When they diverge, the failure message is "the shipped Markdown changed since review; recompile and re-review", which is exactly the signal the reviewer needs — never a silent auto-update.

`review.md` frontmatter (locked keys; the body is prose):

```yaml
---
playbook_id: default-pipeline
artifact_sha256: "sha256:…"
source_sha256: "sha256:…"
contract_fingerprint: "sha256:…"
reviewed_by: "<git identity of the human who approved>"
reviewed_at: "2026-09-14"
decision: approved            # approved | rejected
questions_resolved: 3
capabilities_granted:
  aq_commands: [ensure_task, add_dependency, get_downstream_tasks, gate_create, task_batch_commit]
  harness_tools: []
  plugin_tools: []
profiles_referenced: [reviewer, final-reviewer, spec-ingest]
---
```

Required body sections, asserted by name in T-7 (`## ` headings, exact text):

```
## Compiler questions and decisions
## Semantic diff versus the V1 graph
## Capabilities and why each is needed
## AI profiles, budgets, and output schemas
## Accepted behaviour differences
```

`decision: rejected` is legal and means the fixture is a recorded negative — no activation may reference it. T-7 asserts `decision == "approved"` for all four shipped playbooks and that no `rejected` fixture appears in the activation set.

### 3.5 Shadow-parity types (locked)

Lives in `src/playbooks/migration.py` alongside the inventory so the cutover report can import both without a cycle.

```python
@dataclass(frozen=True, slots=True)
class CommandInvocation:
    order: int              # 0-based, per observation
    command: str
    args_canonical: str     # canonical JSON of the arguments AFTER normalisation (§3.5.1)

@dataclass(frozen=True, slots=True)
class AuthzDecision:
    command: str
    principal_kind: str
    allowed: bool
    reason: str | None

@dataclass(frozen=True, slots=True)
class ShadowObservation:
    arm: Literal["v1", "v2"]
    event_id: str
    event_type: str
    rules_selected: tuple[str, ...]                  # sorted rule ids
    node_path: tuple[str, ...]                       # "<rule_id>/<step_id>" in visit order
    commands: tuple[CommandInvocation, ...]
    routing_outputs: Mapping[str, Any]               # only the outputs a transition actually reads
    terminal: str                                    # completed | failed | timed_out | cancelled
    authorization: tuple[AuthzDecision, ...]

DifferenceClass = Literal["identical", "expected_v2_semantics", "unexplained"]

@dataclass(frozen=True, slots=True)
class ParityFinding:
    field: Literal["rules_selected", "node_path", "commands", "routing_outputs",
                   "terminal", "authorization"]
    v1: Any
    v2: Any
    classification: DifferenceClass
    rationale_id: str | None    # key into EXPECTED_DIFFERENCES; required iff classification == "expected_v2_semantics"

def compare(v1: ShadowObservation, v2: ShadowObservation) -> tuple[ParityFinding, ...]: ...
```

#### 3.5.1 Argument canonicalisation

`args_canonical` is `json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)` after:

1. dropping keys whose contract marks them sensitive (replaced with `"<redacted>"`, key retained so a *missing* key is still a difference);
2. replacing every generated id — `run_id`, `task_id` values created *during* the observation, `event_id` — with a stable placeholder `<gen:N>` allocated in first-appearance order per arm. Ids that came *in* on the event are compared literally;
3. normalising whitespace **only** inside values the contract marks `free_text` (the pipeline's `description` arguments carry `\n`-joined prose that V1 builds by string substitution).

Rule 2 is what makes the comparison meaningful at all: V1 and V2 mint different uuids for the same logical task, and comparing them raw yields 100% difference on every observation.

#### 3.5.2 `EXPECTED_DIFFERENCES` — closed registry

A module-level `dict[str, str]` mapping rationale id → one-paragraph justification. A classifier may only return `expected_v2_semantics` with an id present here, and T-10 asserts **every** id is exercised by at least one observation in the corpus (an unused rationale is a stale waiver and fails the suite).

Locked initial entries:

| id | Difference | Why it is intended |
|---|---|---|
| `run-per-rule` | V1 writes one `playbook_runs` row per `(playbook_id, event_id)` and reuses `primary_runner.run_id` for every rule (`src/orchestrator/core.py:955`); V2 starts one run per matching rule | Roadmap §2: "each run executes exactly one rule". Compared as: `rules_selected` must match; run identity is excluded from comparison |
| `rule-failure-isolation` | V1 aborts remaining rules when one rule fails (`src/orchestrator/core.py:1001-1004`) and marks the shared row failed; V2 fails only that rule's run | Direct consequence of `run-per-rule`. Any observation exercising it must assert the *surviving* rule's commands still appear in the V2 arm |
| `loop-frame-shape` | V1 stores the `for_each` variable in the flat `outputs` dict and pops it in a `finally` (`src/playbooks/pipeline_runner.py:165-188`); V2 holds a typed loop frame in the snapshot | Values compared are per-iteration `commands`, which must be identical; snapshot shape is not compared |
| `unassigned-ref-rejected` | V1's `_substitute` (`src/playbooks/pipeline_runner.py:48-63`) resolves a missing reference to `None` when the whole string is one placeholder and to `""` inside a larger string; V2 rejects unassigned bindings at compile time | Roadmap §2: "bindings are definitely assigned before use". **Each affected V1 behaviour must be listed case-by-case in the observation's finding**, because each one is a latent V1 defect the rebuild fixes, not a blanket waiver |
| `terminal-vocabulary` | V1 `RunResult.status` is `completed`/`failed` only; V2 adds `timed_out`/`cancelled` | Compared after mapping V2 `timed_out`/`cancelled` → not-`completed`. A V2 `completed` where V1 failed, or vice versa, is `unexplained` |

**`authorization` is never `expected_v2_semantics`.** V1 performs no capability check; V2 does. A V2 denial that V1 allowed is `unexplained` and must be fixed either by granting the capability in the reviewed `review.md` (and re-running) or by changing the playbook. The exit gate says "no unexplained behavior **or authorization** differences" — a waiver here would void the package.

### 3.6 Command surface (locked names)

New mixin `src/commands/playbook_migration_commands.py`, mixed into `CommandHandler` beside the existing `PlaybookCommandsMixin`. Tool category `playbooks` in `src/tools/definitions.py:_TOOL_CATEGORIES`, which gives each one a kebab-case CLI form under `aq playbook` via `register_auto_commands`.

| Command | CLI | Writes? | Purpose |
|---|---|---|---|
| `playbook_migration_inventory` | `aq playbook migration-inventory [--json] [--disposition D]` | no | `MigrationInventory.to_dict()` |
| `playbook_migration_acknowledge` | `aq playbook migration-acknowledge --playbook-id ID --reason TEXT` | yes | Insert/replace one `playbook_migration_acks` row |
| `playbook_migration_unacknowledge` | `aq playbook migration-unacknowledge --playbook-id ID` | yes | Delete the ack; the entry returns to its computed disposition |
| `playbook_release_check` | `aq playbook release-check [--json]` | no | §5.5; non-zero exit when any enabled activation is `stale_contract` |
| `playbook_cutover_report` | `aq playbook cutover-report [--json]` | no | §3.7 |
| `playbook_pending_event_list` | `aq playbook pending-event-list [--playbook-id ID]` | no | Package 3's `playbook_pending_events` rows, oldest first |
| `playbook_pending_event_replay` | `aq playbook pending-event-replay --event-id ID` | yes | Re-dispatch one held event against the current activation |
| `playbook_pending_event_discard` | `aq playbook pending-event-discard --event-id ID --reason TEXT` | yes | Auditable drop |

Every command returns the repo's `{"success": bool, ...}` dict. Response DTOs go in `src/api/models/playbook.py` next to the existing ones, named `PlaybookMigrationInventoryResponse`, `PlaybookMigrationAckResponse`, `PlaybookReleaseCheckResponse`, `PlaybookCutoverReportResponse`, `PlaybookPendingEventListResponse`.

Required capability for all of them: `aq_commands: playbook_migration_*` etc. — the three write commands are operator-only and must **not** appear in any shipped agent profile's capability set. T-3 asserts that against `src/profiles/defaults/`.

### 3.7 Cutover report (locked schema)

`playbook_cutover_report` returns, and `docs/guides/playbook-v2-cutover-report-template.md` renders:

```python
{
  "success": True,
  "generated_at": 1788400000.0,
  "contract_fingerprint": "sha256:…",
  "artifacts": [                       # one per enabled activation
    {"playbook_id": "default-pipeline", "scope": "system", "scope_identifier": None,
     "artifact_sha256": "sha256:…", "source_sha256": "sha256:…",
     "activation_health": "ready", "reviewed_by": "…", "reviewed_at": "2026-09-14"}
  ],
  "unresolved": [                      # MigrationInventory.blocking()
    {"playbook_id": "…", "disposition": "question_required",
     "reasons": [{"code": "compile_question", "message": "…", "source_line": 61}]}
  ],
  "acknowledged_disabled": [
    {"playbook_id": "…", "reason": "…", "acknowledged_by": "…", "acknowledged_at": 1788…}
  ],
  "pending_events": {"total": 0, "oldest_age_seconds": None, "by_playbook": {}},
  "active_v1_runs": {"running": 0, "paused": 0, "oldest_age_seconds": None, "runs": []},
  "parity": {"observations": 24, "identical": 19, "expected": 5, "unexplained": 0,
             "suite": "tests/test_playbook_shadow_parity.py", "recorded_at": 1788…},
  "rollback_ready": True,
  "blocking_reasons": []               # human-readable; empty iff rollback_ready and cutover-eligible
}
```

`active_v1_runs` reads `playbook_runs` filtered to `status IN ('running','paused')` — the live V1 table (`src/database/tables.py:898`). `rollback_ready` is `True` only when every enabled activation has a checked-in reviewed fixture **and** the previous V1 compiled artifact is still present in `CompiledPlaybookStore` (Package 7's rollback needs it; Package 6 proves it exists).

`parity` is read from a committed run record at `tests/fixtures/playbooks/v2/parity-report.json`, written by T-12, not recomputed live — the report must be produceable on a machine that cannot run the suite.

### 3.8 Symbols this package imports from Packages 0–5

**Reconciliation is the implementation task's first commit.** If a symbol below is named differently in the live tree when Package 6 starts, amend this table and every reference in §5 in the same commit; do not silently substitute.

| Symbol | Owning package | Module the roadmap assigns | Package 6 uses it for |
|---|---|---|---|
| `CapabilityPolicy` | 0 | `src/profiles/capabilities.py` | capability audit (T-9); `review.md.capabilities_granted` |
| `ExecutionPrincipal` | 0 | `src/commands/principal.py` | shadow arms; operator-only command authorization |
| `CommandContract`, `CommandRegistration` | 1 | `src/commands/contracts/models.py`, `registry.py` | fingerprints, sensitive-field policy, `free_text` marking (§3.5.1) |
| `CommandContractRegistry` (lookup + registry-wide fingerprint) | 1 | `src/commands/contracts/registry.py` | `MigrationInventory.contract_fingerprint`; release check |
| `CommandResult` | 1 | `src/commands/contracts/models.py` | V2 arm's `routing_outputs` |
| `PlaybookDefinition` (strict V2 model) | 2 | `src/playbooks/definition.py` | fixture validation |
| `CompileQuestion` | 2 | `src/playbooks/authoring.py` or `validation.py` | `compile_question` reason; `questions_resolved` |
| `ArtifactRef`, `ArtifactStore` | 3 | `src/playbooks/artifact_store.py` | fixture hashes; canonical-byte assertion |
| `ActivationHealth`, activation repository | 3 | `src/playbooks/activation.py`, `src/database/queries/playbook_artifact_queries.py` | `InventoryEntry.activation_health`; cutover report |
| `playbook_pending_events` table + queries | 3 | `src/database/queries/playbook_run_queries.py` | pending-event commands |
| `ExecutionMode.shadow`, `PlaybookEngine` | 4 | `src/playbooks/engine.py` | V2 arm of the parity harness |
| Artifact diff + activation review UI | 5 | `dashboard/src/pages/playbook-graph/*` | the human review that produces `review.md` |

#### 3.8.1 Reconciliation against the live tree (Commit 1, `solid-harbor.51`)

Packages 0–5 have all landed. Every symbol above exists as assigned except the
three rows below. This section is the amendment §3.8 requires; the table above
is left as drafted so the delta is legible.

| Row as drafted | Live tree | What Package 6 does |
|---|---|---|
| `CommandContractRegistry` (1) | The class is `ContractRegistry` (`src/commands/contracts/registry.py`), reached through the module-level singleton `CONTRACTS` (`src/commands/contracts/__init__.py`). The registry-wide fingerprint is `registry_fingerprint()` | `build_inventory(contract_registry=...)` takes anything exposing `registry_fingerprint()`; the command surface passes `CONTRACTS` |
| `CompileQuestion` (2) | **Does not exist.** Package 2 shipped `Diagnostic` (`severity`/`code`/`message`/`rule_id`/`step_id`, `src/playbooks/validation.py`) and `propose()` (`src/playbooks/proposal.py`); there is no separate question model | The `compile_question` reason is raised from the two conditions the inventory can observe without compiling: no reviewed artifact is active yet, or the authoring Markdown changed after the active artifact was reviewed. `questions_resolved` stays a `review.md` field for Commit 3, which does have the compiler in hand |
| `playbook_pending_event_list\|replay\|discard` (§3.6) | Package 5 already ships `playbook_pending_events` (list) and `playbook_pending_event_action` (dispatch/discard) | **Superseded — Package 6 adds no pending-event commands.** Duplicating the surface would give operators two spellings of one action. Pending-event *surfacing* lands instead as `InventoryEntry.pending_events` and `MigrationInventory.to_dict()["pending_events_total"]`, which is what the §3.7 cutover report reads |

Three further deviations, in the spirit of §2's table:

- **`tests/playbooks/` still does not exist.** The suites are `tests/test_playbook_migration_inventory.py` (T-1/T-2) and `tests/test_playbook_migration_commands.py` (T-3), the latter a split of T-3's assertions out of the inventory suite so the database-backed cases carry their own `sqlite`/`postgres` fixture.
- **§5.1 case 4 pinned line 41** for `default-pipeline.md`'s embedded action fence. The shipped file has moved since the plan was drafted and the fence now opens at line 107. The test recomputes the expected line by classifying fences in the shipped file rather than carrying a literal that will rot again.
- **Package 6's response DTOs live in `src/api/models/playbook_migration.py`**, not in `playbook_v2.py`. That module is Package 5's frozen §4 contract and `tests/test_playbook_v2_api_dtos.py` asserts its `RESPONSE_MODELS` holds exactly the Package 2 and 5 surfaces; Package 6 adds commands to the same `aq playbook` CLI group, not to that contract.

If a symbol turns out **not** to exist because an earlier package deviated, the fallback is explicit, never invented: record it as a `MigrationReason` with code `schema_violation` in the reconciliation commit's message and escalate to the roadmap owner. Package 6 must not ship a local reimplementation of an earlier package's interface.

---
## 4. Security analysis

Package 6 introduces no new execution path, but it does introduce three new trust decisions.

### 4.1 A reviewed fixture is an authority claim

`tests/fixtures/playbooks/v2/<id>/review.md` records that a human approved a set of capabilities for a playbook. If anything ever reads that file to *grant* those capabilities, a repository write becomes a privilege grant.

**Mitigation, enforced by test:** the fixtures are compared against, never applied. `review.md.capabilities_granted` is asserted to be a **superset check in one direction only** — T-9 fails when the artifact requires a capability the review did not list; it never adds a capability because the review listed one. Production capability comes from the profile (`CapabilityPolicy`, Package 0) and from the database activation, both server-owned. T-9 asserts `capabilities_granted` is not referenced by any module under `src/` (`grep -rn "capabilities_granted" src/` must be empty).

### 4.2 Acknowledgement is a waiver, so it must be narrow, attributed, and self-expiring

`playbook_migration_acknowledge` lets an operator declare "this playbook cannot migrate; proceed without it". That is the one mechanism in the package capable of moving the fleet past a real problem.

- **Attributed.** `acknowledged_by` comes from the `ExecutionPrincipal`, never from the request body. A request that supplies `acknowledged_by` has it stripped, exactly as Package 0 strips `_principal` (T-3 asserts this with a spoofing case).
- **Self-expiring.** Keyed by `source_sha256` (§3.3); any edit invalidates it.
- **Narrow.** One row per playbook. There is no "acknowledge all", no glob, and no `--force` that bypasses the reason string. `reason` is `NOT NULL` with a length floor of 12 characters, asserted at the command boundary — an empty waiver is not a waiver.
- **Operator-only.** The three write commands must not appear in any shipped profile's capability set (§3.6). A worker agent that can acknowledge its way past a broken playbook can disable the review pipeline that reviews it.
- **Visible.** Every acknowledged entry appears in `cutover_report.acknowledged_disabled`, and the report is part of the Package 7 gate. A waiver cannot be quiet.

### 4.3 Pending events are held, untrusted input

Events queued against a `needs_rebuild` or unavailable activation (spec, "Compatibility, rebuild, and failure behavior") are replayed later, potentially against a *different* artifact than the one live when they arrived.

- Replay re-runs the current activation's rule matching and its `when` guards from scratch; a held event is never fast-pathed past a guard.
- Replay is subject to the same rule-level deduplication as a fresh dispatch, so an event already consumed cannot be double-applied by an activation (spec: "activation alone never duplicates an event already consumed").
- `playbook_pending_event_replay` requires an explicit `--event-id`; there is no bulk replay in Package 6. Bulk replay, if wanted, is Package 7's decision with its own rehearsal.
- Held payloads are stored as received and are **not** re-signed or re-attributed. The replaying principal is the operator, and the run's `ExecutionPrincipal` is derived from the activation's profile — a held event cannot carry principal fields into the replay. T-16 asserts a held event containing `_principal`/`_capabilities` keys replays with those keys stripped.

### 4.4 The shadow arm must not act

Running V1 and V2 over the same event is only safe if exactly one of them has effects. The parity harness runs **both** arms with side-effect-free executors: the V1 arm's `handler.execute` is a recording double, and the V2 arm uses `ExecutionMode.shadow` (Package 4's "zero command, AI, task, gate, or external side effects").

T-10's first assertion is the negative one: a `CommandHandler` double whose `execute` raises `AssertionError` on every call, wired into both arms, must still produce two complete `ShadowObservation`s. If either arm reaches a real handler the suite fails loudly rather than creating reviewer tasks and gates in a test database.

### 4.5 Residual risks accepted in this package

- **The fixtures record one reviewer's judgement.** Nothing in Package 6 proves the *approved* capability set is minimal, only that the artifact does not exceed it. Minimality is a review-quality question, addressed by the diff UI (Package 5), not by a test.
- **Parity coverage is corpus-bounded.** The harness proves agreement over the events in `tests/fixtures/playbooks/v2/events/` (§8.3), not over all possible events. §5.4 fixes the corpus to every `(rule, guard-outcome)` pair in the four shipped playbooks, which is the strongest bound available without production traffic.
- **`memory-consolidation` and `coding-reflection` are LLM playbooks.** Their V1 behaviour is nondeterministic, so they get *structural* parity (rules, triggers, profiles, budgets, capabilities, transitions) and no per-run command comparison. §5.4 records this as a coverage limit in the cutover report's `parity` block rather than pretending to compare them.

---

## 5. Tasks

Five commits, matching the roadmap's sequence. Each begins with a test task whose assertions fail before the implementation task lands. `aq test` is the runner (repo `CLAUDE.md`); bare `pytest` is fine for a single file.

### 5.1 Commit 1 — `feat: inventory v1 playbooks and migration readiness`

#### T-1 — `tests/test_playbook_migration_inventory.py` (red)

Write the suite first, every case `@pytest.mark.xfail(strict=True, reason="Package 6 T-2")`. The failing assertions, named:

1. `test_reason_codes_are_closed` — `MigrationReason(code="not_a_code", message="x")` raises `ValueError`; every member of `REASON_CODES` is reachable from at least one other test in the file (a `pytest` end-of-module assertion over a collected set).
2. `test_enumerates_all_four_shipped_playbooks` — seed a tmp vault via `ensure_default_playbooks` + `ensure_default_agent_type_playbooks`, run `build_inventory`, assert `{e.playbook_id for e in inv.entries} == {"default-pipeline", "default-assignment-routing", "memory-consolidation", "coding-reflection"}`. **Fails today** because `build_inventory` does not exist; fails for the wrong reason if the implementer forgets the agent-type tree (§1.2).
3. `test_inventory_is_read_only` — pass a `Database` double whose every `execute`/`commit` raises and a `CompiledPlaybookStore` whose `save`/`delete` raise; assert `build_inventory` completes. The assertion that matters is that no exception escapes.
4. `test_embedded_action_block_is_question_required` — the current `default-pipeline.md` yields `disposition == "question_required"` with a `MigrationReason(code="embedded_action_block", source_line=41)`. The line number is asserted exactly: the block opens at `src/prompts/default_playbooks/default-pipeline.md:41`.
5. `test_scope_conflict_detected` — `coding-reflection` yields a `scope_conflict` reason quoting both scopes (`agent-type:coding` from frontmatter, `agent_type:claude-opus` from path).
6. `test_duplicate_id_is_invalid` — two source files with `id: dup` produce **one** entry, `invalid`, `duplicate_playbook_id`, whose message names both `vault_rel_path`s.
7. `test_disabled_requires_ack_unless_frontmatter` — a source with `enabled: false` is `disabled` with no ack row; a source with `enabled: true` that fails to compile is `question_required` until an ack row exists, then `disabled`; editing the source (new `source_sha256`) returns it to `question_required`.
8. `test_blocking_excludes_acknowledged` — `inv.blocking()` omits acknowledged-disabled and frontmatter-disabled entries and includes everything else non-`ready`.
9. `test_superseded_rules_not_reported_missing` — a cached compiled artifact containing a `task-created-routing` rule entry produces no `unknown_*` reason (guarded by `is_deprecated_default_assignment_entry`, `src/playbooks/routing.py:20`).
10. `test_to_dict_is_stable` — `json.dumps(inv.to_dict(), sort_keys=False)` is byte-identical across two calls with a frozen clock, and `generated_at` is the only field that varies with time.

**Verify:** `aq test tests/test_playbook_migration_inventory.py -q` → all xfail, zero xpass.

#### T-2 — `src/playbooks/migration.py`

Implement §3.2 and §3.3. Structure: `_enumerate_sources(vault_root)` (walk + `PLAYBOOK_PATTERNS` + `derive_playbook_scope`), `_classify(entry_inputs)` (the ordered rules), `build_inventory(...)` (compose, sort by `(scope, scope_identifier or "", playbook_id)`).

Module docstring must state, in its first paragraph, that this module performs **no schema migration** and is unrelated to `src/database/hierarchy_migration.py` (§2).

Flip the xfails off case by case. **Verify:** `aq test tests/test_playbook_migration_inventory.py -q` → 10 passed.

#### T-3 — commands, storage, API, CLI

- `src/commands/playbook_migration_commands.py`: `playbook_migration_inventory`, `playbook_migration_acknowledge`, `playbook_migration_unacknowledge`. Mixed into `CommandHandler` alongside the existing playbook mixins.
- Alembic revision per §6 creating `playbook_migration_acks`.
- `src/database/queries/playbook_migration_queries.py`: `upsert_ack`, `delete_ack`, `list_acks` — the `MigrationAckRepository` of §3.2.
- DTOs in `src/api/models/playbook.py` (§3.6).
- `src/tools/definitions.py`: three entries, category `playbooks`.

New assertions appended to the same suite:

11. `test_acknowledge_requires_reason` — `reason=""` and `reason="too short"` are refused (`success: False`, error names the 12-character floor); a 12+ character reason succeeds.
12. `test_acknowledged_by_is_server_derived` — a call passing `acknowledged_by: "root"` in args stores the principal's identity, not `"root"`.
13. `test_ack_write_commands_absent_from_shipped_profiles` — parse every `src/profiles/defaults/*/profile.md` (installed by `src/vault.py:1705` `ensure_default_profiles`) and assert none names `playbook_migration_acknowledge`, `playbook_migration_unacknowledge`, or `playbook_pending_event_*`.
14. `test_alembic_round_trip` — `alembic upgrade head` then `downgrade -1` drops `playbook_migration_acks` and leaves every other table intact (§6).

**Verify:**
```bash
aq test tests/test_playbook_migration_inventory.py -q
aq test tests/test_database.py -q -k "migration or schema"
aq playbook migration-inventory --json | head -40      # against the e2e daemon (§11)
ruff check src/playbooks/migration.py src/commands/playbook_migration_commands.py src/database/queries/playbook_migration_queries.py
```

**Commit gate:** the inventory runs against a real vault and classifies all four shipped playbooks; nothing has been activated.

---

### 5.2 Commit 2 — `refactor: rewrite shipped playbooks as prose authoring sources`

#### T-4 — `tests/test_shipped_playbook_sources.py` (red)

The scan, and the classifier that makes it correct (§1.1).

```python
INSTALLED_SOURCE_ROOTS = (
    "src/prompts/default_playbooks",
    "src/prompts/default_agent_type_playbooks",
)
# Not installed by any code path; documented as examples in T-6.
EXCLUDED_SAMPLE_ROOTS = (
    "src/prompts/example_playbooks",
    "src/prompts/default_rules",
)

def is_action_block(fence_body: str) -> bool:
    """True when a ```json fence is an executable graph rather than an example."""
```

`is_action_block` returns `True` when the fence parses as JSON **and** the parsed object is a mapping containing `"rules"` or `"nodes"` at the top level. `memory-consolidation.md:67` (`{"targets": [...]}`) and `:128` (`{"tasks_created": [...]}`) are `False`; `default-pipeline.md:41` is `True`.

Failing assertions:

1. `test_no_installed_source_has_an_action_block` — over every `.md` under `INSTALLED_SOURCE_ROOTS`. **Fails today on exactly one file**, and the failure message must name it and the line: `src/prompts/default_playbooks/default-pipeline.md:41`.
2. `test_classifier_distinguishes_examples` — the classifier is itself tested against three literal fences (the two from `memory-consolidation.md` and the one from `default-pipeline.md`), so a future loosening of rule 1 cannot be achieved by weakening the classifier unnoticed.
3. `test_excluded_roots_are_declared_not_forgotten` — every `.md` under `src/prompts/` is in exactly one of `INSTALLED_SOURCE_ROOTS`, `EXCLUDED_SAMPLE_ROOTS`, or an explicit `NON_PLAYBOOK_PROMPTS` tuple. A new prompt directory fails the suite until someone classifies it.
4. `test_shipped_sources_declare_every_identifier` — for each installed source, every command name, event name, and `profile_id` the reviewed artifact emits appears verbatim in the Markdown as a backticked identifier or a frontmatter value (spec, "Metadata ownership": "An external identifier absent from the source is a compile error, never a model guess"). This is the assertion that keeps the prose rewrite honest — prose that drops `gate_create` cannot compile to a graph that calls it.

**Verify:** `aq test tests/test_shipped_playbook_sources.py -q` → 1 and 4 fail, 2 and 3 pass.

#### T-5 — rewrite `src/prompts/default_playbooks/default-pipeline.md`

Replace lines 41–224 with normative prose. The frontmatter is unchanged except as Package 0/2 require. The five rules keep their ids (`per-task-review`, `per-branch-final-review`, `spec-ingest-on-approve`, `proposal-ready-gate`, `commit-on-gate-resolve`) — `tests/test_default_pipeline.py:45-52` asserts that set, `src/playbooks/routing.py:14-17` names two *superseded* ids, and the dedup key `review:task:<task_id>` is mirrored in `src/doctor/integration_checks.py:53`.

Representative excerpt of the rewritten form (this is the shape, not a placeholder — the real file carries all five rules at this level of specificity):

```markdown
## Rule: per-task-review

Trigger: `task.completed`.
Guard: the completed task has a non-empty `branch_name`.

1. Call `ensure_task` with dedup key `review:task:{event.task_id}`, profile
   `reviewer`, in the event's project. Title it `Review: {event.title}`. The
   description must give the reviewer the task id, the branch name, the PR URL,
   and this instruction: read the diff and either approve by closing this task
   with a summary, or reject by calling `reopen_with_feedback` on the reviewed
   task and then closing this task. Bind the result as `review`.
2. Call `add_dependency` to record `review.task_id` as `discovered-from` the
   completed task. A failure here is not fatal to the rule.
3. Call `get_downstream_tasks` for the completed task and bind the result as
   `downstream`.
4. For each `dep` in `downstream.tasks`, call `gate_create` with gate type
   `task`, awaiting `review.task_id`, with `dep.id` as the sole waiter, so no
   dependent starts before the review completes.

Any failure ends the rule without failing the others.
```

Sequencing constraint: this task **must not** land before Package 2's compiler can accept a prose-only pipeline source, because `tests/test_default_pipeline.py:34` calls `compile_pipeline(md)` on the shipped file and asserts `r.success`. That V1 test is updated in the same commit to assert the *V1 compiler now reports* `embedded_action_block: absent` rather than a compiled graph — the V1 pipeline compiler stops being able to compile the shipped source, which is intentional and is why this commit is gated behind the reviewed fixture (Commit 3) before anything can execute the default pipeline again.

> **Operational hazard, called out explicitly.** Between Commit 2 and Commit 3 the default pipeline has no executable source under either runtime. This window must not exist on a running fleet. The two commits ship together in one PR, and §13 makes reverting Commit 2 the rollback for both.

#### T-6 — dispositions for the remaining shipped and sample sources

- `default-assignment-routing.md`, `memory-consolidation.md`, `reflection.md`: **no rewrite**. Add a one-line note to each file's prose confirming it is already a prose authoring source. Record the decision in each fixture's `review.md` (§5.3).
- `reflection.md` scope conflict (§1.2): the decision is to **correct the frontmatter to match the install path** — set `scope: agent-type:claude-opus` — because the file is only ever installed under `agent-types/claude-opus/` and there is no `coding` agent-type directory in `src/prompts/default_agent_type_playbooks/`. `id: coding-reflection` stays (it is referenced by `tests/test_default_agent_type_playbooks.py` and by any existing vault). The alternative — renaming the directory to `coding/` — would orphan every already-installed vault copy. Record this in `tests/fixtures/playbooks/v2/coding-reflection/review.md` under `## Compiler questions and decisions`.
- `src/prompts/example_playbooks/` and `src/prompts/default_rules/`: add `README.md` to each stating they are non-installed examples, unreferenced by code, excluded from the migration corpus, and not covered by any reviewed artifact. File a separate task (not this package) to decide whether they should be deleted or converted — Package 6 does not own dead-content cleanup.

**Verify:**
```bash
aq test tests/test_shipped_playbook_sources.py -q          # all pass
aq test tests/test_default_pipeline.py tests/test_default_agent_type_playbooks.py -q
aq test tests/test_default_pipeline_spec_and_proposal.py tests/test_review_pipeline_rules.py -q
```

---

### 5.3 Commit 3 — `build: add reviewed v2 artifact fixtures`

#### T-7 — `tests/test_default_playbook_v2_artifacts.py` (red)

Parametrised over the four playbook ids. Failing assertions:

1. `test_fixture_directory_complete` — each of `source.md`, `artifact.json`, `artifact.sha256`, `review.md` exists.
2. `test_artifact_validates_against_strict_model` — `PlaybookDefinition.model_validate_json(artifact.json)` succeeds; `extra="forbid"` is exercised by a negative case that injects an unknown key and expects `ValidationError`.
3. `test_artifact_bytes_are_canonical` — `sha256(artifact.json) == artifact.sha256`, and re-serialising the parsed model through `ArtifactStore`'s canonical encoder reproduces the same bytes exactly. A trailing-newline difference is a failure, not a nit.
4. `test_source_digest_matches` — `sha256(source.md) == definition.compiled_from.source_digest`.
5. `test_source_matches_live_shipped_file` — `source.md` is byte-identical to the live shipped Markdown. Failure message: "shipped Markdown changed since review — recompile, re-review, and update the fixture" (§3.4).
6. `test_review_record_complete` — `review.md` frontmatter has every locked key (§3.4), `decision == "approved"`, `artifact_sha256` matches `artifact.sha256`, and the five required `## ` sections are present with exact headings.
7. `test_every_command_resolves` — every command the artifact invokes is registered in the Package 1 contract registry, and the artifact's `compiled_against.commands[name]` equals that contract's current fingerprint.
8. `test_every_profile_resolves` — every `profile_id` in the artifact parses from `src/profiles/defaults/`; for `default-pipeline` that is `reviewer`, `final-reviewer`, `spec-ingest`.
9. `test_pipeline_rule_set_unchanged` — the `default-pipeline` artifact's rule ids equal the set pinned by `tests/test_default_pipeline.py`, and neither superseded id (`task-created-routing`, `worker-filed-triage`) appears.
10. `test_review_dedup_key_matches_doctor` — the `ensure_task` step in `per-task-review` uses a dedup key template that renders to `src/doctor/integration_checks._review_dedup_key(task_id)` for a sample id. This is the assertion that stops a prose rewrite from silently disarming `integration.unreviewed_prs` (§2).
11. `test_no_rejected_fixture_is_activatable` — a fixture whose `review.md` says `decision: rejected` is excluded from the activation set (asserted with a synthetic rejected fixture in `tmp_path`, not by shipping one).

**Verify:** `aq test tests/test_default_playbook_v2_artifacts.py -q` → all fail on missing fixtures.

#### T-8 — produce and check in the fixtures

For each shipped playbook, on a workstation with a compiler-capable daemon:

```bash
aq playbook compile --path vault/system/playbooks/default-pipeline.md --propose   # Package 2 surface
# review the semantic diff, capabilities, profiles, budgets and transitions in the
# dashboard's artifact-diff view (Package 5), resolving every compiler question
aq playbook artifact show --sha256 <hash> --canonical > tests/fixtures/playbooks/v2/default-pipeline/artifact.json
```

> The two `aq playbook` verbs above (`compile --propose`, `artifact show --canonical`) are **Package 2 and Package 3 surfaces that do not exist at drafting time**. Confirm their actual names during the §3.8 reconciliation commit and correct this block; do not invent a local equivalent.

Then write `review.md` by hand — it is the human record, and generating it defeats its purpose. Copy the live Markdown to `source.md` and the hash to `artifact.sha256`.

**Determinism note.** Compilation is LLM-driven and not reproducible. The fixture is the *approved recording*; CI validates it, never regenerates it. When a compiler change alters output, the release check (§5.5) fails and a human repeats this procedure. That is the intended cost of "the system never auto-activates an LLM rebuild" (spec, "Compatibility, rebuild, and failure behavior").

#### T-9 — capability audit

`src/playbooks/migration.py::audit_capabilities(definition, policy) -> tuple[CapabilityFinding, ...]`, plus assertions in the same suite:

12. `test_no_wildcard_capability` — no artifact and no referenced profile contains `"*"` in any namespace (roadmap §11 Safety).
13. `test_artifact_capabilities_subset_of_profile` — for every step, the capabilities the step's commands require are a subset of the step profile's `CapabilityPolicy.aq_commands`. A superset is a hard failure naming step, command, and profile.
14. `test_review_lists_every_required_capability` — every capability from (13) appears in `review.md.capabilities_granted`. One direction only (§4.1): the review may not be used to add capabilities.
15. `test_capabilities_granted_unused_by_src` — `grep -rn "capabilities_granted" src/` is empty.

**Verify:**
```bash
aq test tests/test_default_playbook_v2_artifacts.py -q
aq test tests/test_playbook_migration_inventory.py -q      # dispositions flip to "ready" for all four
ruff check tests/test_default_playbook_v2_artifacts.py src/playbooks/migration.py
```

---

### 5.4 Commit 4 — `test: compare v1 and v2 shadow decisions`

#### T-10 — `tests/test_playbook_shadow_parity.py` (red)

The harness. Both arms are driven from one event corpus (§8.3).

- **V1 arm:** `tests/conftest.py:306` `PipelineEngine` with a recording `CommandHandler` double. `PipelineEngine` already reproduces the runtime's rule selection (`_eval_pipeline_when`), `event.task` hydration and `event_id` dedup, so the parity result reflects the shipped dispatch semantics rather than a second implementation. It is driven from the **pre-rewrite** pipeline source, pinned at `tests/fixtures/playbooks/v1/default-pipeline.md` (a copy of the file as it stood at `origin/main` `c7ba28d7`), because the live source no longer contains a graph after Commit 2.
- **V2 arm:** `PlaybookEngine.dispatch_event(event, principal, ExecutionMode.shadow)` against the reviewed artifact loaded from the fixture.

Failing assertions:

1. `test_neither_arm_executes_commands` (§4.4) — a handler double whose `execute` raises `AssertionError` still yields two complete observations.
2. `test_identical_rule_selection` — for every corpus event, `v1.rules_selected == v2.rules_selected`. This is the single most important assertion in the package: it proves the prose rewrite did not change which rules fire.
3. `test_identical_command_sequence` — `v1.commands == v2.commands` after canonicalisation (§3.5.1), for every deterministic (pipeline) playbook.
4. `test_expected_differences_are_registered` — every `ParityFinding` classified `expected_v2_semantics` carries a `rationale_id` present in `EXPECTED_DIFFERENCES`.
5. `test_no_unused_rationales` — every key in `EXPECTED_DIFFERENCES` is exercised by at least one corpus observation. A stale waiver fails the suite.
6. `test_no_unexplained_findings` — `[f for f in findings if f.classification == "unexplained"] == []`.
7. `test_authorization_differences_are_never_waivable` — `compare()` raises `ValueError` when asked to classify an `authorization` finding as `expected_v2_semantics` (§3.5).
8. `test_llm_playbooks_get_structural_parity_only` — for `memory-consolidation` and `coding-reflection`, the harness compares triggers, rule ids, profiles, budgets, output schemas and transitions, and asserts `commands` comparison is explicitly skipped with a recorded coverage note (§4.5).

**Verify:** `aq test tests/test_playbook_shadow_parity.py -q` → all fail (no harness).

#### T-11 — the event corpus

`tests/fixtures/playbooks/v2/events/*.json`, one file per event, each a realistic bus payload. Coverage rule, asserted by `test_corpus_covers_every_guard_outcome`: for every rule in every deterministic shipped playbook, the corpus contains at least one event where the guard passes and one where it fails. For `default-pipeline` that is ten events minimum; §8.3 lists the concrete set.

#### T-12 — classifier and recorded report

Implement `compare()` and `EXPECTED_DIFFERENCES` per §3.5, and have the suite write `tests/fixtures/playbooks/v2/parity-report.json` (checked in, refreshed by a `--parity-record` flag on the suite, never rewritten silently in CI — a mismatch between the committed report and a fresh run fails `test_parity_report_current`).

**Verify:**
```bash
aq test tests/test_playbook_shadow_parity.py -q
aq test tests/test_pipeline_runner.py tests/test_pipeline_dispatch.py tests/test_review_pipeline_rules.py -q
ruff check tests/test_playbook_shadow_parity.py
```

---

### 5.5 Commit 5 — `ci: require artifact rebuilds after command contract changes`

#### T-13 — `tests/test_playbook_contract_release_check.py` (red)

1. `test_clean_tree_passes` — with unchanged contracts, `release_check()` returns `{"success": True, "stale": []}`.
2. `test_changed_fingerprint_blocks_readiness` — the **intentional contract-change fixture** the roadmap requires: monkeypatch one registered contract (`ensure_task`) so its execution fingerprint differs from the value in `default-pipeline`'s `compiled_against.commands`, then assert `release_check()` returns `success: False` and names `default-pipeline` and `ensure_task`. Assert the inventory disposition for that playbook becomes `question_required` with reason `stale_contract`, and that a new run is refused (spec: "A mismatch marks the activation `needs_rebuild` and prevents new runs").
3. `test_presentation_change_does_not_trip_it` — changing a contract's human-facing label alone leaves the fingerprint and the check unchanged (roadmap §4: "Presentation-only labels do not affect the execution fingerprint").
4. `test_disabled_playbooks_do_not_block` — a stale artifact belonging to a disabled or acknowledged playbook does not fail the check.
5. `test_check_is_offline` — `release_check()` performs no network and no LLM call; it compares checked-in fixtures against the in-process registry.

#### T-14 — `playbook_release_check` and `playbook_cutover_report`

Implement both in `src/commands/playbook_migration_commands.py` per §3.6 and §3.7. `release_check` compares, for every enabled activation *and* every checked-in fixture, the artifact's `compiled_against.commands` and `compiled_against.profiles` against the live registries; it reports `stale` entries with the changed dependency named.

#### T-15 — CI and doctor wiring

- No new CI job. `tests/test_playbook_contract_release_check.py` runs in the existing `default` matrix suite (`.github/workflows/tests.yml:24-25`), which is the only place a contract change and the fixtures are both present.
- Add `playbooks.stale_artifacts` to `src/doctor/` following the shape of `src/doctor/pool_checks.py` (private `_find_*`/`_check_*` pair, factory, `CHECKS` snapshot, `run_check` wrapper) so `aq doctor` reports the same condition on a live daemon. Register it the way `src/doctor/integration_checks.py` is registered — note the recent fix `10f2b2d2 "fix: register resource and integration doctor checks"`; an unregistered check is a check that never runs. Assert registration in `tests/test_doctor.py`.
- Add `docs/guides/playbook-v2-cutover-report-template.md` rendering §3.7's schema, with a signature block (`Approved for cutover by / date / commit sha`).

#### T-16 — pending-event policy

Config, under the existing `playbooks:` section (`src/config.py:837` `PlaybooksConfig`):

```yaml
playbooks:
  v2:
    pending_events:
      retention_days: 7            # owned by Package 3 (spec default); Package 6 does not change it
      max_per_activation: 500      # Package 6
      on_overflow: drop_oldest     # Package 6: drop_oldest | reject_new
      replay_on_activation: manual # Package 6: manual | automatic
```

`replay_on_activation: automatic` is **refused by config validation** for any activation whose health is `question_required` — an unreviewed playbook may not auto-consume a backlog. `PlaybooksConfig.validate()` returns a `ConfigError` for that combination.

Implement `playbook_pending_event_list|replay|discard` (§3.6). Overflow and expiry both write an auditable dropped-event record; the cutover report's `pending_events` block reads from the same rows.

Assertions, in `tests/test_playbook_pending_events_policy.py`:

6. `test_overflow_drop_oldest_is_audited` — exceeding `max_per_activation` drops the oldest and records the drop with playbook id, event id, and reason.
7. `test_replay_re_evaluates_guards` — a held event whose guard would now fail does not run (§4.3).
8. `test_replay_strips_principal_fields` — a held payload containing `_principal`/`_capabilities` replays with them removed.
9. `test_automatic_replay_rejected_for_question_required` — config validation error, named playbook.
10. `test_discard_requires_reason` — same 12-character floor as acknowledgement.

**Verify:**
```bash
aq test tests/test_playbook_contract_release_check.py tests/test_playbook_pending_events_policy.py -q
aq test tests/test_doctor.py -q -k "playbook or register"
aq playbook release-check --json
aq playbook cutover-report --json
ruff check src/commands/playbook_migration_commands.py src/doctor tests/test_playbook_contract_release_check.py tests/test_playbook_pending_events_policy.py
```

---
## 6. Storage — Alembic

Package 6 adds **one table and no column changes**. Everything else it reads (`playbook_artifacts`, `playbook_activations`, `playbook_pending_events`) is Package 3's.

`src/database/tables.py`:

```python
playbook_migration_acks = Table(
    "playbook_migration_acks",
    metadata,
    Column("playbook_id", Text, primary_key=True),
    Column("scope", Text, primary_key=True),                 # system|supervisor|agent_type|project
    Column("scope_identifier", Text, primary_key=True, server_default="''"),
    Column("source_sha256", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("acknowledged_by", Text, nullable=False),
    Column("acknowledged_at", Float, nullable=False),
    CheckConstraint("length(reason) >= 12", name="ck_playbook_migration_acks_reason"),
    Index("idx_playbook_migration_acks_source", "source_sha256"),
)
```

`scope_identifier` is part of the primary key and therefore `NOT NULL`; system- and supervisor-scoped rows store `''`, not `NULL`. This is the same sentinel choice `workspace_kinds` made (`migrations/versions/7cdb4618fd0b_add_workspaces_v2_schema.py`, `'__system__'`) and for the same reason: a nullable PK column is illegal on PostgreSQL.

Revision, generated with `alembic revision --autogenerate -m "add playbook_migration_acks"` and then reviewed by hand:

- **upgrade:** `op.create_table(...)` plus `op.create_index(...)`. No data migration — an empty table is the correct initial state, because "no playbook has been acknowledged" is true on every existing install.
- **downgrade:** `op.drop_index(...)`, `op.drop_table("playbook_migration_acks")`. Dropping the table loses acknowledgement history; that is acceptable and is stated in the revision docstring, because the report that consumed them (§3.7) is regenerated from the inventory, and reverting Package 6 means the fleet is not cutting over.

### SQLite and PostgreSQL

- `Float` for `acknowledged_at` matches every other timestamp in `src/database/tables.py` (`REAL` on SQLite, `DOUBLE PRECISION` on Postgres). Do not introduce a `DateTime` here; it would be the only one in the schema.
- `CheckConstraint("length(reason) >= 12")` — `length()` exists on both backends with the same semantics for text. The command boundary also validates it (§4.2), so the constraint is defence in depth, not the only check.
- No `server_default` on `reason`/`acknowledged_by`: they are always supplied.
- The composite three-column PK is portable; no partial index is needed (unlike `uq_playbook_runs_pb_event`, which needs `sqlite_where`/`postgresql_where`).
- Per the project's standing rule that PostgreSQL is production, run the migration check on both: `AGENT_QUEUE_DB_URL=sqlite+aiosqlite:///$TMPDIR/pkg6.db alembic upgrade head` and the same against `POSTGRES_TEST_DSN`, then `alembic downgrade -1` on each (T-3 assertion 14).

---

## 7. Commit sequence

Matches the roadmap's five commits exactly.

1. `feat: inventory v1 playbooks and migration readiness` — T-1, T-2, T-3
2. `refactor: rewrite shipped playbooks as prose authoring sources` — T-4, T-5, T-6
3. `build: add reviewed v2 artifact fixtures` — T-7, T-8, T-9
4. `test: compare v1 and v2 shadow decisions` — T-10, T-11, T-12
5. `ci: require artifact rebuilds after command contract changes` — T-13, T-14, T-15, T-16

**Commits 2 and 3 ship in one PR** (§5.2 operational hazard): between them the default pipeline has no executable source. Commits 1, 4 and 5 are independently revertible.

Prepend the reconciliation commit from §3.8 if any earlier package deviated: `docs: reconcile package 6 plan against packages 0-5`.

---

## 8. Fixture data

### 8.1 `tests/fixtures/playbooks/v2/default-pipeline/review.md` (representative, abridged body)

```markdown
---
playbook_id: default-pipeline
artifact_sha256: "sha256:4f1c0a9d7e2b8c5316ad4e0f97b2c8d1a63f5e04b9c27d8ea1350f6b4c9d2e78"
source_sha256: "sha256:9b2e7d41c8a05f36e19d4b7c2a86f051d3e94c7b0a25f8d6139e4b7c05a2f8d3"
contract_fingerprint: "sha256:c07a13be5d92f4681ca370e5b8d24f19a6c53b0d7e148f92a3b6c05d17e94f28"
reviewed_by: "Jack Kern <jack.w.kern@gmail.com>"
reviewed_at: "2026-09-14"
decision: approved
questions_resolved: 3
capabilities_granted:
  aq_commands: [ensure_task, add_dependency, get_downstream_tasks, gate_create, task_batch_commit]
  harness_tools: []
  plugin_tools: []
profiles_referenced: [reviewer, final-reviewer, spec-ingest]
---

## Compiler questions and decisions

1. **`per-task-review` step 2 failure handling.** The prose says a failed
   `add_dependency` "is not fatal to the rule". The compiler asked whether the
   rule should continue to step 3 or terminate. **Decision: continue.** The V1
   graph hopped `link-discovered-from.on_failure -> done`, terminating; the
   `discovered-from` edge is provenance only, and skipping the downstream gates
   because provenance failed is worse than a missing edge. This is a
   deliberate behaviour change and is listed under Accepted behaviour
   differences and in `EXPECTED_DIFFERENCES` as a per-observation finding.
2. **`per-branch-final-review` re-`ensure_task` of the per-task review.** The
   V1 graph calls `ensure_task` with the same `review:task:` dedup key a second
   time. **Decision: keep it.** The dedup key makes it idempotent and it is
   what lets the branch rule wire `blocks` even when the two rules interleave.
3. **`gate.resolved` filter.** Frontmatter filters to `gate_type: human`; the
   compiler asked whether the rule should also guard on the payload. **Decision:
   no additional guard** — the trigger filter is server-owned and sufficient.

## Semantic diff versus the V1 graph

Rule ids, trigger events, guards, command sequence and dedup keys are unchanged.
Structural differences: one run per rule instead of one per event
(`run-per-rule`); rule failures no longer abort sibling rules
(`rule-failure-isolation`); `outputs.dep` becomes a scoped loop binding
(`loop-frame-shape`); `{{event.task.pr_url}}` is now a typed reference that must
be definitely assigned, where V1 substituted an empty string
(`unassigned-ref-rejected`) — the `per-branch-final-review` guard already
requires `pr_url` to be truthy, so no live path changes.

## Capabilities and why each is needed
…
## AI profiles, budgets, and output schemas
…
## Accepted behaviour differences
…
```

### 8.2 `tests/fixtures/playbooks/v2/default-pipeline/artifact.json` (shape)

Follows the spec's canonical artifact ("Canonical V2 artifact"). The abridged head, showing the fields the tests assert on:

```json
{
  "schema_version": 2,
  "id": "default-pipeline",
  "version": 6,
  "scope": {"type": "system"},
  "source_hash": "sha256:9b2e7d41…",
  "compiled_at": "2026-09-14T18:22:07Z",
  "purpose": "routine",
  "rules": [
    {"id": "per-task-review", "on": "task.completed",
     "guard": {"op": "truthy", "ref": {"kind": "event", "path": ["task", "branch_name"]}},
     "entry": "ensure-review"}
  ],
  "steps": {
    "ensure-review": {
      "kind": "command",
      "rule_id": "per-task-review",
      "command": "ensure_task",
      "args": {
        "project_id": {"kind": "event", "path": ["project_id"]},
        "dedup_key": {"kind": "template", "parts": [
          {"literal": "review:task:"},
          {"kind": "event", "path": ["task_id"]}]},
        "profile_id": {"kind": "literal", "value": "reviewer"}
      },
      "output": {"binding": "review", "schema_ref": "ensure_task.result"},
      "transitions": [
        {"on": "success", "to": "link-discovered-from"},
        {"on": "failure", "to": "done"}
      ],
      "source": {"path": "system/playbooks/default-pipeline.md", "line": 46}
    }
  },
  "compiled_against": {
    "commands": {"ensure_task": "sha256:…", "add_dependency": "sha256:…",
                 "get_downstream_tasks": "sha256:…", "gate_create": "sha256:…"},
    "profiles": {"reviewer": "sha256:…", "final-reviewer": "sha256:…",
                 "spec-ingest": "sha256:…"}
  }
}
```

The exact expression and step encodings are Package 2's; this plan asserts only the fields it names in §5.3 (`schema_version`, `id`, `rules[].id`, `steps[].command`, `steps[].source`, `compiled_against`).

### 8.3 Event corpus — `tests/fixtures/playbooks/v2/events/`

Ten files, one per `(rule, guard-outcome)` pair for `default-pipeline`, plus three for the other playbooks' triggers. Each is a realistic bus payload as `src/orchestrator/core.py:847-860` hydrates it (slim payload plus the `task` row).

| File | Event | Exercises |
|---|---|---|
| `task-completed-with-branch.json` | `task.completed`, task has `branch_name`, no `pr_url` | `per-task-review` fires; `per-branch-final-review` guard fails |
| `task-completed-with-branch-and-pr.json` | both set | **both** rules fire — the `run-per-rule` and `rule-failure-isolation` observation |
| `task-completed-no-branch.json` | neither set | both guards fail; both arms must produce zero commands |
| `task-completed-with-downstream.json` | branch set, two downstream dependents | the `gate-downstream` `for_each` — `loop-frame-shape` |
| `task-completed-no-downstream.json` | branch set, zero dependents | empty-collection loop; both arms emit no `gate_create` |
| `task-completed-add-dependency-fails.json` | branch set; the recording double is primed to fail `add_dependency` | the §8.1 decision 1 behaviour change, per-observation |
| `spec-approved.json` | `spec.approved` with `spec_path` | `spec-ingest-on-approve` |
| `proposal-ready.json` | `proposal.ready` with `proposal_id` | `proposal-ready-gate` |
| `gate-resolved-human.json` | `gate.resolved`, `gate_type: human` | `commit-on-gate-resolve` fires |
| `gate-resolved-task.json` | `gate.resolved`, `gate_type: task` | trigger filter rejects — zero rules in both arms |
| `assignment-route-requested.json` | `assignment.route.requested` | `default-assignment-routing`, structural only |
| `timer-24h.json` | `timer.24h` | `memory-consolidation`, structural only |
| `task-failed.json` | `task.failed` | `coding-reflection`, structural only |

`task-completed-with-branch.json`:

```json
{
  "type": "task.completed",
  "_event_type": "task.completed",
  "event_id": "evt-parity-0001",
  "project_id": "agent-queue",
  "task_id": "solid-harbor.12",
  "title": "Add pool carve-out reconciliation",
  "task": {
    "id": "solid-harbor.12",
    "project_id": "agent-queue",
    "title": "Add pool carve-out reconciliation",
    "status": "COMPLETED",
    "branch_name": "aq/solid-harbor.12",
    "pr_url": null,
    "dedup_key": null,
    "parent_task_id": "solid-harbor"
  }
}
```

### 8.4 Inventory fixture for the negative cases

`tests/fixtures/playbooks/v1/` holds three synthetic sources used by T-1 (they are never installed):

- `duplicate-a.md` / `duplicate-b.md` — both `id: dup`, different paths, for assertion 6.
- `disabled-by-author.md` — `enabled: false`, for assertion 7.
- `default-pipeline.md` — a byte copy of the shipped file at `origin/main` `c7ba28d7`, i.e. **with** its JSON graph, used as the V1 arm of the parity harness after Commit 2 removes the graph from the live source (§5.4 T-10).

---

## 9. API request/response examples

### 9.1 `POST /api/execute` — `playbook_migration_inventory`

```json
{"command": "playbook_migration_inventory", "args": {}}
```

```json
{
  "success": true,
  "generated_at": 1788412345.882,
  "contract_fingerprint": "sha256:c07a13be…",
  "counts": {"ready": 3, "question_required": 1, "invalid": 0, "disabled": 0},
  "entries": [
    {
      "playbook_id": "coding-reflection",
      "scope": "agent_type",
      "scope_identifier": "claude-opus",
      "source": {
        "vault_rel_path": "agent-types/claude-opus/playbooks/reflection.md",
        "bundled_rel_path": "src/prompts/default_agent_type_playbooks/claude-opus/reflection.md",
        "source_sha256": "sha256:71c9…"
      },
      "v1_kind": "", "v1_version": 3, "v1_enabled": true,
      "disposition": "question_required",
      "reasons": [
        {"code": "scope_conflict",
         "message": "frontmatter declares scope 'agent-type:coding' but the install path resolves to agent_type 'claude-opus'",
         "source_line": 6}
      ],
      "artifact": null,
      "activation_health": null,
      "has_embedded_action_block": false,
      "acknowledged_by": null,
      "acknowledged_at": null
    }
  ]
}
```

### 9.2 `playbook_migration_acknowledge` — spoofing attempt is inert

```json
{"command": "playbook_migration_acknowledge",
 "args": {"playbook_id": "legacy-project-playbook",
          "reason": "owner left; superseded by the default pipeline",
          "acknowledged_by": "root"}}
```

```json
{"success": true,
 "playbook_id": "legacy-project-playbook",
 "acknowledged_by": "session:agent-5231de48bbb5",
 "acknowledged_at": 1788412401.5,
 "source_sha256": "sha256:2ad4…",
 "note": "acknowledgement is invalidated if the source markdown changes"}
```

The supplied `acknowledged_by` is discarded; the stored value is the principal's (§4.2).

### 9.3 `playbook_release_check` — a changed contract blocks readiness

```json
{"success": false,
 "contract_fingerprint": "sha256:e91b77…",
 "stale": [
   {"playbook_id": "default-pipeline", "scope": "system", "scope_identifier": null,
    "artifact_sha256": "sha256:4f1c0a9d…",
    "changed": [{"kind": "command", "name": "ensure_task",
                 "artifact_fingerprint": "sha256:aa10…",
                 "registry_fingerprint": "sha256:bb42…"}],
    "activation_health": "stale_contract"}],
 "message": "1 enabled playbook must be rebuilt and reviewed before release: default-pipeline (ensure_task)"}
```

### 9.4 Error shape — reason too short

```json
{"success": false,
 "error": "reason must be at least 12 characters; an acknowledgement is a recorded waiver",
 "code": "invalid_argument", "field": "reason"}
```

---

## 10. Observability and operator failure behavior

Every log line and report row in this package carries `playbook_id`, `scope`, `scope_identifier`, `artifact_sha256` (when known) and `contract_fingerprint`, matching roadmap §10.

| Condition | Operator sees | What they do |
|---|---|---|
| A shipped source changed since review | `test_source_matches_live_shipped_file` fails in CI with "recompile, re-review, and update the fixture" | Repeat §5.3 T-8 |
| A command contract changed | `playbook_release_check` fails; `aq doctor` reports `playbooks.stale_artifacts`; the affected activation is `stale_contract` and refuses new runs | Rebuild and review the affected playbooks |
| A project playbook cannot compile | `aq playbook migration-inventory` shows `question_required` with a `MigrationReason` naming the source line | Fix the Markdown, or acknowledge it disabled with a reason |
| Events pile up behind a stale activation | `aq playbook pending-event-list`; `cutover_report.pending_events.oldest_age_seconds` | Rebuild, then replay or discard each event explicitly |
| Pending events exceed `max_per_activation` | An audited drop record per event; the count appears in the cutover report | Raise the cap or clear the backlog — never silent |
| The parity suite reports an unexplained difference | `test_no_unexplained_findings` fails, naming field, rule, and both values | Fix the playbook, or record a rationale **and** get it reviewed — an authorization difference may never be rationalised (§3.5) |
| A stale `EXPECTED_DIFFERENCES` waiver | `test_no_unused_rationales` fails | Delete the waiver |

Failure behaviour is uniformly **stop and report**. No surface in this package retries, guesses a disposition, or auto-activates. `build_inventory` cannot write; `release_check` cannot rebuild; replay is one event at a time.

---

## 11. Verification

### Per-package required commands (roadmap §5, reconciled per §2)

```bash
# the roadmap's two named suites, at their live-tree paths
aq test tests/test_playbook_migration_inventory.py tests/test_default_playbook_v2_artifacts.py -q

# full default-playbook compiler fixture suite
aq test tests/test_default_pipeline.py tests/test_default_pipeline_spec_and_proposal.py \
        tests/test_default_agent_type_playbooks.py tests/test_pipeline_compiler.py \
        tests/test_assignment_playbook_compiler.py tests/test_playbook_compiler_scope.py \
        tests/test_playbook_install_compiled.py -q

# deterministic shadow-parity suite named by this plan
aq test tests/test_playbook_shadow_parity.py -q

# capability audit over every activated artifact
aq test tests/test_default_playbook_v2_artifacts.py -q -k "capabilit or wildcard"

# clean release-check run with unchanged contracts, and the intentional
# contract-change fixture proving stale artifacts block readiness
aq test tests/test_playbook_contract_release_check.py -q

# shipped-source scan and pending-event policy
aq test tests/test_shipped_playbook_sources.py tests/test_playbook_pending_events_policy.py -q

ruff check src/playbooks/migration.py src/commands/playbook_migration_commands.py \
           src/database/queries/playbook_migration_queries.py src/doctor \
           tests/test_playbook_migration_inventory.py tests/test_default_playbook_v2_artifacts.py \
           tests/test_shipped_playbook_sources.py tests/test_playbook_shadow_parity.py \
           tests/test_playbook_contract_release_check.py tests/test_playbook_pending_events_policy.py
```

### Area suite, once, before closing

```bash
aq test tests/test_playbook*.py tests/test_pipeline*.py tests/test_default_pipeline*.py \
        tests/test_review_pipeline*.py tests/test_routing_admission.py tests/test_doctor.py -q
```

Per the supervisor's standing guidance, do not run the whole repository suite during the package; CI's `default` matrix arm covers it.

### Migration

```bash
AGENT_QUEUE_DB_URL=sqlite+aiosqlite:///$TMPDIR/pkg6.db alembic upgrade head
AGENT_QUEUE_DB_URL=sqlite+aiosqlite:///$TMPDIR/pkg6.db alembic downgrade -1
AGENT_QUEUE_DB_URL="$POSTGRES_TEST_DSN" alembic upgrade head
AGENT_QUEUE_DB_URL="$POSTGRES_TEST_DSN" alembic downgrade -1
aq test tests/test_database.py -q
```

### End-to-end

```bash
scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh    # unchanged: no claim/pool/formula/hierarchy surface moves
aq playbook migration-inventory --json
aq playbook release-check --json
aq playbook cutover-report --json
aq doctor --json | jq '.checks[] | select(.name | startswith("playbooks."))'
```

### Client regeneration

`playbook_migration_*`, `playbook_release_check`, `playbook_cutover_report` and `playbook_pending_event_*` add typed routes via `src/api/codegen.py`, so both clients regenerate:

```bash
scripts/regenerate-api-client.sh --from-file
scripts/regenerate-ts-client.sh --from-file
```

No dashboard component changes ship in this package (the review UI is Package 5's), so `npm run typecheck` and `npm run build` are run once to prove the regenerated client still compiles, and no Vitest suite is added.

---

## 12. Mapping to the package exit gate

> **Exit gate:** Every enabled playbook has one reviewed V2 artifact compatible with the current command contracts. Every non-ready playbook has a visible reason and operator decision. Shadow comparison has no unexplained behavior or authorization differences.

| Gate clause | Proof |
|---|---|
| **every enabled playbook** — the corpus is complete | `tests/test_playbook_migration_inventory.py::test_enumerates_all_four_shipped_playbooks` (both install roots, §1.2) + `test_shipped_playbook_sources.py::test_excluded_roots_are_declared_not_forgotten` (no prompt directory is silently outside the corpus) |
| **has one reviewed V2 artifact** | `tests/test_default_playbook_v2_artifacts.py` 1–6: fixture completeness, strict-model validation, canonical bytes, source digest, live-source match, and a complete `review.md` with `decision: approved` |
| **compatible with the current command contracts** | assertions 7–8 (every command and profile resolves; `compiled_against` equals the live fingerprints) + `tests/test_playbook_contract_release_check.py::test_changed_fingerprint_blocks_readiness` |
| **every non-ready playbook has a visible reason** | closed `REASON_CODES` with `ValueError` on anything else; `MigrationReason.source_line`; `test_embedded_action_block_is_question_required`, `test_scope_conflict_detected`, `test_duplicate_id_is_invalid` |
| **…and an operator decision** | `playbook_migration_acknowledge` with an attributed, ≥12-character, source-hash-scoped reason; `test_disabled_requires_ack_unless_frontmatter`; `test_blocking_excludes_acknowledged`; every ack surfaces in `cutover_report.acknowledged_disabled` |
| **shadow comparison has no unexplained behavior differences** | `tests/test_playbook_shadow_parity.py::test_no_unexplained_findings` over the §8.3 corpus, with `test_corpus_covers_every_guard_outcome` bounding coverage and `test_no_unused_rationales` preventing waiver rot |
| **…or authorization differences** | `test_authorization_differences_are_never_waivable` — `compare()` raises rather than classifying an `authorization` finding as expected (§3.5) |
| **never batch-activate automatically** | no code path in this package activates; `build_inventory` is proven write-free by `test_inventory_is_read_only`; activation is the human flow of §5.3 T-8 through Package 5's UI |
| **pending-event policy** | `tests/test_playbook_pending_events_policy.py` 6–10, and `PlaybooksConfig.validate()` refusing `replay_on_activation: automatic` for `question_required` |
| **release check** | `tests/test_playbook_contract_release_check.py` (all five) + the `playbooks.stale_artifacts` doctor check, registration asserted in `tests/test_doctor.py` |
| **cutover report** | `playbook_cutover_report` returning §3.7's schema, rendered by `docs/guides/playbook-v2-cutover-report-template.md`, including `active_v1_runs` read from the live `playbook_runs` table |

Milestone **M6 — Fleet ready** is claimed only when every command in §11 passes and `aq playbook cutover-report --json` reports `blocking_reasons: []`.

---

## 13. Rollback boundary

The roadmap's boundary for this package: "No production entry point has switched yet. Reviewed artifacts and migration reports can remain stored while V1 continues to execute." This plan preserves it:

- **Nothing in Package 6 dispatches an event, starts a run, or activates an artifact.** V1 remains the only execution path.
- **Commits 1, 4 and 5 are independently revertible.** Commit 1 adds one table and three read-mostly commands; reverting the code leaves an unused table (no downgrade needed). Commit 4 is tests and fixtures only. Commit 5 adds a check and operator commands.
- **Commits 2 and 3 revert together.** Commit 2 removes the only executable form of the default pipeline; Commit 3 supplies its replacement. Reverting Commit 2 alone restores V1 execution; reverting Commit 3 alone leaves the fleet with no runnable default pipeline. They ship in one PR for the same reason (§5.2).
- **Acknowledgements are data, not behaviour.** Reverting the package leaves rows nothing reads.
- **Do not begin Package 7** until `aq playbook cutover-report --json` reports `blocking_reasons: []` **and** `rollback_ready: true` — the latter asserts the previous V1 compiled artifact is still present in `CompiledPlaybookStore` for every enabled playbook, which is what Package 7's rollback window depends on.

---

## 14. Open items for the next child plan

- **Package 7 inherits the drain surface.** `cutover_report.active_v1_runs` already reads `playbook_runs` filtered to `running`/`paused`; Package 7's "list every active V1 run with current step, age, and operator options" should extend that block rather than add a second reader.
- **Package 7 removes** `playbooks.v2.migration.shadow_compare` and the V1 arm of the parity harness (`tests/fixtures/playbooks/v1/default-pipeline.md` and `tests/conftest.py:306` `PipelineEngine`'s use by that suite). The fixture copy of the pre-rewrite pipeline is the *only* remaining executable V1 graph in the tree after Commit 2 — Package 7's "repository search proving V1 execution imports and embedded JSON-action parsing are gone" must account for it explicitly, either by deleting it with the parity suite or by moving it under a clearly-marked historical fixtures path.
- **`src/doctor/integration_checks.py` after cutover.** `_review_dedup_key` hardcodes the pipeline's dedup key. Once the reviewed artifact is authoritative, that check should read the key from the active artifact instead of duplicating it. Raise it in Package 7; T-7 assertion 10 is the ratchet that keeps the two in sync meanwhile.
- **`src/prompts/example_playbooks/` and `src/prompts/default_rules/`** (16 unreferenced files, §2). Package 6 documents them; someone still has to decide whether they are deleted, converted to V2 prose, or wired into an install path. Filed as separate work, not carried by Package 7's cutover.
- **Minimality of approved capability sets** (§4.5). Package 6 proves artifacts do not exceed their reviewed capabilities. Nothing proves the reviewed set is the smallest that works. If that matters, it wants a dedicated audit pass over the activated set, not a test in this package.
- **Parity for LLM playbooks** (§4.5). `memory-consolidation` and `coding-reflection` get structural parity only. If Package 7's acceptance metrics need behavioural parity for them, it needs recorded-transcript replay, which is out of scope here.
