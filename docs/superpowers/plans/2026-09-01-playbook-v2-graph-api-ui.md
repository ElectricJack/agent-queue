# Playbook V2 — Package 5 child plan: Semantic graph API and rich node experience

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to run this plan task by task. Every task below is a red/green/refactor unit with a named failing assertion, a named implementation, and its own verification command. Do not reorder tasks across commit boundaries. §4 is a **frozen interface contract**: the backend and dashboard tasks may run in parallel only because §4 is checked in first (roadmap §7).

**Parent roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` § "Package 5 — Semantic graph API and rich node experience"
**Design spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md` § "Graph and node experience", § "Intent explanation", § "Storage and activation", § "Execution receipts"
**Branch:** `feature/playbook-v2-pkg5` (from `origin/main`)
**Consumes:** Packages 1–4 — explanation payloads (P1), artifact identity and validation (P2), artifact store / activation / health / receipts / waits / pending events (P3), engine receipts and dry-run (P4).
**Produces:** artifact-aware graph, diff, activation, health, pending-event and run-overlay commands; a second dashboard graph surface with event-scoped rule clusters, contract-derived node cards, an exhaustive inspector, diff review, activation control, and exact-artifact run overlays.

**Drafted ahead of its package.** Per the supervisor's instruction (task `solid-harbor.45`), this plan was written against the **current live tree** while Packages 0–4 are still in flight. `origin/feature/playbook-v2-pkg0` is unmerged; there are no plan documents for Packages 1–4 yet. §3 therefore separates *symbols that exist today* from *symbols this plan expects earlier packages to create*, and gives the implementation task a reconciliation checklist it **must** run before its first commit.

---

## 1. Why this package exists (what an operator cannot answer today)

The exit gate asks an operator to answer nine questions from the graph alone. Here is what the live tree actually answers, verified by reading it.

### 1.1 The graph shows node *classification*, not node *behavior*

`src/playbooks/graph_view.py:103` `_classify_node` reduces every node to one of six strings (`entry`, `entry+decision`, `action`, `decision`, `checkpoint`, `terminal`). The card built from it (`dashboard/src/pages/playbook-graph/PlaybookStepNode.tsx:25` `PlaybookStepCard`) shows: the node id, a unicode symbol, that classification, and `actionCommand(node.details.action) ?? node.prompt_preview` — i.e. a bare command name or the first 60 characters of an LLM prompt (`graph_view.py:133` `_prompt_preview`).

It never shows what the command does, which inputs it reads, what it binds, or which capability it needs. There is nothing to show: no contract exists yet (Package 1) and no typed step model exists yet (Package 2).

### 1.2 The inspector renders raw JSON for exactly the fields that matter

`dashboard/src/pages/playbook-graph/PlaybookNodeInspector.tsx:157-173` renders `details.action`, `details.for_each` and `details.output` through the `Payload` component — `JSON.stringify(value, null, 2)` in a `<pre>`. The action payload is the entire semantic content of a pipeline step. The design spec's requirement, "operators can inspect what happens inside every node without reading raw JSON", is failed at the one place it matters most.

### 1.3 Edges are derived from four unrelated shapes, and one is silently dropped

`build_edges` (`src/playbooks/graph_view.py:332`) walks `node.goto`, `node.transitions`, `node.action["on_success"|"on_failure"]`, and `node.on_timeout`. The timeout edge is emitted only `if node.on_timeout and node.on_timeout != node.goto` (`graph_view.py:381`) — a timeout that targets the same node as the fallthrough produces **no edge**, so an executable transition is invisible. Edge ids are positional (`layout.ts:76`, `` `${index}:${source}->${target}:${kind}` ``), so an edge's identity changes when an unrelated edge is inserted upstream, and two transitions between the same pair with the same kind are indistinguishable.

Roadmap §2 requires "every edge displayed in the graph is an executable transition, and every executable transition is displayed". §4 of this plan gives every edge an artifact-derived id and one edge per transition record.

### 1.4 There is no artifact identity anywhere in the read path

`usePlaybookGraph` (`dashboard/src/api/hooks.ts:836`) calls `playbookGraphView({playbook_id, direction, show_prompts, include_live_state:false, include_metrics:false, include_history:false})`. The command (`src/commands/playbook_commands.py:1158` `_cmd_playbook_graph_view`) resolves the playbook through `orchestrator.playbook_manager.get_playbook(playbook_id)` (`playbook_commands.py:1215`) — *the current in-memory compiled object*. A run overlay fetched through the same command (`playbook_commands.py:1209`, `run_id` argument) is therefore projected onto whatever is compiled **now**, not onto what executed. `PlaybookGraphViewResponse.run_overlay` is `dict[str, Any] | None` (`src/api/models/playbook.py:295`) — untyped, and the dashboard disables it outright.

The spec is explicit: "The overlay is never projected onto a newer activation." That cannot be satisfied without artifact pinning, which is why this package consumes Package 3.

### 1.5 Activation, health, diff and pending events have no surface at all

`grep -rn "playbook_activations\|pending_event\|artifact_sha" src/` returns nothing on the live tree. `set_playbook_enabled` (`src/commands/playbook_commands.py:1491`) toggles a boolean on a Markdown file's frontmatter and recompiles; there is no artifact hash, no review step, no health state beyond `PlaybookHealthResponse` (`src/api/models/playbook.py:153`), which is *run statistics* (`run_count`, `success_rate`, `avg_tokens`), not activation health.

**Package 5 does not create any of that state — Package 3 does.** Package 5 is the surface: it projects the artifact into a graph, diffs two artifacts, exposes and mutates activation, lists and resolves pending events, and overlays receipts on the pinned artifact.

---

## 2. Live-tree reconciliation — deviations from the roadmap's file list

Roadmap §3 and §5 allow a child plan to refine filenames after inspecting the live tree, but require the deviation be documented. Every row was verified against `origin/main` at `30b86a68`.

| Roadmap says | Live tree | Decision |
|---|---|---|
| "V2 graph, artifact-diff, activation, health, pending-event, and run-overlay **endpoints under `src/api/`**" | Playbook HTTP routes are **not hand-written**. `src/api/codegen.py:497` builds `POST /api/{category}/{stripped-name}` for every entry in `src/tools/definitions.py::_TOOL_CATEGORIES`, dispatching to `CommandHandler.execute` and typing the response from `src/api/models/*::RESPONSE_MODELS` | Ship the six surfaces as **CommandHandler commands**, not FastAPI routes. This is one implementation reaching CLI (`aq playbook v2-graph`), MCP and HTTP at once (aq-surface D6), and it is the only path the committed OpenAPI snapshot and both generated clients already cover. New module `src/api/models/playbook_v2.py`; new mixin `src/commands/playbook_v2_commands.py` |
| Modify `src/api/models/playbook.py` | 391 lines, all V1 shapes, aggregated by `src/api/models/__init__.py:48` `get_all_response_models` | **Do not modify.** V1 models stay untouched so Package 7 can delete the V1 command set without unpicking V2 DTOs from the same file. Add `playbook_v2` to the `get_all_response_models` import list and merge tuple |
| Modify `dashboard/src/pages/playbook-graph/{types,layout,PlaybookGraphCanvas,PlaybookGraphView,PlaybookNodeInspector,PlaybookStepNode}.tsx` | These six render `CompiledPlaybookNode` (`src/api/models/playbook.py:213`) — `prompt`, `action`, `for_each`, `goto`, `transitions[].when` | **Create a sibling directory `dashboard/src/pages/playbook-graph-v2/` and leave V1 untouched.** The V2 node model shares *no* field with `CompiledPlaybookNode`; branching both shapes through one component is precisely the "indefinite compatibility layer" roadmap §1 forbids, and it would leave Package 7 editing components instead of deleting a directory. The V1 Graph tab stays available until Package 7 (roadmap Package 5 rollback boundary requires this) |
| Modify `dashboard/src/api/hooks.ts` | 900+ lines, React Query conventions established (`playbookGraphKey` at `hooks.ts:829`, `enabled: !!id`, `refetchInterval`, mutation `onSettled` invalidation) | **Modify as stated.** Seven new hooks follow the same conventions (§6.4) |
| Modify "generated dashboard API client files" | `packages/aq-ts-client/src/` (regenerated by `scripts/regenerate-ts-client.sh`), re-exported through `dashboard/src/api/client.ts`; Python twin `packages/aq-client/` (`scripts/regenerate-api-client.sh`); the spec snapshot is the committed `openapi.json`, built **offline** by `src/api/spec.py` — no daemon needed | **Modify as stated**, and commit `openapi.json` in the same commit as the DTOs. Two existing guards fail otherwise: `tests/test_api_client_contract.py` asserts a bidirectional match between `create_app()` operations and the committed client, and `::test_committed_openapi_json_matches_the_live_app_surface` (`tests/test_api_client_contract.py:281`) fails when the committed spec drifts from what the app serves |
| Verify `scripts/regenerate-api-client.sh --from-file` / `scripts/regenerate-ts-client.sh --from-file` | Both scripts now take `--offline`, which builds the spec in-process through `src/api/spec.py` with no daemon; `--from-file` reuses whatever `openapi.json` happens to be on disk and so cannot detect drift | Substitute `--offline` for both. `--from-file` stays valid but is the wrong tool for a package that *adds* operations: it would regenerate the clients from the stale snapshot and leave `test_committed_openapi_json_matches_the_live_app_surface` red |
| Verify `npm test -- --run dashboard/src/pages/playbook-graph` **from `dashboard/`** | From `dashboard/` that path does not exist; the tree root for vitest is `dashboard/` | Substitute `npm test -- --run src/pages/playbook-graph src/pages/playbook-graph-v2` (from `dashboard/`), or `npm -w dashboard test -- --run src/pages/playbook-graph-v2` from the repo root |
| Verify `npm run lint` / `npm run typecheck` / `npm run build` | Root `package.json` delegates `lint`/`typecheck` to `npm -w dashboard`; root `build` also builds `packages/aq-ts-client` first | Run all three **from the repo root** so the regenerated TS client is compiled by the same command that consumes it |
| (not listed) | `src/tools/definitions.py:114-131` maps command → category; a command absent from `_TOOL_CATEGORIES` gets **no HTTP route and no CLI verb** | Add `src/tools/definitions.py` to the modify list (both `_TOOL_CATEGORIES` and `_ALL_TOOL_DEFINITIONS`) |
| (not listed) | `src/commands/handler.py:159` `PAUSED_PLAYBOOK_COMMANDS` gates every playbook command behind `playbooks.enabled`, which ships `False` (`src/config.py:844`) | Add `src/commands/handler.py` — the seven new names join that frozenset, and the mixin joins the `CommandHandler` bases at `handler.py:315` |
| (not listed) | Feature flags need a home; `PlaybooksConfig` (`src/config.py:837`) currently holds one field | Add `src/config.py` — `playbooks.v2_api` and `playbooks.v2_activation_writes` (§8) |
| (not listed) | `src/playbooks/graph_view.py` is a 816-line V1 projector deleted-adjacent to Package 7 | Add **new** `src/playbooks/graph_projection.py`, `src/playbooks/artifact_diff.py`, `src/playbooks/run_overlay.py`. All three are pure functions over Package 2/3 models, testable with no DB, mirroring `graph_view.py`'s "all functions are pure" contract (`graph_view.py:17`) |
| Create "API and dashboard tests for each new surface" under implied `tests/api/` | `tests/api/`, `tests/playbooks/`, `tests/commands/` **do not exist**; all 380+ suites are flat `tests/test_*.py`, with only `tests/fixtures/`, `tests/llm/`, `tests/perf/` as subdirectories | Flat names throughout: `tests/test_playbook_graph_projection.py`, `tests/test_playbook_artifact_diff.py`, `tests/test_playbook_run_overlay.py`, `tests/test_playbook_v2_api_dtos.py`, `tests/test_api_playbook_v2_commands.py`, `tests/test_playbook_activation_commands.py`, `tests/test_playbook_pending_events_commands.py`. Same deviation Package 0's plan recorded (`2026-09-01-playbook-v2-phase0-security.md` §2) |
| (no storage change implied by "additive API and UI routes") | Operator **writes** (activate an artifact; dispatch/discard a pending event) need somewhere to record who did it and when | **One conditional additive Alembic revision** (§9). Run the §3.2 checklist first: if Package 3 already shipped `playbook_activations.activated_by` and `playbook_pending_events.resolved_at/resolved_by/resolution`, this package ships **no** migration |
| Commit sequence: 6 commits | — | **6 commits, unchanged names** (§12). C1 is widened to carry all six DTO families plus the regenerated clients, because roadmap §7 makes checked-in DTOs the precondition for parallel work |

### 2.1 Two naming reconciliations

- **`ActivationHealth` values.** The design spec (§"Compatibility, rebuild, and failure behavior") says `ready` / `needs_rebuild` / `unavailable` / `invalid`. Roadmap §4 locks `ready` / `question_required` / `invalid` / `disabled` / `stale_contract`. **Roadmap §4 wins** — it is the locked cross-package interface. `needs_rebuild` is the spec's name for `stale_contract`; `unavailable` has no roadmap equivalent and describes a real, distinct, *transient* condition (the artifact file or its provider failed to load), so it is added as a **sixth** value. Roadmap §4 permits additions, forbids renames. The DTO in §4.4 is the authority; Package 3 must emit exactly these six.
- **"Endpoint" means "command".** Every use of "endpoint" in roadmap Package 5 is read as "CommandHandler command reachable at `POST /api/playbook/<name>`". The URL table in §11 is the literal mapping.

---

## 3. What this plan assumes from Packages 0–4

### 3.1 Expected symbols (do not exist on `origin/main` today)

| Symbol | Owner | This package uses it for |
|---|---|---|
| `src/playbooks/definition.py::PlaybookDefinition`, `Rule`, `Trigger`, `SourceRef`, the seven-member step union | P2 | The input to `graph_projection.project_graph` |
| `src/playbooks/expressions.py` typed value union (`Literal`, `EventRef`, `BindingRef`, `LoopRef`, `Template`, `Comparison`, `BooleanOp`, `Exists`) | P2 | `ExplanationValueDTO.kind`, `.canonical` |
| `src/playbooks/explanation.py::explain_step(step, contract) -> StepExplanation` with `EffectClause` | P1 | `StepExplanationDTO` — projected 1:1, never re-derived (§5.2) |
| `src/commands/contracts/registry.py::get_contract(name)`, `CommandContract.execution_fingerprint`, `.sensitive_fields` | P1 | `contract_fingerprint`, redaction, stale-contract diagnostics |
| `src/profiles/capabilities.py::CapabilityPolicy` (`harness_tools`/`aq_commands`/`plugin_tools`, canonical serialization) | P0 | `CapabilityNamespacesDTO`, `capability_fingerprint` |
| `src/playbooks/artifact_store.py::ArtifactRef`, `ArtifactStore.load(sha) -> PlaybookDefinition` | P3 | Loading the **pinned** artifact for an overlay and both sides of a diff |
| `src/playbooks/activation.py::ActivationHealth`, activation record | P3 | §4.4 DTOs |
| `src/database/queries/playbook_artifact_queries.py` — list artifacts for a playbook, get activation, set activation | P3 | `playbook_v2_graph`, `playbook_activation_health`, `playbook_activate` |
| `src/database/queries/playbook_run_queries.py` — get V2 run, list receipts for a run, list/resolve pending events | P3 | `playbook_run_overlay`, pending-event commands |
| `src/playbooks/receipts.py` receipt row (attempt, iteration, outcome, selected transition, usage, idempotency key) | P3/P4 | `ReceiptDTO`, `NodeOverlayDTO`, `EdgeOverlayDTO` |
| `src/playbooks/engine.py::PlaybookEngine.dispatch_event` | P4 | The pending-event `dispatch` action re-enters the engine rather than re-implementing dispatch |

### 3.2 Reconciliation checklist — run this **before commit 1**

```bash
# 1. Do the expected modules exist, and are the symbols named as assumed?
python - <<'PY'
import importlib
WANT = {
  "src.playbooks.definition":  ["PlaybookDefinition", "Rule", "SourceRef"],
  "src.playbooks.explanation": ["explain_step", "StepExplanation", "EffectClause"],
  "src.playbooks.artifact_store": ["ArtifactRef", "ArtifactStore"],
  "src.playbooks.activation":  ["ActivationHealth"],
  "src.commands.contracts.registry": ["get_contract"],
  "src.profiles.capabilities": ["CapabilityPolicy"],
  "src.database.queries.playbook_artifact_queries": [],
  "src.database.queries.playbook_run_queries": [],
}
for mod, names in WANT.items():
    try:
        m = importlib.import_module(mod)
    except Exception as exc:
        print(f"MISSING MODULE {mod}: {exc}"); continue
    for n in names:
        if not hasattr(m, n):
            print(f"MISSING SYMBOL {mod}.{n}")
PY

# 2. Did Package 3 already ship the operator-write columns?  Decides whether §9 runs at all.
python -c "from src.database.tables import metadata; \
t=metadata.tables; \
print('activations:', sorted(c.name for c in t['playbook_activations'].columns) if 'playbook_activations' in t else 'ABSENT'); \
print('pending:', sorted(c.name for c in t['playbook_pending_events'].columns) if 'playbook_pending_events' in t else 'ABSENT')"

# 3. Is the six-value health enum what Package 3 emits?
python -c "from src.playbooks.activation import ActivationHealth; print(sorted(v.value for v in ActivationHealth))"

# 4. Are the V1 surfaces still present (Package 7 has not run)?
ls dashboard/src/pages/playbook-graph/ src/playbooks/graph_view.py
```

**If any line reports a mismatch, stop and amend this document in the same commit as the code that reconciles it.** Roadmap §7: "Changes to locked interfaces require updating all not-yet-completed child plans." Record the amendment in §16 (done — see §16).

### 3.3 The one thing this package must not do

Package 5 is a **read-and-review** surface plus two narrow operator writes (activate, resolve pending event). It must not compile, validate, execute, or repair anything. If a projection needs a fact the artifact does not carry, the fix belongs in Package 2 or 3 — not in a projector that infers it.

---

## 4. Locked API DTOs — the parallelism contract

> This section is the reason Package 5 can be split across parallel tasks (roadmap §7: "Package 5 backend endpoints and dashboard components may proceed in parallel **after API DTOs are checked in**"). It is checked in whole, verbatim, as `src/api/models/playbook_v2.py` in commit 1. A backend task may **add** an optional field with a default; it may not rename, retype, or remove one without amending this section and re-running every dashboard suite.

Conventions, all enforced by `tests/test_playbook_v2_api_dtos.py`:

- every model sets `model_config = ConfigDict(extra="forbid")`;
- optional blocks are `X | None = None` and are serialized as explicit `null` — the V2 commands are **not** added to `src/api/codegen.py::RESPONSE_EXCLUDE_NONE` (that hack exists for `playbook_graph_view`'s "key present only when the compiler set it" contract, `codegen.py:65`; V2 typing makes it unnecessary and its absence is what lets the TS client type optionality);
- timestamps are `float` POSIX seconds (matching `PlaybookRunSummary.started_at`, `src/api/models/playbook.py:53`) except `ArtifactRefDTO.compiled_at`, which is an ISO-8601 string (matching `PlaybookGraphIdentity.compiled_at`, `playbook.py:179`);
- hashes are the full `"sha256:<64 lowercase hex>"` form, never truncated server-side; the UI truncates for display;
- every free-text field is already redacted server-side. There is no client-side redaction anywhere in this package.

### 4.1 Shared identity and value primitives

```python
"""Typed response models for the Playbook V2 semantic-graph surface.

Deliberately separate from ``src/api/models/playbook.py`` (V1): the two share
no field, and Package 7 deletes the V1 module wholesale.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class V2Model(BaseModel):
    """Strict base — an unknown key is a contract break, not a warning."""

    model_config = ConfigDict(extra="forbid")


class ArtifactRefDTO(V2Model):
    """Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
    immutable artifact; every graph, diff and overlay response carries one."""

    playbook_id: str
    artifact_sha256: str                    # "sha256:<64 hex>"
    schema_generation: int                  # PlaybookDefinition.schema_version
    contract_fingerprint: str               # canonical digest of compiled_against.commands
    source_digest: str                      # "sha256:<64 hex>" of the Markdown source
    compiler_build: str                     # compiler build identity
    compiled_at: str | None = None          # ISO-8601 UTC
    version: int = 0                        # monotonic per playbook, display only


class SourceRefDTO(V2Model):
    """Where in the authoring Markdown this element came from."""

    path: str                               # vault-relative, e.g.
                                            # "system/playbooks/default-pipeline.md" — system playbooks
                                            # live under vault/system/playbooks/ (see
                                            # src/commands/playbook_commands.py:1286 _vault_playbook_dirs)
    start_line: int
    end_line: int
    heading: str | None = None
    excerpt: str | None = None              # <= 400 chars, redaction-clean


ValueKind = Literal[
    "literal", "event_ref", "binding_ref", "loop_ref",
    "template", "expression", "redacted", "unresolved",
]


class ExplanationValueDTO(V2Model):
    """One typed value, in both its human and canonical forms.

    ``display`` is always present and always safe to render.  ``canonical`` is
    the Advanced-view payload and is ``None`` whenever ``redacted`` is true.
    """

    kind: ValueKind
    display: str
    canonical: Any | None = None
    redacted: bool = False
    type_name: str | None = None            # declared type, e.g. "string", "TaskRef"


ValueSource = Literal[
    "event", "binding", "loop", "literal", "template",
    "profile", "policy", "derived",
]


class ExplanationRowDTO(V2Model):
    """A labelled input/output row: ``Project -> this event's project``."""

    label: str
    value: ExplanationValueDTO
    source: ValueSource
    required: bool = True
    description: str | None = None
```

### 4.2 Explanation (Package 1's payload, projected 1:1)

```python
EffectKind = Literal[
    "creates", "updates", "deletes", "reads", "sends", "schedules",
    "waits", "branches", "binds", "invokes_ai", "delegates", "noop",
]


class EffectClauseDTO(V2Model):
    """One typed effect clause from the command contract.

    ``detail`` is rendered by the backend from the clause and its resolved
    inputs.  The frontend lays this out; it never re-derives command meaning
    (design spec: "The frontend lays out this structure but does not
    reinterpret command semantics")."""

    kind: EffectKind
    subject: str                            # "task", "gate", "message", ...
    detail: str
    arguments: list[ExplanationRowDTO] = []
    conditional_on: str | None = None       # rendered condition, when the clause is conditional


class OutcomeExplanationDTO(V2Model):
    """One legal outcome of a step and where it goes."""

    outcome: str                            # exact typed outcome, e.g. "success", "approve"
    label: str                              # human label, presentation-only
    target_step_id: str | None = None       # None only for a terminal outcome
    target_title: str | None = None
    reserved: bool = False                  # engine-reserved rather than business outcome
    terminal_outcome: str | None = None     # set when the outcome ends the rule


ExplanationRenderer = Literal["contract", "canonical"]


class StepExplanationDTO(V2Model):
    """The contract-derived intent card.  Node card and inspector consume
    this same object (design spec UI invariant)."""

    title: str
    effect_summary: str
    effects: list[EffectClauseDTO] = []
    inputs: list[ExplanationRowDTO] = []
    result: ExplanationRowDTO | None = None
    outcomes: list[OutcomeExplanationDTO] = []
    contract_fingerprint: str | None = None  # None for non-command steps
    renderer: ExplanationRenderer = "contract"
```

`renderer="canonical"` is the spec's lossless fallback: presentation metadata was absent, so every executable field is shown as a field/value pair. It is a display fact, never a reason to hide a field, and never blocks activation.

### 4.3 Graph

```python
StepKind = Literal["command", "llm", "agent_task", "decision", "wait", "foreach", "terminal"]

EdgeKind = Literal[
    "success", "failure", "decision_case", "decision_default",
    "loop_body", "loop_exit", "loop_back", "timeout",
    "wait_matched", "runtime_error", "cancelled", "terminal",
]

DiagnosticSeverity = Literal["error", "warning", "question", "info"]


class GraphDiagnosticDTO(V2Model):
    """A compile question, invalid reference, stale contract or disabled
    activation.  Diagnostics annotate the graph; they never hide it."""

    severity: DiagnosticSeverity
    code: str                               # stable machine code, e.g. "stale_contract"
    message: str
    rule_id: str | None = None
    step_id: str | None = None
    source: SourceRefDTO | None = None


class GridPositionDTO(V2Model):
    x: int = 0
    y: int = 0


class ClusterBoundsDTO(V2Model):
    """Grid-unit bounding box of one rule cluster."""

    x: int
    y: int
    width: int
    height: int


class GraphLayoutDTO(V2Model):
    direction: Literal["TD", "LR"] = "TD"
    grid_positions: dict[str, GridPositionDTO] = {}
    cluster_bounds: dict[str, ClusterBoundsDTO] = {}   # rule_id -> bounds


class CapabilityNamespacesDTO(V2Model):
    """``CapabilityPolicy`` projected.  Sorted; empty list means deny-all."""

    harness_tools: list[str] = []
    aq_commands: list[str] = []
    plugin_tools: list[str] = []


class AiBudgetDTO(V2Model):
    max_calls: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    timeout_seconds: int | None = None


class DelegationPolicyDTO(V2Model):
    """AgentTaskStep only."""

    child_profile_id: str
    wait_for_completion: bool = True
    cancel_child: bool = False
    narrowed_from: str | None = None        # parent principal provenance, human-readable


class AiNodeDetailDTO(V2Model):
    """Everything an operator needs about an AI state (design spec: "AI cards
    show the profile, resolved capability namespaces, capability fingerprint,
    budgets, and delegation policy")."""

    profile_id: str
    intelligence_class: str | None = None
    provider: str | None = None
    model: str | None = None
    capabilities: CapabilityNamespacesDTO
    capability_fingerprint: str
    budget: AiBudgetDTO
    output_schema: dict[str, Any] | None = None
    tool_use_enabled: bool = False
    delegation: DelegationPolicyDTO | None = None


class LoopNodeDetailDTO(V2Model):
    """ForEachStep only."""

    collection: ExplanationValueDTO
    item_binding: str
    failure_policy: Literal["halt", "continue", "collect"]
    body_entry_step_id: str
    continuation_step_id: str | None = None


class WaitNodeDetailDTO(V2Model):
    """WaitStep only."""

    wait_kind: Literal["event", "human", "task", "timer"]
    awaited: str                            # event type, gate title, or task reference
    correlation_key: ExplanationValueDTO
    timeout_seconds: int | None = None
    timeout_step_id: str | None = None


class RetryPolicyDTO(V2Model):
    max_attempts: int = 1
    backoff_seconds: float | None = None
    retry_on: list[str] = []                # outcomes that retry rather than transition


class IdempotencyDTO(V2Model):
    supported: bool = False
    key_template: str | None = None         # e.g. "<run_id>:<step_id>:<attempt>"
    retry_safe: bool = False                # False -> operator_decision_required on ambiguity


class RedactionRowDTO(V2Model):
    field: str
    policy: Literal["safe", "summarized", "opaque_handle", "redacted"]


class NodeAdvancedDTO(V2Model):
    """Advanced view.  Canonical data, never the default explanation."""

    typed_step: dict[str, Any]              # the exact step object from the artifact
    resolved_inputs: list[ExplanationRowDTO] = []
    result_schema: dict[str, Any] | None = None
    retry: RetryPolicyDTO | None = None
    idempotency: IdempotencyDTO | None = None
    redaction: list[RedactionRowDTO] = []
    execution_fingerprint: str | None = None


class NodeBadgeDTO(V2Model):
    """One compact chip on the card.  Ordered by the backend."""

    kind: Literal[
        "profile", "budget", "capability", "timeout",
        "retry", "idempotency", "loop", "wait", "redaction", "diagnostic",
    ]
    label: str
    value: str


class GraphNodeDTO(V2Model):
    id: str                                 # artifact-local step id
    rule_id: str
    step_kind: StepKind
    title: str
    description: str | None = None
    entry: bool = False
    terminal_outcome: str | None = None
    explanation: StepExplanationDTO
    badges: list[NodeBadgeDTO] = []
    ai: AiNodeDetailDTO | None = None
    loop: LoopNodeDetailDTO | None = None
    wait: WaitNodeDetailDTO | None = None
    source: SourceRefDTO
    advanced: NodeAdvancedDTO
    diagnostics: list[GraphDiagnosticDTO] = []
    out_degree: int = 0
    position: GridPositionDTO = GridPositionDTO()


class GraphEdgeDTO(V2Model):
    """One transition record.  ``id`` is derived from artifact content, so it
    is stable across recompiles that do not change the transition, and unique
    within the artifact: ``f"{rule_id}::{source}::{outcome}"``."""

    id: str
    rule_id: str
    source: str
    source_port: str                        # == outcome; the card anchors ports by this
    target: str
    outcome: str
    label: str                              # presentation label; defaults to ``outcome``
    kind: EdgeKind
    reserved: bool = False
    condition: str | None = None            # rendered case condition, decision edges only


class RuleClusterDTO(V2Model):
    """One first-class rule.  A rule owns a closed subgraph — no edge in
    ``GraphEdgeDTO`` ever crosses ``rule_id``."""

    rule_id: str
    name: str
    event_type: str
    trigger_filter: dict[str, Any] | None = None
    entry_step_id: str
    step_ids: list[str] = []
    source: SourceRefDTO
    diagnostics: list[GraphDiagnosticDTO] = []


class EventGroupDTO(V2Model):
    event_type: str
    rule_ids: list[str] = []
    node_count: int = 0
    edge_count: int = 0


class GraphLegendDTO(V2Model):
    step_kinds: dict[str, str] = {}         # StepKind -> label
    edge_kinds: dict[str, str] = {}         # EdgeKind  -> label


class PlaybookV2GraphResponse(V2Model):
    success: bool = True
    artifact: ArtifactRefDTO
    activation: ActivationStateDTO
    purpose: str = "routine"                # "routine" | "assignment_routing"
    event_groups: list[EventGroupDTO] = []
    rules: list[RuleClusterDTO] = []
    nodes: list[GraphNodeDTO] = []
    edges: list[GraphEdgeDTO] = []
    layout: GraphLayoutDTO = GraphLayoutDTO()
    diagnostics: list[GraphDiagnosticDTO] = []
    legend: GraphLegendDTO = GraphLegendDTO()
```

**Filtering is server-side and lossless.** `playbook_v2_graph(event_type=...)` narrows `rules`/`nodes`/`edges` to the rules triggered by that event and **every node reachable from them**. `event_groups` always lists all events, so the selector never depends on the current filter. Filtering never drops a reachable branch (design spec UI invariant, asserted by `test_event_filter_preserves_every_reachable_branch`).

### 4.4 Activation and health

```python
ActivationHealthValue = Literal[
    "ready", "question_required", "invalid",
    "disabled", "stale_contract", "unavailable",
]


class ActivationHealthReasonDTO(V2Model):
    code: str                               # e.g. "command_contract_changed"
    message: str
    subject: str | None = None              # command name / profile id / file path
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None


class ActivationStateDTO(V2Model):
    playbook_id: str
    scope: str                              # "system" | "project" | "agent_type"
    scope_identifier: str | None = None
    enabled: bool = False
    active_artifact_sha256: str | None = None
    health: ActivationHealthValue = "disabled"
    reasons: list[ActivationHealthReasonDTO] = []
    activated_at: float | None = None
    activated_by: str | None = None
    pending_event_count: int = 0
    running_count: int = 0


class PlaybookActivationHealthResponse(V2Model):
    success: bool = True
    activations: list[ActivationStateDTO] = []
    count: int = 0
    by_health: dict[str, int] = {}          # ActivationHealthValue -> count


class SetPlaybookActivationResponse(V2Model):
    success: bool = True
    activation: ActivationStateDTO
    previous_artifact_sha256: str | None = None
    changed: bool = False
    blocked: bool = False
    blockers: list[str] = []                # non-empty only when blocked
```

`enabled` and `health` are independent (design spec). A disabled activation still reports its computed health; `health="disabled"` is used only when there is no active artifact at all.

### 4.5 Semantic diff

```python
DiffChange = Literal["added", "removed", "modified", "unchanged"]


class FieldChangeDTO(V2Model):
    path: str                               # JSON pointer within the step or rule
    before: ExplanationValueDTO | None = None
    after: ExplanationValueDTO | None = None
    executable: bool = True                 # False => presentation-only (labels, help text)


class StepDiffDTO(V2Model):
    step_id: str
    rule_id: str | None = None
    change: DiffChange
    step_kind: StepKind | None = None
    title_before: str | None = None
    title_after: str | None = None
    field_changes: list[FieldChangeDTO] = []
    explanation_before: StepExplanationDTO | None = None
    explanation_after: StepExplanationDTO | None = None


class EdgeDiffDTO(V2Model):
    edge_id: str
    rule_id: str
    source: str
    target: str
    outcome: str
    change: DiffChange


class RuleDiffDTO(V2Model):
    rule_id: str
    change: DiffChange
    event_type_before: str | None = None
    event_type_after: str | None = None
    step_ids_added: list[str] = []
    step_ids_removed: list[str] = []


class ContractChangeDTO(V2Model):
    command: str
    fingerprint_before: str | None = None
    fingerprint_after: str | None = None
    change: DiffChange


class PlaybookArtifactDiffResponse(V2Model):
    success: bool = True
    base: ArtifactRefDTO | None = None      # None when activating the first artifact
    target: ArtifactRefDTO
    executable_change: bool = False
    semantic_change_count: int = 0
    presentation_change_count: int = 0
    rules: list[RuleDiffDTO] = []
    steps: list[StepDiffDTO] = []
    edges: list[EdgeDiffDTO] = []
    contracts: list[ContractChangeDTO] = []
    diagnostics: list[GraphDiagnosticDTO] = []
    activation_blocked: bool = False
    activation_blockers: list[str] = []
```

`executable_change=False` with `presentation_change_count>0` is exactly the spec's "a label or help-text improvement does not block activation or change an execution fingerprint". The diff is computed from the two `PlaybookDefinition` objects, **not** from their JSON bytes: reordering an unordered map is `unchanged`.

### 4.6 Pending events

```python
PendingReason = Literal[
    "stale_contract", "invalid_artifact", "disabled",
    "unavailable", "question_required",
]


class PendingEventDTO(V2Model):
    pending_event_id: str
    playbook_id: str
    event_type: str
    event: dict[str, Any] = {}              # redacted projection, never the raw payload
    received_at: float
    reason: PendingReason
    attempts: int = 0
    last_error: str | None = None
    expires_at: float | None = None         # retention deadline (7 days by default)


class ListPlaybookPendingEventsResponse(V2Model):
    success: bool = True
    events: list[PendingEventDTO] = []
    count: int = 0
    oldest_received_at: float | None = None
    by_reason: dict[str, int] = {}


PendingAction = Literal["dispatch", "discard"]


class PlaybookPendingEventActionResponse(V2Model):
    success: bool = True
    action: PendingAction
    requested: int = 0
    dispatched_run_ids: list[str] = []
    discarded_ids: list[str] = []
    skipped: list[str] = []                 # ids that no longer exist or already resolved
    errors: list[str] = []
```

### 4.7 Run overlay

```python
RunLifecycle = Literal[
    "running", "paused", "cancelling",
    "completed", "failed", "timed_out", "cancelled",
]

NodeRunState = Literal[
    "not_visited", "running", "completed", "failed",
    "paused", "cancelled", "timed_out", "skipped",
]


class TokenUsageDTO(V2Model):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False                 # True when the provider reported no usage


class WaitFactsDTO(V2Model):
    wait_kind: Literal["event", "human", "task", "timer"]
    correlation_key: str
    registered_at: float
    deadline_at: float | None = None
    deadline_source: Literal["wait", "run"] | None = None
    matched_at: float | None = None
    matched_event_id: str | None = None


class CancellationFactsDTO(V2Model):
    requested_at: float
    acknowledged_at: float | None = None
    cancelled_child: bool = False


class ReceiptDTO(V2Model):
    receipt_id: str
    step_id: str
    rule_id: str
    step_kind: StepKind
    attempt: int = 1
    iteration_index: int | None = None      # set only inside a foreach body
    outcome: str
    selected_edge_id: str | None = None     # joins GraphEdgeDTO.id
    started_at: float
    completed_at: float | None = None
    duration_seconds: float | None = None
    inputs: list[ExplanationRowDTO] = []    # contract-redacted, default-deny
    result: ExplanationValueDTO | None = None
    token_usage: TokenUsageDTO | None = None
    idempotency_key: str | None = None
    principal_fingerprint: str | None = None
    profile_id: str | None = None
    contract_fingerprint: str | None = None
    error: str | None = None
    wait: WaitFactsDTO | None = None
    cancellation: CancellationFactsDTO | None = None


class LoopIterationOverlayDTO(V2Model):
    index: int
    item_display: str
    outcome: str | None = None
    receipt_ids: list[str] = []
    started_at: float | None = None
    completed_at: float | None = None


class NodeOverlayDTO(V2Model):
    step_id: str
    state: NodeRunState = "not_visited"
    visit_count: int = 0
    last_outcome: str | None = None
    receipt_ids: list[str] = []
    iterations: list[LoopIterationOverlayDTO] = []


class EdgeOverlayDTO(V2Model):
    edge_id: str
    traversal_count: int = 0
    last_traversed_at: float | None = None


class OperatorDecisionDTO(V2Model):
    """A run paused with ``operator_decision_required`` after an ambiguous
    interruption of a non-retry-safe command (design spec, run-state §)."""

    step_id: str
    attempt: int
    reason: str
    options: list[Literal["accept_outcome", "retry", "fail", "cancel"]] = []
    raised_at: float


class RunBudgetDTO(V2Model):
    llm_calls: int = 0
    total_tokens: int = 0
    max_total_tokens: int | None = None
    cost_usd: float | None = None


class PlaybookRunOverlayResponse(V2Model):
    success: bool = True
    run_id: str
    artifact: ArtifactRefDTO                # the run's PINNED artifact
    artifact_is_active: bool = False        # False => "this run used an older artifact"
    rule_id: str
    lifecycle: RunLifecycle
    current_step_id: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    deadline_at: float | None = None
    trigger_event: dict[str, Any] = {}      # redacted
    nodes: list[NodeOverlayDTO] = []
    edges: list[EdgeOverlayDTO] = []
    receipts: list[ReceiptDTO] = []
    bindings: list[ExplanationRowDTO] = []
    operator_decision: OperatorDecisionDTO | None = None
    budget: RunBudgetDTO | None = None
    truncated: bool = False                 # receipts capped (§5.4)
    receipt_total: int = 0
```

The overlay response carries the artifact ref of the **pinned** artifact and nothing else. The dashboard fetches the graph for `overlay.artifact.artifact_sha256`, never for the playbook's current activation; `artifact_is_active=False` renders a persistent banner. This is the single mechanism satisfying "Run overlays are pinned to the exact artifact executed".

### 4.8 Registration

```python
RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "playbook_v2_graph": PlaybookV2GraphResponse,
    "playbook_activation_health": PlaybookActivationHealthResponse,
    "playbook_activate": SetPlaybookActivationResponse,
    "playbook_artifact_diff": PlaybookArtifactDiffResponse,
    "playbook_pending_events": ListPlaybookPendingEventsResponse,
    "playbook_pending_event_action": PlaybookPendingEventActionResponse,
    "playbook_run_overlay": PlaybookRunOverlayResponse,
}
```

`ActivationStateDTO` is referenced by `PlaybookV2GraphResponse` before its definition in §4.3; in the checked-in module the activation block (§4.4) is defined **above** the graph block. Pydantic would otherwise need `model_rebuild()`, and the generated TS client emits cleaner types with a plain forward-free ordering.

### 4.9 Generated client names (deterministic, do not guess)

`src/api/codegen.py:518` sets `operation_id=cmd_name`, so both generators derive names mechanically:

| Command | HTTP | TS SDK fn (`dashboard/src/api/client.ts`) | Python client module |
|---|---|---|---|
| `playbook_v2_graph` | `POST /api/playbook/v2-graph` | `playbookV2Graph` | `api/playbook/playbook_v2_graph.py` |
| `playbook_activation_health` | `POST /api/playbook/activation-health` | `playbookActivationHealth` | `.../playbook_activation_health.py` |
| `playbook_activate` | `POST /api/playbook/activate` | `playbookActivate` | `.../playbook_activate.py` |
| `playbook_artifact_diff` | `POST /api/playbook/artifact-diff` | `playbookArtifactDiff` | `.../playbook_artifact_diff.py` |
| `playbook_pending_events` | `POST /api/playbook/pending-events` | `playbookPendingEvents` | `.../playbook_pending_events.py` |
| `playbook_pending_event_action` | `POST /api/playbook/pending-event-action` | `playbookPendingEventAction` | `.../playbook_pending_event_action.py` |
| `playbook_run_overlay` | `POST /api/playbook/run-overlay` | `playbookRunOverlay` | `.../playbook_run_overlay.py` |

(`_strip_category_prefix("playbook_v2_graph", "playbook")` → `v2_graph` → path `v2-graph`; `src/cli/auto_commands.py:280`. CLI verbs follow: `aq playbook v2-graph`, `aq playbook activate`, …)

---

## 5. Backend design

### 5.1 `src/playbooks/graph_projection.py` — artifact → graph DTO

Pure, synchronous, no DB, no I/O. Mirrors `graph_view.py`'s stated contract so it is unit-testable from a JSON fixture alone.

```python
def project_graph(
    definition: PlaybookDefinition,
    artifact: ArtifactRef,
    activation: ActivationState,
    *,
    event_type: str | None = None,
    contracts: ContractLookup,           # name -> CommandContract | None
    profiles: ProfileLookup,             # profile_id -> resolved profile + CapabilityPolicy
    direction: str = "TD",
) -> dict[str, Any]:                     # validates as PlaybookV2GraphResponse
```

Rules it enforces (each is a test in `tests/test_playbook_graph_projection.py`):

1. **One edge per declared transition, and nothing else.** For every step, iterate its declared transitions in artifact order and emit one `GraphEdgeDTO` per entry; a `foreach`'s `body_entry` and a `decision`'s cases and `default` are declared transitions too. The design spec allows a step to satisfy the reserved outcomes *either* by mapping each one *or* by supplying one visible `runtime_error` target — when it supplies the catch-all, the projector emits **one** `runtime_error` edge, not one edge per reserved outcome, and marks it `reserved: true`. Never dedupe by `(source, target)`. Never suppress an edge because its target equals another edge's target — this is the direct fix for the V1 `on_timeout != goto` drop (§1.3). For the §10.1 fixture this yields exactly **28** edges: 17 in `review-on-task-completed`, 11 in `sweep-on-spec-created`.
2. **Edge id is content-derived**: `f"{rule_id}::{step_id}::{outcome}"`. Unique by construction (a step maps each outcome once; validation in Package 2 guarantees it) and stable across recompiles.
3. **No cross-cluster edge.** Assert `target in rule.step_ids` for every edge; a violation raises `GraphProjectionError` rather than emitting a cross-cluster edge. Package 2 already rejects cross-rule transitions, so this is a defense-in-depth assert with a test that feeds a hand-built violating definition.
4. **Shared terminals are duplicated per cluster.** Terminal steps are owned by exactly one rule in V2, so this falls out of rule ownership; the test asserts two rules ending in `done` produce two distinct node ids.
5. **Explanation is not re-derived.** `explain_step` (Package 1) is called once per step; the projector copies the result into `StepExplanationDTO` field-for-field. A test monkeypatches `explain_step` to return a sentinel and asserts the sentinel reaches the DTO unmodified.
6. **Layout.** Layered BFS from the rule's entry step *within the cluster*, reusing `graph_view._compute_layout`'s algorithm shape but scoped per rule, then clusters are stacked vertically (TD) or horizontally (LR) with a one-cell gutter. `cluster_bounds` is the min/max box of each cluster's positions. Deterministic: iteration follows artifact order, never `dict` insertion luck.
7. **Diagnostics, never omission.** A step whose command has no registered contract still produces a node — with `renderer="canonical"`, an `error`-severity diagnostic `code="unknown_command"`, and `explanation.effects=[]`. Nothing disappears from the canvas because it is broken.
8. **Redaction is applied here.** A field marked sensitive by its contract becomes `ExplanationValueDTO(kind="redacted", display="(redacted)", canonical=None, redacted=True)` in both `explanation` and `advanced`. Default-deny: an *unmarked* field on a command whose contract declares `sensitive_fields` is safe; a step whose contract is missing entirely gets its arguments rendered as `kind="unresolved"` with `canonical=None`.

### 5.2 `src/playbooks/artifact_diff.py`

```python
def diff_artifacts(
    base: PlaybookDefinition | None,
    target: PlaybookDefinition,
    *,
    base_ref: ArtifactRef | None,
    target_ref: ArtifactRef,
    contracts: ContractLookup,
    profiles: ProfileLookup,
) -> dict[str, Any]:                     # validates as PlaybookArtifactDiffResponse
```

- Matches rules by `rule_id`, steps by `(rule_id, step_id)`, edges by `GraphEdgeDTO.id`. Identity is artifact-local, which is exactly what Package 2 guarantees to be stable.
- `FieldChangeDTO.executable` is computed from the field's membership in the step's execution fingerprint input set — the same set `CommandContract.execution_fingerprint` uses (Package 1). Title, description and label changes are `executable=False`.
- `activation_blocked` is true when the target's health would be `invalid` or `stale_contract`; `activation_blockers` carries the human strings. The diff command **never** activates.
- `base=None` (first artifact) yields every rule/step/edge as `added` and `executable_change=True`.

### 5.3 `src/playbooks/run_overlay.py`

```python
def project_overlay(
    run: PlaybookRunV2,
    receipts: list[StepReceipt],
    definition: PlaybookDefinition,      # loaded from run.artifact_sha256, NOT the activation
    artifact: ArtifactRef,
    *,
    active_sha256: str | None,
    contracts: ContractLookup,
    receipt_limit: int = 500,
) -> dict[str, Any]:                     # validates as PlaybookRunOverlayResponse
```

- `artifact_is_active = (artifact.artifact_sha256 == active_sha256)`.
- Loop iterations: receipts carrying `iteration_index` are grouped under the owning `foreach` body node's `NodeOverlayDTO.iterations`; the node keeps `visit_count = len(iterations)` and one definition node. This is the spec's "shows a traversal count on the node and exposes individual iterations in the inspector rather than drawing forty copies".
- Edge traversal comes from `receipt.selected_transition`, mapped through the same `f"{rule_id}::{step_id}::{outcome}"` rule as the projector — so `EdgeOverlayDTO.edge_id` always joins a `GraphEdgeDTO.id`. A test asserts every overlay edge id exists in the projected graph for the same artifact.
- `truncated=True` when `receipt_total > receipt_limit`; the newest `receipt_limit` receipts are returned. Never silently drop.

### 5.4 `src/commands/playbook_v2_commands.py`

One mixin, seven `_cmd_*` coroutines, added to `CommandHandler`'s bases immediately after `PlaybookCommandsMixin` (`src/commands/handler.py:315`). Every command:

- returns `{"error": "..."}` on bad input (the established convention — `playbook_commands.py:1186`);
- returns `{"error": "playbook v2 api is disabled (playbooks.v2_api=false)"}` when the flag is off;
- is added to `PAUSED_PLAYBOOK_COMMANDS` (`handler.py:159`) so `playbooks.enabled=false` still pauses it.

| Command | Args | Notes |
|---|---|---|
| `playbook_v2_graph` | `playbook_id` (req), `artifact_sha256`, `event_type`, `direction`, `include_advanced` | Defaults to the **active** artifact; `artifact_sha256` selects a specific one (this is how the overlay pins). `include_advanced=false` omits `NodeAdvancedDTO.typed_step` bodies to keep the payload small — the field stays present with `typed_step={}` so the type never changes |
| `playbook_activation_health` | `playbook_id`, `scope`, `health` (filter) | All activations when `playbook_id` is absent |
| `playbook_activate` | `playbook_id` (req), `artifact_sha256` (req), `enabled` (default `true`), `acknowledge_diff` (req when `executable_change`) | Write. Gated by `playbooks.v2_activation_writes`. Refuses when health would be `invalid`; refuses without `acknowledge_diff` when the diff against the currently active artifact is executable (§7.3) |
| `playbook_artifact_diff` | `playbook_id` (req), `target_sha256` (req), `base_sha256` (defaults to active) | Read-only |
| `playbook_pending_events` | `playbook_id`, `reason`, `limit` (default 100) | Read-only |
| `playbook_pending_event_action` | `action` (`dispatch`\|`discard`, req), `pending_event_ids` (req, non-empty) | Write. `dispatch` re-enters `PlaybookEngine.dispatch_event`; it does **not** re-implement matching |
| `playbook_run_overlay` | `run_id` (req), `receipt_limit` | Read-only. Loads the definition by the run's pinned hash |

---

## 6. Dashboard design

### 6.1 Files created (`dashboard/src/pages/playbook-graph-v2/`)

| File | Responsibility |
|---|---|
| `types.ts` | Geometry constants, `StepKind`→tone map, `EdgeKind`→stroke map (dash patterns so kind survives colour-blindness, as V1 does at `playbook-graph/types.ts:47`), `NEUTRAL_EDGE_STYLE`, node-type labels |
| `layout.ts` | Pure: `GraphLayoutDTO` + nodes/edges → xyflow `Node[]`/`Edge[]` including one `group` node per rule cluster; returns `droppedEdgeCount` like V1 |
| `PlaybookSemanticGraphView.tsx` | Tab body: event selector, canvas, inspector, diagnostics banner, overlay picker; owns selected-node/selected-event/advanced state |
| `PlaybookSemanticGraphCanvas.tsx` | xyflow wrapper; pan/zoom/fit/Escape-to-clear identical to `PlaybookGraphCanvas.tsx:70-140`; edge legend panel |
| `EventScopeSelector.tsx` | "All events" + one option per `EventGroupDTO`; a `<select>` with a real label |
| `RuleClusterNode.tsx` | Non-interactive xyflow group node: rule name, event chip, cluster diagnostics count |
| `StepNodeCard.tsx` | Compact card for all seven kinds (§6.2); one `<button>` like V1 so pointer and Enter/Space share one control |
| `IntentSections.tsx` | Renders `StepExplanationDTO` (effects, inputs, result, outcomes). Used by **both** card and inspector — the spec's "node cards and the inspector consume the same explanation payload" is a shared component, not a convention |
| `SemanticNodeInspector.tsx` | Full intent card + AI/loop/wait blocks + source ref + Advanced toggle. Collapsed/absent when nothing is selected |
| `AdvancedNodeDetail.tsx` | `NodeAdvancedDTO`: typed JSON, resolved inputs, result schema, retry, idempotency, redaction table, execution fingerprint |
| `DiagnosticsBanner.tsx` | Graph- and rule-level diagnostics; links to the offending node |
| `ArtifactDiffPanel.tsx` | Rule/step/edge diff list, executable-vs-presentation split, blockers |
| `ActivationPanel.tsx` | Artifact list, active hash, health + reasons, activate button (diff acknowledgement required) |
| `PendingEventsPanel.tsx` | Pending events with reason, age, and dispatch/discard actions |
| `RunOverlayPanel.tsx` | Run picker, pinned-artifact banner, per-node state, loop iteration list, receipt detail |
| `__tests__/fixtures.ts` | The §10 fixture, typed against the generated client |
| `__tests__/*.test.tsx` | One suite per component (§12) |

### 6.2 The compact card contract (what each kind shows without selection)

| Step kind | Line 1 | Line 2 | Badges |
|---|---|---|---|
| `command` | `explanation.title` | `effect_summary` (e.g. "Create or reuse the matching task") | result binding, idempotency, retry |
| `llm` | title | declared outcome choices, comma-joined | `profile_id`, budget (`max_total_tokens`), "AI" |
| `agent_task` | title | objective, one line | `profile_id`, wait/no-wait, `cancel_child` |
| `decision` | title | rendered condition summary + case count | — |
| `wait` | title | `wait.wait_kind` + `wait.awaited` | timeout |
| `foreach` | title | `loop.collection.display` → `loop.item_binding` | failure policy |
| `terminal` | title | `terminal_outcome` | — |

Every card shows its outcome ports as labelled anchors on the card edge, and every port is the `source_port` of exactly one edge. `out_degree` mismatching the rendered port count is a test failure, not a visual glitch.

### 6.3 Layout and cluster rendering

`layout.ts` scales `GridPositionDTO` to pixels exactly as V1 does (`playbook-graph/layout.ts:29` `toPixels`), then adds, for each `ClusterBoundsDTO`, an xyflow node of `type: "ruleCluster"` sized to the bounds plus padding, with `zIndex` below the step nodes and `selectable: false`. Step nodes set `parentId` to their cluster and use cluster-relative positions — xyflow's built-in parenting keeps a cluster visually cohesive without a second layout engine. **Do not introduce dagre here**: the backend owns rank and order (V1's stated invariant, `playbook-graph/layout.ts:38`), and a client-side re-layout would make the rendered graph a second interpretation of the artifact.

### 6.4 Hooks (`dashboard/src/api/hooks.ts`)

```ts
export const playbookV2GraphKey = (playbookId: string, artifactSha?: string, eventType?: string) =>
  ["playbook-v2-graph", playbookId, artifactSha ?? "active", eventType ?? "all"] as const;

usePlaybookV2Graph(playbookId?, opts?: {artifactSha?, eventType?})   // enabled: !!playbookId
usePlaybookActivationHealth(playbookId?)                             // refetchInterval: 30_000
usePlaybookArtifactDiff(playbookId?, targetSha?, baseSha?)           // enabled: !!targetSha
useSetPlaybookActivation()                                           // mutation
usePlaybookPendingEvents(playbookId?)                                // refetchInterval: 30_000
usePlaybookPendingEventAction()                                      // mutation
usePlaybookRunOverlay(runId?, opts?: {live?: boolean})               // refetchInterval: live ? 5_000 : false
```

Both mutations invalidate `["playbook-v2-graph", playbookId]`, `["playbook-activation-health"]`, `["playbook-pending-events", playbookId]` in `onSettled` — the same pattern as `useRunPlaybook` (`hooks.ts:805`). `usePlaybookV2Graph` is *not* polled: an artifact is immutable, so the only thing that changes is which one is active, and that changes only through a mutation this session made or a 30 s health refetch.

### 6.5 Where it mounts

`dashboard/src/pages/PlaybookDetail.tsx:20` gains a fourth tab:

```ts
type TabId = "source" | "graph" | "semantic" | "runs";
// { id: "semantic", label: "Semantic graph" } — rendered only when the
// activation-health query reports an activation for this playbook.
```

The existing `graph` tab and every V1 component are untouched. Package 7 deletes the `graph` tab, `dashboard/src/pages/playbook-graph/`, and renames `semantic` → `graph`.

### 6.6 Accessibility and ergonomics (carried from V1, tested in C6)

- canvas is `role="region"` with `aria-label`, `tabIndex={0}`, Escape clears selection and returns focus (V1 `PlaybookGraphCanvas.tsx:70-86`);
- each card is a `<button>` with `aria-label` and `aria-pressed`;
- each edge carries `ariaLabel` naming kind, source and target (V1 `layout.ts:81`);
- the event selector is a labelled `<select>`, and changing it never re-fits the camera;
- the inspector is an `<aside aria-label="Node inspector">` that is absent (not an empty panel) when nothing is selected;
- Advanced is a toggle whose state persists across selection changes within one mount, so an operator inspecting five nodes in Advanced mode does not re-open it five times.

---

## 7. Security analysis

### 7.1 New boundaries introduced

| Boundary | Threat | Control |
|---|---|---|
| `playbook_activate` (write) | An agent session activates a hostile artifact | The command is **not** added to `AGENT_COMMAND_SET` (`src/api/scope.py:15`), so `check_request_scope` refuses it for every non-elevated session before dispatch. With Package 0 landed it additionally needs an `aq_commands` capability no worker profile holds. Two independent gates |
| `playbook_pending_event_action` (write, `dispatch`) | Replaying a held event to trigger execution at will | Same scope + capability gates. `dispatch` re-enters `PlaybookEngine.dispatch_event` with the **server-derived** `ExecutionPrincipal` of the operator request, never a principal from the stored event |
| Graph/diff/overlay reads | Leaking secrets held in bindings, prompts, or command arguments | Redaction is applied in `graph_projection` and `run_overlay` (§5.1.8, §5.3), driven by contract `sensitive_fields`, **default-deny** for receipts. Nothing reaches the DTO unredacted, so there is no client-side redaction to bypass |
| `SourceRefDTO.excerpt` | Markdown source leaking through a graph read to a caller who cannot read the vault | Excerpt is capped at 400 characters and omitted entirely when the request scope is not local/elevated. The `path` is vault-relative and never absolute |
| `NodeAdvancedDTO.typed_step` | The canonical step JSON is the richest payload in the package | It is the artifact, which the same caller can already read through `playbook_v2_graph`; redaction is applied to values inside it before serialization, so it is not a redaction bypass. Verified by `test_advanced_typed_step_is_redacted_like_the_explanation` |

### 7.2 What this package explicitly does not do

- It does not add any command to `AGENT_COMMAND_SET`, `_TRIAGE_COMMANDS`, or any other server-owned allowlist. Package 0's plan (§1.5) is emphatic that widening those lists is the exact failure mode the security baseline exists to prevent.
- It does not accept a caller-supplied principal, profile, capability set or scope. All seven commands read `_scope` only through the established server-derived path (`src/api/execute.py:58` strips a client `_scope` before `execute.py:71` injects the middleware-derived one before injecting the middleware-derived one).
- It does not let a *displayed* value influence execution. The graph is a projection; nothing in it is written back.

### 7.3 The activation review gate

`playbook_activate` refuses unless:

1. `playbooks.v2_activation_writes` is enabled; **and**
2. the target artifact's computed health is not `invalid`; **and**
3. either the diff against the currently active artifact has `executable_change=False`, or the caller passed `acknowledge_diff=<target_sha256>` — the literal hash, so an acknowledgement cannot be replayed against a different artifact.

Every refusal returns `blocked=true` with machine-readable `blockers`, and the attempt is logged with `playbook_id`, both hashes, and the requesting principal. This is the API-level form of "Diff review precedes activation".

### 7.4 Failure behavior

- Artifact file missing or hash mismatch on load → health `unavailable`, an `error` diagnostic, **the graph still renders** from the activation record's metadata with zero nodes and a banner. An operator must never see a blank tab with no explanation.
- Contract fingerprint mismatch → health `stale_contract`, a per-node `stale_contract` diagnostic on every affected command node, and `activation_blocked` on any diff targeting it. Runs are not started; events land in `playbook_pending_events` (Package 3) and appear in `PendingEventsPanel`.
- A receipt referencing a step id absent from the pinned artifact → an `error` diagnostic plus a `skipped` node overlay entry. Never a crash, never a silently dropped receipt.

---

## 8. Feature flags and their removal package

| Flag | Location | Default | Owner | Removed in |
|---|---|---|---|---|
| `playbooks.v2_api` | `src/config.py::PlaybooksConfig.v2_api: bool = False` | `False` | Package 5 | **Package 7**, together with the V1 graph command |
| `playbooks.v2_activation_writes` | `src/config.py::PlaybooksConfig.v2_activation_writes: bool = False` | `False` | Package 5 | **Package 7** |

Two flags, not one, because roadmap Package 5's rollback boundary requires it: "Activation writes are independently feature-gated from graph reads." An operator can read, diff and review the entire V2 surface with writes still disabled — which is precisely the Package 6 review posture.

The dashboard reads the flags indirectly: the `semantic` tab renders when `playbook_activation_health` succeeds, and hides the activate button when `playbook_activate` returns the disabled error. No flag value is shipped to the client.

---

## 9. Storage and Alembic — conditional, additive only

**Run §3.2 check 2 first.** Package 5 is a read surface; the only state it writes is *who activated what, when* and *how a pending event was resolved*. If Package 3 already shipped those columns, **this package ships no migration and §9 is a no-op**. Record the outcome in the commit message either way.

If they are absent, one additive revision, `playbook_v2_operator_audit`:

```python
def upgrade() -> None:
    op.add_column("playbook_activations", sa.Column("activated_by", sa.String(), nullable=True))
    op.add_column("playbook_pending_events", sa.Column("resolved_at", sa.Float(), nullable=True))
    op.add_column("playbook_pending_events", sa.Column("resolved_by", sa.String(), nullable=True))
    op.add_column("playbook_pending_events", sa.Column("resolution", sa.String(), nullable=True))


def downgrade() -> None:
    # SQLite cannot DROP COLUMN outside a table rebuild; batch mode rebuilds.
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.drop_column("resolution")
        batch.drop_column("resolved_by")
        batch.drop_column("resolved_at")
    with op.batch_alter_table("playbook_activations") as batch:
        batch.drop_column("activated_by")
```

- **All four columns are nullable with no server default**, so `upgrade` is a metadata-only operation on PostgreSQL (no table rewrite) and a plain `ALTER TABLE ADD COLUMN` on SQLite. Existing rows read `NULL` = "resolved before auditing existed", which the DTO renders as `activated_by: null`.
- **`resolution`** stores `"dispatched"` or `"discarded"`, matching `PendingAction`. It is a plain string, not an enum type: PostgreSQL enums require a separate `CREATE TYPE`/`DROP TYPE` in both directions and the codebase's existing status columns are strings (`src/database/tables.py`).
- **Downgrade is exercised** — `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` on SQLite, then the same against PostgreSQL on `:5533` (project convention: PostgreSQL is production).
- `tables.py` is edited in the same commit as the revision, per CLAUDE.md ("Never edit `tables.py` without generating a migration").

---

## 10. Fixture data

### 10.1 `tests/fixtures/playbooks/v2/review-pipeline.artifact.json`

One artifact exercising **every step kind, every edge kind, branching, convergence, a loop, a wait, and two rules on different events**. Representative, not placeholder: it is a V2 transcription of the shipped review pipeline.

```json
{
  "schema_version": 2,
  "id": "default-pipeline",
  "version": 5,
  "scope": {"type": "system"},
  "source_hash": "sha256:6f1c0d2b9a4e7f38c5b1de20a7f4c8931ee5b0a6d2c74f9138ab5e0c7d41928f",
  "compiled_at": "2026-09-01T00:00:00Z",
  "purpose": "routine",
  "compiled_against": {
    "commands": {
      "ensure_task": "sha256:aa11bb22cc33dd44ee55ff6677889900aabbccddeeff00112233445566778899",
      "gate_create": "sha256:bb22cc33dd44ee55ff6677889900aabbccddeeff001122334455667788990011",
      "list_tasks":  "sha256:cc33dd44ee55ff6677889900aabbccddeeff00112233445566778899001122"
    },
    "profiles": {
      "reviewer": "sha256:dd44ee55ff6677889900aabbccddeeff0011223344556677889900112233"
    }
  },
  "rules": [
    {
      "id": "review-on-task-completed",
      "name": "Open review for a completed task",
      "trigger": {"event_type": "task.completed", "filter": {"task_type": "code"}},
      "entry_step": "ensure-review-task",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 18, "end_line": 46,
                 "heading": "Open review for a completed task"}
    },
    {
      "id": "sweep-on-spec-created",
      "name": "Sweep downstream tasks for a new spec",
      "trigger": {"event_type": "spec.created", "filter": null},
      "entry_step": "list-downstream",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 48, "end_line": 71,
                 "heading": "Sweep downstream tasks for a new spec"}
    }
  ],
  "steps": {
    "ensure-review-task": {
      "type": "command", "rule": "review-on-task-completed",
      "title": "Ensure a review task", "command": "ensure_task",
      "inputs": {
        "project_id": {"type": "event_ref", "path": "project_id"},
        "title": {"type": "template", "parts": [
          {"type": "literal", "value": "Review: "},
          {"type": "event_ref", "path": "title"}
        ]}
      },
      "save_result_as": "review",
      "transitions": {"success": "classify-risk", "failure": "review-unavailable",
                      "runtime_error": "review-unavailable"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 20, "end_line": 27}
    },
    "classify-risk": {
      "type": "llm", "rule": "review-on-task-completed",
      "title": "Classify review risk", "profile_id": "reviewer",
      "output_schema": {"type": "object", "properties": {
        "risk": {"enum": ["low", "high"]}}, "required": ["risk"]},
      "budget": {"max_calls": 2, "max_output_tokens": 1024,
                 "max_total_tokens": 8000, "timeout_seconds": 120},
      "save_result_as": "risk",
      "transitions": {"low": "await-approval", "high": "escalate",
                      "invalid_output": "review-unavailable",
                      "budget_exceeded": "review-unavailable",
                      "provider_error": "review-unavailable",
                      "timed_out": "review-unavailable",
                      "cancelled": "cancelled-end"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 29, "end_line": 34}
    },
    "escalate": {
      "type": "agent_task", "rule": "review-on-task-completed",
      "title": "Escalate to a senior reviewer", "profile_id": "reviewer",
      "objective": "Re-review the change and record the riskiest line",
      "wait_for_completion": true, "cancel_child": false, "timeout_seconds": 3600,
      "save_result_as": "escalation",
      "transitions": {"completed": "await-approval", "failed": "review-unavailable",
                      "timed_out": "review-unavailable", "cancelled": "cancelled-end"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 36, "end_line": 39}
    },
    "await-approval": {
      "type": "wait", "rule": "review-on-task-completed",
      "title": "Wait for human approval", "wait_kind": "human",
      "correlation_key": {"type": "binding_ref", "binding": "review", "path": "task_id"},
      "timeout_seconds": 86400, "save_result_as": "approval",
      "transitions": {"approve": "done", "revise": "ensure-review-task",
                      "timed_out": "review-unavailable"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 41, "end_line": 44}
    },
    "review-unavailable": {"type": "terminal", "rule": "review-on-task-completed",
      "title": "Review unavailable", "outcome": "failed",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 45, "end_line": 45}},
    "cancelled-end": {"type": "terminal", "rule": "review-on-task-completed",
      "title": "Cancelled", "outcome": "cancelled",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 46, "end_line": 46}},
    "done": {"type": "terminal", "rule": "review-on-task-completed",
      "title": "Review complete", "outcome": "completed",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 46, "end_line": 46}},

    "list-downstream": {
      "type": "command", "rule": "sweep-on-spec-created",
      "title": "List downstream tasks", "command": "list_tasks",
      "inputs": {"project_id": {"type": "event_ref", "path": "project_id"},
                 "status": {"type": "literal", "value": "READY"}},
      "save_result_as": "downstream",
      "transitions": {"success": "for-each-task", "failure": "sweep-failed",
                      "runtime_error": "sweep-failed"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 50, "end_line": 55}
    },
    "for-each-task": {
      "type": "foreach", "rule": "sweep-on-spec-created",
      "title": "For each downstream task",
      "collection": {"type": "binding_ref", "binding": "downstream", "path": "tasks"},
      "item_binding": "task", "failure_policy": "collect",
      "body_entry": "open-gate", "continuation": "sweep-done",
      "transitions": {"completed": "sweep-done", "failed": "sweep-failed"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 57, "end_line": 62}
    },
    "open-gate": {
      "type": "command", "rule": "sweep-on-spec-created",
      "title": "Open a spec-ingest gate", "command": "gate_create",
      "inputs": {"project_id": {"type": "event_ref", "path": "project_id"},
                 "gate_type": {"type": "literal", "value": "review"},
                 "title": {"type": "template", "parts": [
                   {"type": "literal", "value": "Spec ingest: "},
                   {"type": "loop_ref", "binding": "task", "path": "title"}]}},
      "save_result_as": "gate",
      "transitions": {"success": "check-gate", "failure": "sweep-failed",
                      "runtime_error": "sweep-failed"},
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 63, "end_line": 66}
    },
    "check-gate": {
      "type": "decision", "rule": "sweep-on-spec-created",
      "title": "Was the gate already open?",
      "cases": [{"when": {"type": "comparison", "op": "eq",
                          "left": {"type": "binding_ref", "binding": "gate", "path": "created"},
                          "right": {"type": "literal", "value": false}},
                 "goto": "for-each-task", "label": "already open"}],
      "default": "for-each-task",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 67, "end_line": 69}
    },
    "sweep-done": {"type": "terminal", "rule": "sweep-on-spec-created",
      "title": "Sweep complete", "outcome": "completed",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 70, "end_line": 70}},
    "sweep-failed": {"type": "terminal", "rule": "sweep-on-spec-created",
      "title": "Sweep failed", "outcome": "failed",
      "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 71, "end_line": 71}}
  }
}
```

Properties this fixture is chosen to have, each load-bearing for a test:

- **two rules on two events** → event grouping and filtering;
- **convergence** — `classify-risk:low` and `escalate:completed` both target `await-approval`;
- **loop-back** — `await-approval:revise` → `ensure-review-task`;
- **a loop whose body re-enters the loop node** — `check-gate` → `for-each-task` (both the case and the default), giving two distinct edges with the same `(source, target)` pair that must remain independently selectable;
- **three terminals in one rule** — proves shared terminals stay inside their cluster;
- **every reserved outcome mapped on the LLM node** — proves reserved edges render and are labelled `reserved`;
- **a template, an event ref, a binding ref, a loop ref, a literal and a comparison** — one of every expression kind in §4.1's `ValueKind`.

### 10.2 Companion fixtures

- `tests/fixtures/playbooks/v2/review-pipeline.v6.artifact.json` — the same artifact with (a) a changed `ensure_task` title template (executable), (b) a reworded step title (presentation-only), (c) one added `decision` case. Drives the diff suite: `executable_change=True`, `presentation_change_count=1`.
- `tests/fixtures/playbooks/v2/review-pipeline.receipts.json` — 11 receipts for one run of `review-on-task-completed`: `ensure-review-task` attempt 1 `failure`, attempt 2 `success`, `classify-risk` `high`, `escalate` `completed`, `await-approval` `approve`, `done`; plus 5 `open-gate` receipts with `iteration_index` 0–4 from a `sweep-on-spec-created` run, one of which is `failed` under `failure_policy: collect`. Drives loop-iteration overlay, multi-attempt overlay and `truncated=False`.
- `dashboard/src/pages/playbook-graph-v2/__tests__/fixtures.ts` — the TS mirror, built from the generated client types so a DTO rename breaks the dashboard build rather than a runtime assertion. It is generated once by hand from the backend fixture and asserted equivalent by `tests/test_playbook_v2_api_dtos.py::test_dashboard_fixture_matches_backend_projection`, which loads the JSON the projector produces and compares node/edge ids and kinds against a JSON copy exported under `dashboard/src/pages/playbook-graph-v2/__tests__/graph.fixture.json`.

---

## 11. API request/response examples

In every example below `"…": "…"` and `sha256:31c9…8f0a` mark elided text for readability; the wire format carries full 64-hex digests and every declared field, since the models are `extra="forbid"`.

### 11.1 `POST /api/playbook/v2-graph`

```json
{"playbook_id": "default-pipeline", "event_type": "task.completed", "direction": "TD"}
```

```json
{
  "success": true,
  "artifact": {
    "playbook_id": "default-pipeline",
    "artifact_sha256": "sha256:31c9…8f0a",
    "schema_generation": 2,
    "contract_fingerprint": "sha256:7ab2…44c1",
    "source_digest": "sha256:6f1c…928f",
    "compiler_build": "aq-compiler/2026.09.01+30b86a68",
    "compiled_at": "2026-09-01T00:00:00Z",
    "version": 5
  },
  "activation": {
    "playbook_id": "default-pipeline", "scope": "system", "scope_identifier": null,
    "enabled": true, "active_artifact_sha256": "sha256:31c9…8f0a",
    "health": "ready", "reasons": [], "activated_at": 1788300000.0,
    "activated_by": "local", "pending_event_count": 0, "running_count": 1
  },
  "purpose": "routine",
  "event_groups": [
    {"event_type": "task.completed", "rule_ids": ["review-on-task-completed"], "node_count": 7, "edge_count": 17},
    {"event_type": "spec.created", "rule_ids": ["sweep-on-spec-created"], "node_count": 6, "edge_count": 11}
  ],
  "rules": [{
    "rule_id": "review-on-task-completed",
    "name": "Open review for a completed task",
    "event_type": "task.completed",
    "trigger_filter": {"task_type": "code"},
    "entry_step_id": "ensure-review-task",
    "step_ids": ["ensure-review-task","classify-risk","escalate","await-approval",
                 "done","review-unavailable","cancelled-end"],
    "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 18, "end_line": 46,
               "heading": "Open review for a completed task", "excerpt": null},
    "diagnostics": []
  }],
  "nodes": [{
    "id": "ensure-review-task",
    "rule_id": "review-on-task-completed",
    "step_kind": "command",
    "title": "Ensure a review task",
    "description": null,
    "entry": true,
    "terminal_outcome": null,
    "explanation": {
      "title": "Ensure a review task exists",
      "effect_summary": "Create or reuse the matching task",
      "effects": [{"kind": "creates", "subject": "task",
                   "detail": "Creates the task when no matching one exists, otherwise reuses it",
                   "arguments": [], "conditional_on": null}],
      "inputs": [
        {"label": "Project", "source": "event", "required": true, "description": null,
         "value": {"kind": "event_ref", "display": "this event's project",
                   "canonical": {"type": "event_ref", "path": "project_id"},
                   "redacted": false, "type_name": "string"}},
        {"label": "Title", "source": "template", "required": true, "description": null,
         "value": {"kind": "template", "display": "\"Review: \" + event title",
                   "canonical": {"type": "template", "parts": [
                     {"type": "literal", "value": "Review: "},
                     {"type": "event_ref", "path": "title"}]},
                   "redacted": false, "type_name": "string"}}
      ],
      "result": {"label": "Saved as", "source": "derived", "required": true, "description": null,
                 "value": {"kind": "literal", "display": "review", "canonical": "review",
                           "redacted": false, "type_name": "TaskRef"}},
      "outcomes": [
        {"outcome": "success", "label": "Success", "target_step_id": "classify-risk",
         "target_title": "Classify review risk", "reserved": false, "terminal_outcome": null},
        {"outcome": "failure", "label": "Failure", "target_step_id": "review-unavailable",
         "target_title": "Review unavailable", "reserved": false, "terminal_outcome": null},
        {"outcome": "runtime_error", "label": "Runtime error", "target_step_id": "review-unavailable",
         "target_title": "Review unavailable", "reserved": true, "terminal_outcome": null}
      ],
      "contract_fingerprint": "sha256:aa11…8899",
      "renderer": "contract"
    },
    "badges": [{"kind": "idempotency", "label": "idempotent", "value": "run:step:attempt"}],
    "ai": null, "loop": null, "wait": null,
    "source": {"path": "system/playbooks/default-pipeline.md", "start_line": 20, "end_line": 27,
               "heading": null, "excerpt": null},
    "advanced": {
      "typed_step": {"type": "command", "command": "ensure_task", "…": "…"},
      "resolved_inputs": [], "result_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}},
      "retry": {"max_attempts": 2, "backoff_seconds": 5.0, "retry_on": ["unavailable"]},
      "idempotency": {"supported": true, "key_template": "<run_id>:<step_id>:<attempt>", "retry_safe": true},
      "redaction": [], "execution_fingerprint": "sha256:aa11…8899"
    },
    "diagnostics": [],
    "out_degree": 3,
    "position": {"x": 0, "y": 0}
  }],
  "edges": [
    {"id": "review-on-task-completed::ensure-review-task::success",
     "rule_id": "review-on-task-completed", "source": "ensure-review-task",
     "source_port": "success", "target": "classify-risk", "outcome": "success",
     "label": "success", "kind": "success", "reserved": false, "condition": null},
    {"id": "review-on-task-completed::ensure-review-task::runtime_error",
     "rule_id": "review-on-task-completed", "source": "ensure-review-task",
     "source_port": "runtime_error", "target": "review-unavailable", "outcome": "runtime_error",
     "label": "runtime error", "kind": "runtime_error", "reserved": true, "condition": null}
  ],
  "layout": {"direction": "TD",
             "grid_positions": {"ensure-review-task": {"x": 0, "y": 0}},
             "cluster_bounds": {"review-on-task-completed": {"x": 0, "y": 0, "width": 3, "height": 5}}},
  "diagnostics": [],
  "legend": {"step_kinds": {"command": "command"}, "edge_kinds": {"success": "on success"}}
}
```

### 11.2 `POST /api/playbook/activate` — blocked by an unacknowledged executable diff

```json
{"playbook_id": "default-pipeline", "artifact_sha256": "sha256:9de1…0b77", "enabled": true}
```

```json
{
  "success": true,
  "activation": {"playbook_id": "default-pipeline", "scope": "system", "scope_identifier": null,
                 "enabled": true, "active_artifact_sha256": "sha256:31c9…8f0a",
                 "health": "ready", "reasons": [], "activated_at": 1788300000.0,
                 "activated_by": "local", "pending_event_count": 0, "running_count": 1},
  "previous_artifact_sha256": "sha256:31c9…8f0a",
  "changed": false,
  "blocked": true,
  "blockers": ["executable change requires acknowledge_diff=sha256:9de1…0b77"]
}
```

Note `changed: false` and the **unchanged** activation echoed back: a blocked activation is not a partial one.

### 11.3 `POST /api/playbook/run-overlay` — the pinning guarantee

```json
{"run_id": "run_01J8Z…"}
```

```json
{
  "success": true, "run_id": "run_01J8Z…",
  "artifact": {"playbook_id": "default-pipeline", "artifact_sha256": "sha256:31c9…8f0a", "…": "…"},
  "artifact_is_active": false,
  "rule_id": "review-on-task-completed",
  "lifecycle": "completed",
  "current_step_id": null,
  "nodes": [
    {"step_id": "ensure-review-task", "state": "completed", "visit_count": 2,
     "last_outcome": "success", "receipt_ids": ["rc_1", "rc_2"], "iterations": []},
    {"step_id": "open-gate", "state": "completed", "visit_count": 5, "last_outcome": "success",
     "receipt_ids": ["rc_7","rc_8","rc_9","rc_10","rc_11"],
     "iterations": [{"index": 0, "item_display": "task AQ-14", "outcome": "success",
                     "receipt_ids": ["rc_7"], "started_at": 1788300010.0, "completed_at": 1788300011.2}]}
  ],
  "edges": [{"edge_id": "review-on-task-completed::ensure-review-task::success",
             "traversal_count": 1, "last_traversed_at": 1788300004.0}],
  "receipts": [{"receipt_id": "rc_1", "step_id": "ensure-review-task", "rule_id": "review-on-task-completed",
                "step_kind": "command", "attempt": 1, "iteration_index": null, "outcome": "failure",
                "selected_edge_id": "review-on-task-completed::ensure-review-task::failure",
                "started_at": 1788300001.0, "completed_at": 1788300002.0, "duration_seconds": 1.0,
                "inputs": [], "result": null, "token_usage": null,
                "idempotency_key": "run_01J8Z…:ensure-review-task:1",
                "principal_fingerprint": "sha256:5c0e…9a13", "profile_id": null,
                "contract_fingerprint": "sha256:aa11…8899",
                "error": "project not found", "wait": null, "cancellation": null}],
  "bindings": [], "operator_decision": null,
  "budget": {"llm_calls": 1, "total_tokens": 1840, "max_total_tokens": 8000, "cost_usd": null},
  "truncated": false, "receipt_total": 11
}
```

The dashboard then calls `playbook_v2_graph({playbook_id, artifact_sha256: "sha256:31c9…8f0a"})` — never the active artifact — and shows an "older artifact" banner because `artifact_is_active` is false.

---

## 12. Tasks

Every task names its failing assertion first. `aq test` (not bare `pytest`) for anything past a single file; `npm -w dashboard test -- --run <path>` for focused Vitest.

### 12.1 Commit 1 — `feat: expose artifact aware semantic graph api`

**This commit is the parallelism unlock.** It checks in §4 whole, plus the graph/health/pending-event reads and both regenerated clients. Backend tasks T-8+ and dashboard tasks T-11+ may then run in parallel.

| Task | Red | Green |
|---|---|---|
| T-1 | `tests/test_playbook_v2_api_dtos.py::test_every_v2_model_forbids_extra_keys` — iterate every `BaseModel` subclass in `src.api.models.playbook_v2`, assert `model_config["extra"] == "forbid"`. Fails: module missing | Create `src/api/models/playbook_v2.py` exactly as §4 |
| T-2 | `…::test_response_models_registered_for_seven_commands` — `get_all_response_models()` contains the seven names of §4.8. Fails: `playbook_v2` not in the aggregate | Add `playbook_v2` to the import + merge tuple in `src/api/models/__init__.py:48` |
| T-3 | `…::test_v2_commands_are_not_in_response_exclude_none` — the seven names are absent from `src.api.codegen.RESPONSE_EXCLUDE_NONE`. Fails only if someone adds them later; ships green as a ratchet | (no change — this is a pin) |
| T-4 | `tests/test_playbook_graph_projection.py::test_one_edge_per_transition_record` — load §10.1, project, and assert the projected edge-id set equals the set the test builds independently by walking the artifact's declared transitions (`f"{rule}::{step}::{outcome}"`), that ids are unique, and that `len(edges) == 28`. Fails: module missing | Create `src/playbooks/graph_projection.py` per §5.1 |
| T-5 | `…::test_timeout_edge_survives_when_it_shares_a_target` — a step whose `timed_out` and `failure` both target `review-unavailable` yields **two** edges. This is the V1 bug of §1.3 | Covered by T-4's implementation; the test is the regression pin |
| T-6 | `…::test_no_edge_crosses_a_rule_cluster` and `…::test_shared_terminal_titles_do_not_merge_nodes` | Rule-ownership assertions in the projector |
| T-7 | `…::test_event_filter_preserves_every_reachable_branch` — projecting with `event_type="task.completed"` yields the same node/edge sets as the unfiltered projection restricted to that rule, and `event_groups` still lists both events | `event_type` filter in `project_graph` |
| T-8 | `…::test_explanation_is_copied_not_rederived` — monkeypatch `explain_step` to a sentinel; assert it appears verbatim in `nodes[0].explanation` | Projector delegates to Package 1 |
| T-9 | `…::test_missing_contract_yields_canonical_renderer_and_error_diagnostic` — a step referencing an unregistered command still produces a node, `renderer == "canonical"`, one `error` diagnostic `code == "unknown_command"` | §5.1 rule 7 |
| T-10 | `tests/test_api_playbook_v2_commands.py::test_v2_graph_returns_the_active_artifact` and `::test_v2_graph_honours_artifact_sha256`; `::test_v2_graph_refused_when_flag_disabled` | Create `src/commands/playbook_v2_commands.py` with `_cmd_playbook_v2_graph`, `_cmd_playbook_activation_health`, `_cmd_playbook_pending_events`; register in `src/tools/definitions.py`, `PAUSED_PLAYBOOK_COMMANDS`, `CommandHandler` bases; add both flags to `src/config.py` |
| T-11 | `tests/test_api_client_contract.py` (existing) fails after T-10 on both counts: the app serves seven operations the committed client lacks, and `test_committed_openapi_json_matches_the_live_app_surface` sees the spec drift | Regenerate offline (no daemon): `./scripts/regenerate-api-client.sh --offline` then `./scripts/regenerate-ts-client.sh --offline`; commit `openapi.json`, `packages/aq-client/`, `packages/aq-ts-client/src/` in this same commit |

### 12.2 Commit 2 — `feat: render rich typed playbook nodes and exact edges`

| Task | Red | Green |
|---|---|---|
| T-12 | `playbook-graph-v2/__tests__/layout.test.ts::maps every DTO edge to exactly one flow edge` — 23 DTO edges → 23 flow edges, ids preserved verbatim (not positional) | `layout.ts` |
| T-13 | `…::places every step node inside its rule cluster` — each node's `parentId` is its `rule_id` and its position lies within `cluster_bounds` | cluster group nodes in `layout.ts` |
| T-14 | `StepNodeCard.test.tsx::renders the compact contract per step kind` — one case per row of §6.2, asserting the visible text comes from `explanation`, never from `advanced.typed_step` | `StepNodeCard.tsx` + `IntentSections.tsx` |
| T-15 | `…::renders one labelled outcome port per outgoing edge` — port count == `out_degree`, labels == outcomes | port rendering |
| T-16 | `PlaybookSemanticGraphCanvas.test.tsx::keeps every edge kind visually distinct` — distinct `strokeDasharray` per `EdgeKind`; unknown kind falls back to `NEUTRAL_EDGE_STYLE` and stays labelled | `types.ts` stroke map |
| T-17 | `…::two edges between the same pair remain independently selectable` — uses the `check-gate` case+default pair from §10.1 | distinct edge ids reach xyflow |
| T-18 | `EventScopeSelector.test.tsx::lists every event group and an All events option`, `::refetches with the selected event_type` | selector + `usePlaybookV2Graph` option |

### 12.3 Commit 3 — `feat: add source inspector and advanced details`

| Task | Red | Green |
|---|---|---|
| T-19 | `SemanticNodeInspector.test.tsx::shows inputs outputs outcomes and targets for a command node` — asserts every `OutcomeExplanationDTO.target_title` is visible | `SemanticNodeInspector.tsx` |
| T-20 | `…::shows profile capabilities budget and output schema for an AI node`; `…::shows delegation policy for an agent task node` | `AiNodeDetailDTO` block |
| T-21 | `…::shows wait kind correlation and timeout`, `…::shows loop collection item binding and failure policy` | wait/loop blocks |
| T-22 | `…::renders no inspector when nothing is selected` — asserts the `aside` is **absent**, not empty | conditional render |
| T-23 | `AdvancedNodeDetail.test.tsx::exposes typed json expressions ids fingerprints and redaction decisions`; `…::is not the default view` | Advanced toggle |
| T-24 | `…::renders a redacted value without its canonical payload` — `redacted:true` shows the display string and no JSON | redaction rendering |
| T-25 | `SemanticNodeInspector.test.tsx::links to the authoring markdown with path and line range` | `SourceRefDTO` rendering |
| T-26 | `DiagnosticsBanner.test.tsx::shows compile questions invalid references stale contracts and disabled activations without hiding the graph` — canvas still renders all nodes with a diagnostic present | `DiagnosticsBanner.tsx` |

### 12.4 Commit 4 — `feat: add artifact diff and activation review`

| Task | Red | Green |
|---|---|---|
| T-27 | `tests/test_playbook_artifact_diff.py::test_executable_change_detected` (§10.2 v6 fixture) and `::test_presentation_only_change_does_not_block` | `src/playbooks/artifact_diff.py` |
| T-28 | `…::test_first_artifact_diffs_against_none` | `base=None` path |
| T-29 | `tests/test_playbook_activation_commands.py::test_activate_requires_acknowledge_diff_for_executable_change` — asserts the §11.2 response, and that the DB activation is unchanged | `_cmd_playbook_activate` |
| T-30 | `…::test_activate_refuses_invalid_artifact`; `…::test_activate_refused_when_write_flag_disabled`; `…::test_activate_records_activated_by` | §7.3 gate |
| T-31 | `tests/test_api_scope.py::test_v2_activation_commands_are_out_of_scope_for_agent_sessions` — a non-elevated session token gets `out of scope: playbook_activate` | (no change — pin that the command was **not** added to `AGENT_COMMAND_SET`) |
| T-32 | `tests/test_playbook_pending_events_commands.py::test_dispatch_reenters_the_engine` (asserts `PlaybookEngine.dispatch_event` called with the server-derived principal) and `::test_discard_marks_resolved_without_dispatch` | `_cmd_playbook_pending_event_action` |
| T-33 | `ArtifactDiffPanel.test.tsx::separates executable from presentation-only changes`; `ActivationPanel.test.tsx::requires diff acknowledgement before enabling activate`; `::shows the active hash and health reasons` | diff + activation panels |
| T-34 | `PendingEventsPanel.test.tsx::lists reason and age and offers dispatch and discard` | pending panel |
| T-35 | Migration (only if §3.2 check 2 says the columns are absent): `tests/test_database.py::test_playbook_v2_operator_audit_columns` | §9 revision + `tables.py` |

### 12.5 Commit 5 — `feat: overlay exact artifact execution receipts`

| Task | Red | Green |
|---|---|---|
| T-36 | `tests/test_playbook_run_overlay.py::test_overlay_uses_the_pinned_artifact_not_the_activation` — activate a *newer* artifact, then overlay an old run; assert `artifact.artifact_sha256 == run.artifact_sha256` and `artifact_is_active is False` | `src/playbooks/run_overlay.py` |
| T-37 | `…::test_every_overlay_edge_id_exists_in_the_projected_graph` — join against `project_graph` output for the same artifact | shared edge-id rule |
| T-38 | `…::test_loop_iterations_are_listed_not_collapsed` — `open-gate` has `visit_count == 5` and five `iterations`, one `failed`, and the node is **one** node | iteration grouping |
| T-39 | `…::test_multiple_attempts_on_one_step_are_both_present` — `ensure-review-task` keeps both receipts with distinct `attempt` | attempt handling |
| T-40 | `…::test_receipt_cap_sets_truncated`; `…::test_receipt_for_unknown_step_yields_diagnostic_not_crash` | `receipt_limit`, resilience |
| T-41 | `tests/test_api_playbook_v2_commands.py::test_run_overlay_command_returns_pinned_artifact_ref` | `_cmd_playbook_run_overlay` |
| T-42 | `RunOverlayPanel.test.tsx::pins the graph to the runs artifact and warns when it is not active`; `::selects an iteration and shows its receipt` | `RunOverlayPanel.tsx` + `usePlaybookRunOverlay` |

### 12.6 Commit 6 — `test: cover graph semantics accessibility and generated clients`

| Task | Red | Green |
|---|---|---|
| T-43 | `tests/test_playbook_graph_projection.py::test_projection_is_deterministic` — project the §10.1 fixture twice, assert byte-identical canonical JSON | stable ordering |
| T-44 | `tests/test_playbook_v2_api_dtos.py::test_dashboard_fixture_matches_backend_projection` (§10.2) | export the JSON fixture |
| T-45 | `PlaybookSemanticGraphCanvas.test.tsx::preserves pan zoom fit selection and escape-to-clear` — mirrors `playbook-graph/__tests__/PlaybookGraphCanvas.test.tsx` assertions against the V2 canvas | — |
| T-46 | `…::every card is a button with an accessible name and pressed state`; `…::every edge carries an aria label naming kind source and target`; `…::the event selector has a label` | a11y fixes |
| T-47 | `dashboard/src/pages/__tests__/PlaybookDetail.test.tsx::shows the semantic graph tab only when an activation exists`, `::leaves the v1 graph tab intact` (extends the existing suite) | tab wiring in `PlaybookDetail.tsx` |
| T-48 | `tests/test_api_client_contract.py -q` green with the final DTO set; re-run both regeneration scripts and confirm a **clean** `git diff` (idempotent regeneration) | commit any drift |

---

## 13. Verification

### 13.1 Per-package required commands (roadmap §5, reconciled per §2)

```bash
# Backend — the suites this child plan names
aq test tests/test_playbook_graph_projection.py tests/test_playbook_artifact_diff.py \
        tests/test_playbook_run_overlay.py tests/test_playbook_v2_api_dtos.py
aq test tests/test_api_playbook_v2_commands.py tests/test_playbook_activation_commands.py \
        tests/test_playbook_pending_events_commands.py
# Regression: V1 surfaces untouched
aq test tests/test_api_playbook_graph_view.py tests/test_playbook_graph_view.py \
        tests/test_playbook_commands.py tests/test_api_scope.py tests/test_api_client_contract.py
ruff check src/api/models/playbook_v2.py src/commands/playbook_v2_commands.py \
           src/playbooks/graph_projection.py src/playbooks/artifact_diff.py \
           src/playbooks/run_overlay.py tests/test_playbook_graph_projection.py \
           tests/test_playbook_artifact_diff.py tests/test_playbook_run_overlay.py \
           tests/test_playbook_v2_api_dtos.py tests/test_api_playbook_v2_commands.py \
           tests/test_playbook_activation_commands.py tests/test_playbook_pending_events_commands.py

# Generated clients — regenerated offline, then asserted idempotent
./scripts/regenerate-api-client.sh --offline    # src/api/spec.py builds the spec in-process
./scripts/regenerate-ts-client.sh --offline
git diff --exit-code openapi.json packages/aq-client packages/aq-ts-client   # must be clean (idempotent)

# Dashboard — focused during work
npm -w dashboard test -- --run src/pages/playbook-graph-v2
# Dashboard — once, at the end (from the repo root so the TS client builds first)
npm run lint && npm run typecheck && npm run build
npm -w dashboard test -- --run src/pages/playbook-graph src/pages/playbook-graph-v2

# Area suite — once, before the exit gate (not repeatedly; supervisor guidance on solid-harbor.45)
aq test tests/test_playbook*.py tests/test_api_playbook*.py
```

Expected outcome for each: zero failures and zero `xpassed`.

### 13.2 Migration (only when §9 applies)

```bash
alembic heads && alembic upgrade head && alembic downgrade -1 && alembic upgrade head
aq test tests/test_database.py
```

Then the same three-step against PostgreSQL on `:5533` (`docker compose`), per the project's PostgreSQL-is-production convention. The four columns are nullable and default-free, so the PostgreSQL upgrade is metadata-only; the SQLite downgrade goes through `batch_alter_table`, which rebuilds the table — assert row counts before and after.

### 13.3 Manual scenario review (roadmap requires screenshots)

Run against a daemon with `playbooks.enabled: true`, `playbooks.v2_api: true`, `playbooks.v2_activation_writes: true`, and the §10 fixture installed as a real artifact. Capture one screenshot each:

1. **Branching** — `classify-risk` with its seven labelled outgoing edges, `low`/`high` visually distinct from the five reserved ones.
2. **Convergence** — `classify-risk:low` and `escalate:completed` both entering `await-approval`, both labels legible.
3. **Loop** — `for-each-task` with its body, `loop_back` from `check-gate`, and the traversal count from an overlay.
4. **AI node** — `classify-risk` selected, inspector showing `profile_id`, capability namespaces, capability fingerprint, budget and output schema.
5. **Invalid node** — an artifact whose `gate_create` contract fingerprint was bumped: `stale_contract` diagnostics visible, graph still fully drawn.
6. **Diff review** — v5 → v6, executable and presentation-only changes separated, activate disabled until acknowledged.
7. **Run overlay** — a completed run of the v5 artifact while v6 is active: the "older artifact" banner plus the traversed path.

Store them under `docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/` and link them from the Package 5 exit-gate evidence.

---

## 14. Mapping to the package exit gate

> **Exit gate:** An operator can answer, from the graph alone: what event enters this rule, what each node does, which data it reads and writes, which capabilities it uses, where each outcome goes, what artifact is active, and what happened in a selected run.

| Gate clause | Proof |
|---|---|
| **what event enters this rule** | `EventGroupDTO` + `RuleClusterDTO.event_type`/`trigger_filter`; `EventScopeSelector.test.tsx` (T-18); `test_event_filter_preserves_every_reachable_branch` (T-7) |
| **what each node does** | `StepExplanationDTO` rendered by the shared `IntentSections` in both card and inspector; `StepNodeCard.test.tsx` per step kind (T-14); `test_explanation_is_copied_not_rederived` (T-8) proves the displayed intent is the contract's, not the UI's |
| **which data it reads and writes** | `explanation.inputs` + `explanation.result` + `advanced.resolved_inputs`; T-19, T-23 |
| **which capabilities it uses** | `AiNodeDetailDTO.capabilities` + `capability_fingerprint` + `DelegationPolicyDTO`; T-20 |
| **where each outcome goes** | one edge per transition record with a content-derived id (T-4), the timeout-collision regression pin (T-5), distinct kinds (T-16), independently selectable overlapping edges (T-17), `OutcomeExplanationDTO.target_title` in the inspector (T-19) |
| **what artifact is active** | `ArtifactRefDTO` + `ActivationStateDTO.active_artifact_sha256`/`health`/`reasons` on every graph response; `ActivationPanel.test.tsx` (T-33) |
| **what happened in a selected run** | `PlaybookRunOverlayResponse` pinned to `run.artifact_sha256` (T-36), edge ids joining the graph (T-37), loop iterations listed not collapsed (T-38), both attempts retained (T-39) |
| **diff review precedes activation** | `test_activate_requires_acknowledge_diff_for_executable_change` (T-29) + §11.2 |
| **pending events are visible and operable** | `PendingEventsPanel` (T-34) + `test_dispatch_reenters_the_engine` (T-32) |
| **existing ergonomics preserved** | T-45 mirrors the V1 canvas assertions; T-47 pins that the V1 tab still exists |
| **clients regenerated from the checked-in snapshot** | T-11, T-48, and the `git diff --exit-code` in §13.1 |

Milestone **M5 — Operator legible** is claimed only when every command in §13.1 passes and all seven §13.3 screenshots are attached.

---

## 15. Rollback boundary

- **Reads are additive.** `src/api/models/playbook_v2.py`, `src/playbooks/graph_projection.py`, `artifact_diff.py`, `run_overlay.py`, `src/commands/playbook_v2_commands.py` and `dashboard/src/pages/playbook-graph-v2/` are new files. Reverting commits 6→1 in order removes them without touching a single V1 line — the only edits to existing modules are additive list/dict entries (`get_all_response_models`, `_TOOL_CATEGORIES`, `_ALL_TOOL_DEFINITIONS`, `PAUSED_PLAYBOOK_COMMANDS`, `CommandHandler` bases, the `TABS` array) plus the two config fields.
- **The V1 graph never stops working.** `playbook_graph_view`, `src/playbooks/graph_view.py` and `dashboard/src/pages/playbook-graph/` are untouched by this package, and T-47 pins that.
- **Writes are separately gated.** `playbooks.v2_activation_writes` defaults to `False`; turning it off leaves the entire review surface readable. Turning `playbooks.v2_api` off makes all seven commands return a single explicit error, which the dashboard renders as a hidden tab rather than a broken one.
- **The migration is optional and additive.** Four nullable columns; `alembic downgrade -1` is safe but not required to revert the code.
- **Client regeneration is reversible** by reverting `openapi.json` and both generated packages together with the commit that added the commands — `tests/test_api_client_contract.py` is the guard that they move as one.

---

## 16. Reconciliation record — §3.2 run on 2026-09-02

The §3.2 checklist was run against `origin/main` at `f0e03446` before commit 1,
as §3.2 requires. **Result: every Package 1-4 symbol this plan consumes is
absent.** Packages 1, 2, 3 and 4 have merged their *child plan documents* only;
`origin/feature/playbook-v2-pkg3` and `-pkg4` carry docs commits and nothing
else. Package 0 has landed (`src/profiles/capabilities.py`,
`src/commands/principal.py`, `src/commands/authorization.py` all exist).

| Checklist item | Expected | Found |
|---|---|---|
| `src/playbooks/definition.py` (`PlaybookDefinition`, `Rule`, `SourceRef`) | P2 | absent |
| `src/playbooks/expressions.py` typed value union | P2 | absent |
| `src/playbooks/explanation.py` (`explain_step`, `StepExplanation`, `EffectClause`) | P1 | absent |
| `src/commands/contracts/registry.py` (`get_contract`) | P1 | package directory absent |
| `src/playbooks/artifact_store.py` (`ArtifactRef`, `ArtifactStore`) | P3 | absent |
| `src/playbooks/activation.py` (`ActivationHealth`) | P3 | absent |
| `src/playbooks/receipts.py`, `run_state.py`, `waits.py` | P3 | absent |
| `src/playbooks/engine.py` (`PlaybookEngine.dispatch_event`) | P4 | absent |
| `src/database/queries/playbook_artifact_queries.py` | P3 | absent |
| `src/database/queries/playbook_run_queries.py` | P3 | absent |
| `playbook_activations` / `playbook_pending_events` tables (§3.2 check 2) | P3 | absent — §9 cannot run and is deferred with the storage that owns those tables |
| `src/profiles/capabilities.py::CapabilityPolicy` | P0 | present |
| V1 surfaces still present (§3.2 check 4) | yes | `dashboard/src/pages/playbook-graph/` and `src/playbooks/graph_view.py` untouched |

### 16.1 What commit 1 shipped under that reconciliation

The frozen §4 contract does not depend on any Package 1-4 symbol, and roadmap
§7 makes it the precondition for every parallel task in this package. It was
therefore shipped in full, together with everything that carries it to a
client:

- `src/api/models/playbook_v2.py` — §4 verbatim, all six DTO families,
  `extra="forbid"` throughout, activation block ordered above the graph block
  per §4.8.
- Registration: `src/api/models/__init__.py`, `src/tools/definitions.py`
  (`_TOOL_CATEGORIES` **and** `_ALL_TOOL_DEFINITIONS`), `PAUSED_PLAYBOOK_COMMANDS`
  and the `CommandHandler` bases in `src/commands/handler.py`.
- `src/config.py` — both §8 flags, defaulting to `False`.
- `src/commands/playbook_v2_commands.py` — the seven commands of §5.4 with
  their argument validation, both feature gates, and the exact error strings.
- Regenerated `openapi.json` (7 new paths, 58 new schemas, **zero** removals)
  and `packages/aq-client/`; the TS client regenerates cleanly and produces the
  seven §4.9 SDK function names (its output tree is gitignored).
- `tests/test_playbook_v2_api_dtos.py`, `tests/test_api_playbook_v2_commands.py`.

### 16.2 What is deferred, and to whom

`src/playbooks/graph_projection.py`, `artifact_diff.py` and `run_overlay.py`
(§5.1-§5.3) each take a `PlaybookDefinition` and an `explain_step` result as
**input**. Writing them now would mean inventing Packages 2's artifact model and
Package 1's explanation payload inside a projector — precisely what §3.3 forbids
("If a projection needs a fact the artifact does not carry, the fix belongs in
Package 2 or 3 — not in a projector that infers it"), and a guaranteed conflict
with those packages.

Every command therefore validates its arguments, honours both flags, and then
returns one honest error at a single named seam,
`PlaybookV2CommandsMixin._v2_storage_unavailable`:

```
playbook v2 artifact storage is unavailable: the typed artifact model,
artifact store and run receipts (playbook V2 roadmap packages 2-4) are not
present in this build
```

The task that lands the artifact store replaces that one method and fills in the
three projectors **behind the already-frozen wire contract**. Tasks T-4 to T-9,
T-27, T-28, T-32 and T-36 to T-41, the §10 fixtures, and the §9 migration move
with it. Commits 2-6 of §12 (the dashboard slice) are unaffected: they consume
§4, which is checked in.

### 16.3 Re-run on 2026-09-02 at `62667475` (task solid-harbor.46.1, second attempt)

§3.2 was re-run after PR #180 (Package 3, `aq/solid-harbor.35`) and PR #181
(commit 1 of this package, `feature/playbook-v2-pkg5-api`) merged to `main`.
Result: **Package 3 is present; Packages 1, 2 and 4 are still absent on `main`
and on every `origin/*` branch** (`git cat-file -e` against each remote ref for
`definition.py`, `explanation.py`, `engine.py`, `receipts.py`,
`contracts/registry.py`, `playbook_run_queries.py` — no hits).

| Checklist item | Expected | Found at `62667475` |
|---|---|---|
| `src/playbooks/definition.py` (`PlaybookDefinition`, `Rule`, `SourceRef`) | P2 | absent |
| `src/playbooks/explanation.py` (`explain_step`, `StepExplanation`, `EffectClause`) | P1 | absent |
| `src/commands/contracts/registry.py` (`get_contract`) | P1 | absent |
| `src/playbooks/artifact_store.py` (`ArtifactRef`, `ArtifactStore`) | P3 | **present** — `load(sha)` returns `PlaybookDefinition` when Package 2 is importable, else the parsed JSON |
| `src/playbooks/activation.py` (`ActivationHealth`) | P3 | **present** — exactly the six §4.4 values (check 3 passes) |
| `src/database/queries/playbook_artifact_queries.py` | P3 | **present** — `upsert_playbook_artifact`, `get_playbook_artifact`, `set_playbook_activation`; no list-artifacts or get/list-activation reads yet |
| `src/database/queries/playbook_run_queries.py` | P3/P4 | absent |
| `src/playbooks/receipts.py`, `src/playbooks/engine.py` | P4 | absent |
| `playbook_activations` table (§3.2 check 2) | P3 | **present, with `activated_by`** — the activation half of §9 is unnecessary and will not ship |
| `playbook_pending_events` table (§3.2 check 2) | P3/P4 | absent — the pending half of §9 stays with the package that creates the table; it is not an additive migration here |
| V1 surfaces (§3.2 check 4) | present | `dashboard/src/pages/playbook-graph/` and `src/playbooks/graph_view.py` untouched |

Consequence: the §16.2 deferral still stands. `project_graph` takes a
`PlaybookDefinition` and an `explain_step` result, `diff_artifacts` takes two
`PlaybookDefinition`s and the execution-fingerprint input set from
`get_contract`, and `project_overlay` takes a `PlaybookRunV2` and `StepReceipt`
rows — none of which exist. Every test named in §16.2 (T-4 to T-9, T-27, T-28,
T-32, T-36 to T-41, T-43, T-44) consumes one of those inputs. The
`_v2_storage_unavailable` seam remains in place; its module docstring was
updated in this commit to say which packages have landed. Re-run §3.2 once
Packages 1, 2 and 4 are on `main`.

### 16.4 Re-run on 2026-09-02 at `d8086cb9` (task solid-harbor.46.1, third attempt)

§3.2 was re-run a third time after PR #182 merged (an import-cycle fix; no
playbook files). Every row of the §16.3 table is unchanged: checks 1-4 report
the same modules present and absent, `playbook_pending_events` is still
`ABSENT`, and a `git cat-file -e` sweep of all 280 `origin/*` refs finds no
branch carrying `definition.py`, `explanation.py`, `contracts/registry.py`,
`engine.py`, `receipts.py` or `playbook_run_queries.py`. No open PR touches
them either. Nothing was built; the task was closed as blocked rather than
re-queued a fourth time, because the queue has no dependency edge from this
task to the Package 1, 2 and 4 tasks and each attempt re-derives the same
result. The next attempt should be scheduled only after those three packages
merge, and should start at §3.2.

### 16.5 Re-run on 2026-09-02 at `d8086cb9` (task solid-harbor.46.1, fourth attempt)

`origin/main` had not moved since §16.4, and the four §3.2 checks and the
`origin/*` sweep gave the same result. Two facts recorded here so the next
attempt does not re-derive them:

- `ArtifactStore.load` (`src/playbooks/artifact_store.py`) returns the raw
  JSON `dict` whenever `src.playbooks.definition` is absent, and
  `playbook_artifact_queries.py` exposes only `upsert`/`get` artifact and
  `set_playbook_activation` — there is no activation read. So even the
  Package 3-only slice of item 1 (`playbook_activation_health`) has no input
  model and no query to sit on without inventing Package 2/3 shapes (§3.3).
- Open PR #191 (`aq/solid-harbor.35-rework`) reworks Package 3's
  `artifact_store.py` and `playbook_artifact_queries.py`. It adds none of the
  Package 1/2/4 files and no activation read query; the seam should not be
  wired to Package 3 surfaces until it merges.

This session's scope cannot list the queue or read any other task, so the
missing `blocks` edges to the Package 1, 2 and 4 tasks could not be added
from here; a human was asked (via `aq message send`) to add them and reopen.

### 16.6 Re-run on 2026-09-02 at `d8086cb9` (task solid-harbor.46.1, fifth attempt)

`origin/main` still at `d8086cb9`; the four §3.2 checks and the `origin/*`
sweep (285 refs) are unchanged from §16.5. PRs #191 and #194 (both Package 3
rework) are still open; no open PR adds a Package 1, 2 or 4 file. Two new
facts, recorded so the loop can be stopped rather than re-run:

- **The blockers by task id.** The Package 1, 2 and 4 inputs this package
  needs are owned by `solid-harbor.26` (P1 — contracts registry and
  explanation service; PAUSED), `solid-harbor.30` (P2 — strict definition
  model; DEFINED) and `solid-harbor.39` (P4 — engine, receipts, run
  queries; DEFINED). The `playbook_pending_events` table of §9 belongs to
  `solid-harbor.36` (PAUSED). `solid-harbor.46.1` needs `blocks` edges to
  the first three; this session's scope cannot add them.
- **Why `--failure-class hard` does not stop the re-runs.** The `events`
  table shows a `task.ready` / `promoted` row 30–60 s after every hard
  close (09:41:04, 09:48:04, 09:52:39, 09:56:46 UTC). A hard close sets
  BLOCKED (`src/orchestrator/execution.py`, `session_close_hard_failure`),
  and the projected promotion rule (`src/orchestrator/monitoring.py`,
  `_projected_promotion_decisions`) re-promotes any BLOCKED task with
  `is_blocked = 0` that carries at least one blocking edge. This task's
  only edge is its `parent-child` edge to `solid-harbor.46`, which counts,
  so the failure-BLOCKED carve-out of design §4.4 never applies to a child
  of a container. Filed as its own task (see the task comment); not fixed
  here because it is orchestrator scope, not Package 5.

### 16.7 Re-run on 2026-09-02 at `5a3c31b0` (task solid-harbor.46.1, eighth attempt)

`origin/main` has moved to `5a3c31b0` — PR #191 (Package 3 rework) merged.
The §3.2 checks are otherwise unchanged from §16.5/§16.6:

- **Check 1.** Present on `main`: `src/playbooks/artifact_store.py`,
  `src/playbooks/artifact_ref.py`, `src/playbooks/activation.py`,
  `src/database/queries/playbook_artifact_queries.py`. Missing:
  `src/playbooks/definition.py` (P2), `src/playbooks/explanation.py` and
  `src/commands/contracts/registry.py` (P1), `src/playbooks/engine.py`,
  `src/playbooks/receipts.py` and
  `src/database/queries/playbook_run_queries.py` (P4).
- **Check 2.** `playbook_activations` present with `activated_by`;
  `playbook_pending_events` still absent from `src/database/tables.py`.
- **`origin/*` sweep.** 289 refs; no ref carries any Package 1, 2 or 4 file.

One thing did change, and it closes the §16.5 caveat: with #191 merged, the
Package 3 surfaces the seam would bind to are now stable on `main`. They are
still not sufficient on their own. `ArtifactStore.load` is typed
`PlaybookDefinitionT`, which degrades to a raw `json.loads` dict while
`src/playbooks/definition.py` is absent; `PlaybookArtifactQueryMixin` exposes
only `upsert_playbook_artifact`, `get_playbook_artifact` and
`set_playbook_activation` — there is still no activation *read*, and no run or
receipt query at all. So item 1 of the task (artifact load **plus** activation
read **plus** V2 run and receipts read) cannot be completed from P3 alone, and
items 2–4 consume the P2 definition model and P1's `explain_step`, which §3.3
forbids inventing here.

Status is therefore unchanged: this package stays blocked on
`solid-harbor.26` (P1), `solid-harbor.30` (P2) and `solid-harbor.39` (P4),
and §9's `playbook_pending_events` half stays with `solid-harbor.36`.

### 16.8 Re-run on 2026-09-02 at `4a49e615` (task solid-harbor.46.1, ninth attempt)

`origin/main` has moved to `4a49e615` (four merges since `5a3c31b0`: #189,
#186, #195, #193 — CLI/MCP schemas, OpenAPI guard rendering, pipeline
review derivation, `aq test` path validation). None of them touches
Package 1, 2 or 4.

- **Check 1.** Unchanged from §16.7. Present on `main`:
  `src/playbooks/artifact_store.py`, `src/playbooks/activation.py`,
  `src/database/queries/playbook_artifact_queries.py`,
  `src/api/models/playbook_v2.py`, `src/commands/playbook_v2_commands.py`.
  Missing: `src/playbooks/definition.py` (P2),
  `src/playbooks/explanation.py` and `src/commands/contracts/registry.py`
  (P1), `src/playbooks/engine.py`, `src/playbooks/receipts.py` and
  `src/database/queries/playbook_run_queries.py` (P4). Also still absent,
  as expected: `graph_projection.py`, `artifact_diff.py`, `run_overlay.py`.
- **Check 2.** `playbook_activations` present with `activated_by`;
  `playbook_pending_events` still absent from `src/database/tables.py`
  (the tables between `playbook_activations` and `workflows` are
  `task_assignment_routes` and nothing else).
- **`origin/*` sweep.** 290 refs; zero hits for any P1/P2/P4 file.
- **Open PRs.** Six (#198, #197, #196, #194, #192, #184); none adds a
  Package 1, 2 or 4 file.

New this run, and the only material change: **PR #198
("Keep terminal-BLOCKED tasks out of the BLOCKED-recovery rule,
crisp-pinnacle-54") is open**, which is the fix for the re-promotion loop
described in §16.6. Once it merges, a `fail --failure-class hard` close of
this task will stay BLOCKED instead of being re-promoted within a minute,
so the re-run churn should stop on its own. The remaining human action is
narrower than in §16.6/§16.7: add `blocks` edges
`solid-harbor.46.1 -> solid-harbor.26 / .30 / .39` (or pause this task) so
the dependency is recorded rather than merely inert.

Status otherwise unchanged: blocked on P1 (`solid-harbor.26`),
P2 (`solid-harbor.30`) and P4 (`solid-harbor.39`); §9's
`playbook_pending_events` half stays with `solid-harbor.36`.

---

## 17. Open items for the next child plans

- **Package 3 must emit the six-value `ActivationHealth`** of §4.4 (`ready`, `question_required`, `invalid`, `disabled`, `stale_contract`, `unavailable`), and must decide whether the design spec's `needs_rebuild` name survives as an alias. This plan assumes `stale_contract` is the wire value. Raise it in Package 3's child plan.
- **Package 3 should ship the operator-audit columns** (`playbook_activations.activated_by`, `playbook_pending_events.resolved_at`/`resolved_by`/`resolution`) with its own tables, which makes §9 unnecessary. If it does, delete §9 in the reconciliation commit.
- **Package 1 must expose the effect-clause kinds of §4.2** and fail contract registration when an effect cannot be rendered (its own required outcome). `EffectKind` here is the closed set the UI switches on; a new clause kind is a coordinated change to both plans.
- **Package 4 must record `selected_transition` on every receipt** in a form that reconstructs `f"{rule_id}::{step_id}::{outcome}"`. Without it the overlay cannot highlight edges, and T-37 fails.
- **Package 6** consumes this surface for reviewed activation: its inventory can start read-only before Package 5 lands (roadmap §7), but no artifact may be activated until the diff and health UI of commit 4 exists.
- **Package 7** deletes `playbook_graph_view`, `src/playbooks/graph_view.py`, `src/api/models/playbook.py`'s graph DTOs, `dashboard/src/pages/playbook-graph/`, the `graph` tab, and both flags from §8; then renames the `semantic` tab to `graph`. Nothing else in this package is temporary.
- **Deferred deliberately:** live streaming of run overlays (the 5 s poll of §6.4 is sufficient for M5 and avoids coupling to `src/api/streams.py` before the engine's lifecycle events settle in Package 4), and an artifact-history browser beyond the activation panel's list. Both are follow-ups, not exit-gate requirements.
