---
tags: [design, llm, supervisor, playbooks, plugins, config, cleanup]
status: approved design (2026-08-30) — awaiting implementation plan
date: 2026-08-30
related:
  - ../../specs/design/supervisor-agent.md
  - ../../specs/design/session-runtime.md
  - ../../specs/design/playbooks.md
  - ../../specs/design/feature-pauses.md
  - ../plans/2026-08-21-legacy-chat-removal.md
---

# The LLM direct path — replacing `Supervisor.chat()` and `chat_provider`

## 1. Purpose

Finish decision S9 of [[supervisor-agent]]. Since the 2026-08-21 cutover every chat
surface (Discord, dashboard, `aq chat`) talks to the *session-based* supervisor — a
profile running in tmux. The in-process `Supervisor` class, its `chat()` tool loop,
`src/chat_providers/`, and the `chat_provider:` config block were kept "dormant as
reference" because five side-consumers still called them: playbook nodes and
natural-language transitions, the plugin `invoke_llm` API, reference-stub enrichment,
`aq vault rebuild-index --with-summaries`, and the optional LLM plan parser.

Those consumers are legitimate: the framework still needs a way to make a **direct,
message-in / message-out LLM call** without spawning a tmux session. What it does not
need is the 2,200-line chat brain wrapped around that call. This spec replaces the
brain with a small, generic client and deletes everything that only existed to serve
the old chat path.

Out of scope: the session runtime, harnesses, intelligence-class *files*, the
session-based supervisor, the messages subsystem, and the aq-memory plugin's own
code (it is updated separately; see §7).

## 2. Decisions taken

| # | Decision | Why |
|---|---|---|
| L1 | **A direct-LLM path stays.** `src/llm/` provides `LLMClient.complete()` (single shot) and `LLMClient.run_tools()` (generic tool loop). | Playbook nodes, transitions, plugins, stub enrichment, and vault summaries are cheap utility calls; spawning a session for each is the wrong cost shape. User decision 2026-08-30. |
| L2 | **The client is generic, not a supervisor.** No chat history, no active project, no reply-to-user tool, no reflection. Tools and the executor are supplied by the caller. | The old `chat()` bundled supervisor semantics every caller had to work around (`_DummySupervisor`, `_last_messages` scraping, throwaway `Supervisor` construction in five places). |
| L3 | **Existing provider adapters are kept**, moved to `src/llm/providers/`. | ~600 tested lines covering Anthropic-native tools/thinking, Gemini, and OpenAI-compatible endpoints (Ollama). Rewriting them buys nothing. |
| L4 | **Config block is `llm:`**, provider ids `anthropic \| google \| openai`. Legacy `chat_provider:` loads with a deprecation warning (`gemini→google`, `ollama→openai`). | The name says what it is; the ids match the intelligence-class mapping keys so one vocabulary serves sessions and direct calls. |
| L5 | **Intelligence levels apply to direct calls.** `LLMCallSpec.intelligence_class` resolves through the same `vault/intelligence-classes/*.md` as sessions; `llm.default_class` is the baseline; explicit `model` wins. | User decision: cheap tiers for transitions and enrichment, strong tiers for consolidation, with one set of definitions. |
| L6 | **`src/runtimes/` is deleted; `harness` is the only selector.** `## Config.runtime` is rejected by the profile parser with a pointer to `harness`, exactly as `agent_name` is today. | The Supervisor was the only registered Runtime and the shipped supervisor profile already carries `harness: claude`, which wins whenever sessions are enabled. The supervisor is "just another agent config running in tmux". |
| L7 | **Only live consumers are ported.** `reflect`, `observe`, `summarize`, `expand_rule_prompt`, `break_plan_into_tasks` + plan.md discovery, `chat_observer.py`, `chat_agent.py`, `vault_glossary.extract_concepts_llm` are deleted. | None has an in-tree caller, or (plan discovery) was unwired by supervisor-agent §9 and replaced by the `planner` profile. Moving dead code is waste. |
| L8 | **One client instance per daemon**, owned by the orchestrator and injected into consumers. | Replaces five ad-hoc `create_chat_provider(...)` sites and three per-call provider-swap mechanisms with one construction and one cache. |

## 3. The `src/llm/` package

```
src/llm/
  __init__.py        LLMClient, LLMCallSpec, LLMResponse, LLMRunResult, ToolCall
  client.py          LLMClient (complete / run_tools / provider cache / logging)
  spec.py            LLMCallSpec + resolution against LLMConfig + intelligence classes
  types.py           (moved from chat_providers/types.py) ChatResponse blocks etc.
  tool_conversion.py (moved) provider-neutral tool schema helpers
  providers/
    base.py          (moved) LLMProvider ABC — create_message(), model_name
    anthropic.py     (moved)
    google.py        (moved from gemini.py; class renamed GoogleProvider)
    openai.py        (moved from ollama.py; serves OpenAI and any base_url endpoint)
    adapters/        (moved) gemini_adapter.py, openai_adapter.py
  fake.py            FakeProvider — scripted responses, for tests and `--dry-run` tooling
```

### 3.1 `LLMCallSpec`

```python
@dataclass(frozen=True)
class LLMCallSpec:
    provider: str | None = None            # "anthropic" | "google" | "openai"
    model: str | None = None               # explicit model id; wins over class
    intelligence_class: str | None = None  # e.g. "fast-low"; resolved per provider
    max_tokens: int | None = None
    caller: str = "llm"                    # logged; e.g. "playbook:memory-consolidation"
```

Resolution order (`spec.resolve(config, classes) -> ResolvedCall`):

1. `provider` = spec.provider or `config.llm.provider`.
2. `model` = spec.model, else the model from `resolve_class(classes[spec.intelligence_class or config.llm.default_class], provider)`, else `config.llm.model`, else the provider's built-in default.
3. Provider-specific tuning fields from the class slice (`thinking`, `reasoning_effort`, `thinking_budget`) ride along to the adapter; they are not top-level config.
4. `max_tokens` = spec.max_tokens or `config.llm.max_tokens`.

An unknown class id or a class with no slice for the provider logs a warning and falls through to the next step — never a hard failure, matching `sessions/spec.py`.

**The profile's `harness` does not select a provider here** (settled 2026-09-03, task `swift-ember-68`). A playbook `LlmStep` names a profile, and `src/playbooks/executors/llm.py` takes only the profile's `default_class` from it, deliberately leaving `spec.provider` unset so step 1 resolves to `llm.provider`. Two reasons:

- `harness` names the **CLI that runs a session**; a direct-path call is headless and runs no CLI, so a codex- or gemini-harnessed profile has no CLI to honour here.
- `llm:` carries a **single** `api_key` / `base_url` pair bound to `llm.provider`, and `LLMConfig.validate()` only checks credentials for that one provider. Deriving the provider per profile would hand one provider's credentials to another's adapter, failing at call time with nothing at config time able to catch it.

Read-only surfaces follow this path rather than restating it: the semantic graph's AI card for an `llm` node resolves through `src.profiles.intelligence.direct_call_intelligence_for`, which builds the executor's own `LLMCallSpec` and calls `resolve_call`, so the card cannot name a provider or model the step would not use. An `agent_task` node does launch a session, so its card keeps the harness-derived `intelligence_for`. `ProfileLookup` exposes the two as separate methods (`direct_routing` / `routing`) precisely so a caller has to say which surface it is on.

### 3.2 `LLMClient`

```python
class LLMClient:
    def __init__(self, config: LLMConfig, *, classes_loader, llm_logger=None): ...

    async def complete(
        self, messages: list[dict] | str, *, system: str = "", spec: LLMCallSpec = LLMCallSpec()
    ) -> LLMResponse: ...

    async def run_tools(
        self,
        messages: list[dict] | str,
        tools: list[dict],                       # JSON-schema tool definitions
        execute: Callable[[str, dict], Awaitable[Any]],
        *,
        system: str = "",
        spec: LLMCallSpec = LLMCallSpec(),
        max_turns: int = 25,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> LLMRunResult: ...

    def is_configured(self) -> bool: ...
    async def is_model_loaded(self, spec=LLMCallSpec()) -> bool: ...
```

- `LLMResponse`: `text`, `tool_calls: list[ToolCall]`, `usage`, `raw` (provider blocks).
- `LLMRunResult`: `text` (final assistant text), `transcript: list[dict]` (full message
  list including tool results — what playbook output extraction reads), `turns`,
  `stopped_by: "done" | "max_turns" | "cancelled"`, `usage`.
- The tool loop: send → if no tool calls, return → else execute each call via
  `execute(name, args)` (exceptions become `{"success": false, "error": ...}` tool
  results, never abort the loop) → append results → repeat. `on_progress(kind, text)`
  fires for `"text"` and `"tool"` events, the contract the playbook runner already uses.
- Provider cache: one adapter per `(provider, model, base_url)`; adapters are created
  lazily so a misconfigured provider fails at first call, not at daemon start.
- Logging: every call goes through `LLMLogger.log_llm_call(caller=spec.caller, ...)`
  writing `llm.jsonl` (replaces `log_chat_provider_call` / `chat_provider.jsonl`).
  `caller_override` (a ContextVar) is deleted; the caller is an explicit field.
- Construction: `Orchestrator.__init__` builds `self.llm = LLMClient(config.llm, ...)`
  with the intelligence-class loader it already has for sessions. Consumers receive
  `orchestrator.llm`; nothing else constructs a client or an adapter.

## 4. Configuration

### 4.1 `llm:` block

```yaml
llm:
  provider: google          # anthropic | google | openai
  model: gemini-2.5-flash   # optional; class or provider default otherwise
  api_key: ""               # optional; env vars ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY
  base_url: ""              # openai only: e.g. http://localhost:11434/v1 for Ollama
  max_tokens: 4096
  default_class: fast-medium  # intelligence class when a call names none
```

`LLMConfig` replaces `ChatProviderConfig`. Dropped fields: `keep_alive`, `num_ctx`,
`thinking_budget`, `playbook_max_tokens`. `validate()` checks the provider id and
requires `base_url` when `provider: openai` and no `OPENAI_API_KEY`/`api_key` is set
(local endpoint) — the current Ollama rule, reworded.

Legacy loading: if the YAML has `chat_provider:` and no `llm:`, load it into
`LLMConfig` with the id mapping and log one deprecation warning naming the file.
If both are present, `llm:` wins and `chat_provider:` is ignored with a warning.
`llm` joins `RESTART_REQUIRED_SECTIONS`; `chat_provider` leaves every section list.
The config editor's reflective schema picks the new dataclass up automatically.

### 4.2 Other config changes

| Field | Change |
|---|---|
| `auto_task.llm_parser_model` | deleted (never read). `auto_task.use_llm_parser` stays; add `auto_task.llm_parser_class: str = ""`. |
| `memory.stub_enrichment_provider`, `stub_enrichment_model` | deleted; add `memory.stub_enrichment_class: str = ""`. `stub_enrichment_enabled`/`max_source_chars` stay. |
| `memory.compact_llm_*`, `revision_*`, `consolidation_*` | **unchanged** — read by the external aq-memory plugin (§7). |
| `supervisor.reflection`, `supervisor.observation` (`ReflectionConfig`, `ObservationConfig`) | deleted with the methods. `supervisor.global` stays (session supervisor). |
| `planner.legacy_plan_discovery` | deleted (§6.3). |
| `default_runtime` | deleted; the profile parser's `runtime` rejection replaces its validation. |
| `llm_logging` | unchanged shape; `chat_provider.jsonl` references become `llm.jsonl`. |
| `supervisor_agent.*` | unchanged (session supervisor switch). |

Unknown keys in existing YAML (`chat_provider.keep_alive`, `planner.legacy_plan_discovery`,
`default_runtime`, …) are ignored, not rejected: old config files must keep loading.

### 4.3 Playbook frontmatter

`llm_config:` and `transition_llm_config:` keep their names and accept
`{provider, model, intelligence_class, max_tokens}`; the compiler passes them through
into an `LLMCallSpec`. `thinking_budget`/`num_ctx`/`keep_alive` keys are dropped with a
compile warning. Shipped `memory-consolidation.md` keeps `provider: gemini` → the
compiler maps the legacy id to `google` (same table as the config loader).

## 5. Consumers, ported

| Consumer | Before | After |
|---|---|---|
| Playbook node execution (`playbooks/runner.py`) | `supervisor.chat(...)` when profile `runtime == "supervisor"`, else one-shot session | Always `llm.run_tools(prompt, tools, execute, system=..., spec=node_spec, on_progress=..., cancel_event=...)`. `tools` = tool registry definitions filtered by the playbook profile's `allowed` list and `tool_overrides`; `execute` = `handler.execute` with `set_caller_profile` applied for the call. The existing `_execute_node_via_platform` session path is kept for profiles that declare a `harness`. |
| NL transitions (`runner_transitions.py`) | `supervisor.chat` | `llm.complete(..., spec=transition_spec)`. |
| Output extraction (`runner_context.py`) | reads `supervisor._last_messages` | reads `LLMRunResult.transcript`. |
| Runner construction | takes `supervisor` | takes `PlaybookServices(llm, handler, tool_registry, llm_logger)`; `_DummySupervisor` deleted. `playbook_commands`, `resume_handler`, `workflow_stage_resume_handler`, `core._dispatch_playbook_trigger` pass `orchestrator.playbook_services()`. |
| Plugin API (`plugins/base.py`, `registry.py`, `core._plugin_invoke_llm`) | `invoke_llm(prompt, *, model, provider, tools, thinking_budget)` → per-call provider or `supervisor.chat` | `invoke_llm(prompt, *, intelligence_class=None, model=None, provider=None, tools=None, system="") -> str`. With `tools`, runs `run_tools` with `execute = handler.execute`; without, `complete`. `thinking_budget` removed. |
| Reference-stub enricher | own `ChatProviderConfig` + fallback chain | constructor takes `llm: LLMClient`; calls `complete(spec=LLMCallSpec(intelligence_class=config.memory.stub_enrichment_class, caller="stub-enricher"))`. |
| `vault rebuild-index --with-summaries` (`system_commands`, `vault_index.py`) | `create_chat_provider(config.chat_provider)` | `VaultIndexGenerator.generate_all_with_summaries(llm)` using `complete`. |
| LLM plan parser (`orchestrator/core.py` ~424) | `LoggedChatProvider(caller="plan_parser")` | `llm.complete(spec=LLMCallSpec(intelligence_class=config.auto_task.llm_parser_class, caller="plan_parser"))`. |

`CommandHandler` and the tool registry, which `Supervisor` used to own, are constructed
directly in `main.py` and handed to the orchestrator (`set_command_handler`,
`set_tool_registry`); `core.set_supervisor` is deleted.

## 6. Deletions

### 6.1 Code

- `src/runtimes/` (whole package: `base.py`, `supervisor.py`, `__init__.py` registry).
- `src/chat_providers/` (moved to `src/llm/`, then removed), `src/chat_agent.py`,
  `src/chat_observer.py`, `src/reflection.py`.
- `vault_glossary.extract_concepts_llm` and `build_from_vault`'s LLM branch.
- `LLMLogger.log_chat_provider_call` (replaced by `log_llm_call`).
- `orchestrator/execution.py`: the `RuntimeRegistry.create(...)` dispatch and
  `_is_session_routed` — sessions are the only route; `agent_reconciler.py`'s
  `Supervisor.name` / `requires_workspace` checks.
- Setup wizard: `step_chat_provider` (Anthropic/Ollama picker, Ollama installer,
  `anthropic`/`openai` probes) and the chat section of `step_test_connectivity`.
  `step_write_config` writes a default `llm:` block (`provider: anthropic`,
  `default_class: fast-medium`) with a comment pointing at the env var.
- pyproject extras `anthropic`, `gemini`, `ollama` become `anthropic`, `google`, `openai`;
  `llm = [all three]`. No new dependencies.

### 6.2 Profiles

- `parser.py` rejects `## Config.runtime` with
  `"'runtime' was removed; select the CLI with 'harness' (claude|codex|gemini)"`.
- Shipped `supervisor/profile.md` (and the copy in `vault.py`) drops `runtime` and the
  "lightweight chat-provider LLM" paragraph; it is `harness: claude` with its existing
  tool allowlist.
- Startup profile migration: an existing vault profile carrying `runtime` gets the key
  removed in place (same mechanism that handled `agent_name`), logged once.

### 6.3 Plan discovery

`Supervisor.break_plan_into_tasks`, `on_task_completed` plan archival, `plan.md`
scanning in `approval.py`, the `AWAITING_PLAN_APPROVAL` promotion in `execution.py`,
the call sites in `task_commands.py`, `planner.legacy_plan_discovery`, and the
`plan-parser-system` prompt template are deleted. `TaskStatus.AWAITING_PLAN_APPROVAL`
stays in the enum (rows may exist); a doctor check reports tasks left in that state
with the instruction to reopen or close them.

### 6.4 Docs

Delete `docs/specs/chat-providers/`. Rewrite `docs/specs/config.md` §4.6 as `llm`,
`docs/specs/llm-logging.md` (file name, `caller`), `orchestrator.md` / `plan-parser.md`
plan-parser rows, `setup-wizard.md`, `plugin-system.md:530`. Update
`docs/specs/design/supervisor-agent.md` §10 table (everything becomes **Removed**;
add a pointer to this spec) and `feature-pauses.md:216`. `CLAUDE.md` and `profile.md`
replace `src/runtimes/` + `src/chat_providers/` with `src/llm/`.

## 7. aq-memory follow-up (outside this repo)

The plugin's compaction, revision, and consolidation calls should move to
`ctx.invoke_llm(prompt, intelligence_class=...)`. Until it does, it keeps reading
`memory.compact_llm_*` / `revision_*` / `consolidation_*` and constructing its own
client; those fields therefore stay in `MemoryConfig`, documented as
"plugin-owned; superseded once aq-memory calls `invoke_llm`". Nothing in
this repo imports `aq_memory`, so the plugin's current behaviour is unaffected by this
change except that it must not import `src.chat_providers` — a grep of the plugin is the
first step of that follow-up.

## 8. Testing

- `tests/llm/`: `FakeProvider` (FIFO scripted `ChatResponse`s, records calls) — the
  one useful idea from the deleted `chat_eval` suite. Unit tests for `complete`; the
  tool loop (single tool, multi-tool turn, executor exception → error result,
  `max_turns`, `cancel_event`, `on_progress` events); `LLMCallSpec` resolution
  (explicit model > class > config > default; unknown class warns and falls through;
  missing provider slice falls through); config loading (`llm:`; legacy
  `chat_provider:` mapping + warning; both present; unknown keys ignored; `openai`
  without `base_url`/key rejected); logging writes `llm.jsonl` with `caller`.
- Playbook runner tests: replace `supervisor.chat` mocks with a `FakeProvider`-backed
  `LLMClient`; assert node tools are filtered by `allowed`; transition classification
  uses `complete`; output extraction reads the transcript.
- Plugin tests: `invoke_llm` with and without `tools`.
- Stub enricher, vault index, plan parser: adjust construction; behaviour tests unchanged.
- Profile parser: `runtime` rejected; startup migration strips it.
- Deleted: `test_supervisor.py`, `test_supervisor_model_override.py`,
  `test_llm_config_provider_swap.py`, `test_supervisor_profile_config.py`,
  `test_logged_provider.py`, `test_supervisor_observe.py`, `test_supervisor_runtime.py`,
  `test_compiler_llm_path_removed.py` (its assertion moves to a one-line grep test on
  `src/llm` not being imported by the compiler), plan-discovery tests.
  `test_supervisor_cutover.py` keeps its Discord-routing half.
- Gate: `pytest tests/ -n auto` green on Postgres; `scripts/e2e-env.sh --reset &&
  scripts/e2e-smoke.sh` green; `ruff check` clean; `grep -rn "chat_providers\|runtimes\.supervisor\|chat_provider" src/` empty.

## 9. Delivery

Branch `llm-direct-path` in a worktree, landed as four reviewable commits:

1. **Add** `src/llm/` + `LLMConfig` + legacy loader + tests (nothing else changes; both
   paths coexist).
2. **Port** consumers (§5) onto `orchestrator.llm`; `Supervisor` no longer constructed
   anywhere except `main.py`.
3. **Delete** `src/runtimes/`, `src/chat_providers/`, dead methods, plan discovery,
   config fields, wizard step, extras (§6); profile parser rejection + migration.
4. **Docs** (§6.4) and CLAUDE.md/profile.md.

Each commit leaves the suite green so the work can be bisected or partially reverted.

## Deviations applied during implementation

1. **`src/runtimes/` kept as a seam, not deleted.** §6.4/§9 said delete
   `src/runtimes/` outright. Instead `base.py` (the `Runtime` ABC, `Capability` enum,
   `requires_workspace` ClassVar) and the `RuntimeRegistry` stayed as the test/dispatch
   injection seam used by `sync_workflow` and tests; only `supervisor.py` (the in-process
   Supervisor runtime implementation) was deleted. `default_registry(config=...)`
   registers nothing in production — there are no in-tree `Runtime` implementations left,
   since every agent runs as a tmux session selected by `harness`.
2. **The LLM plan parser was deleted, not ported.** §5's port list implied the
   plan-discovery LLM call would move onto the direct path. In practice it was dead
   code — `_chat_provider` on the plan parser was never read — so it was deleted
   outright along with plan discovery itself (`.claude/plan.md` parsing,
   `AWAITING_PLAN_APPROVAL`), not migrated to `LLMClient`. The `use_llm_parser` and
   `llm_parser_model` config keys were removed with it.
3. **No `usage` field on `LLMResponse`/`LLMRunResult`.** The design implied
   provider-reported token accounting would ride along with responses. The shipped
   adapters expose content blocks only — no normalized usage struct — so token
   accounting stays estimate-based (unchanged from the chat-provider era). A
   `tool_calls_made` count was added to `LLMRunResult` instead, since callers needed a
   way to tell whether the tool loop actually invoked anything.
4. **`AgentProfile.runtime` field and DB column retained, inert.** Rather than dropping
   the `runtime` dataclass field and column, both stay in place but unused:
   `src/profiles/parser.py` rejects `runtime` in profile config with a pointer to
   `harness`, and a startup migration strips any lingering `runtime` key from vault
   profiles. This avoided an extra migration/rename for a field with no remaining
   writers.
5. **`process_plan`/`process_task_completion` commands deleted outright, not just
   their call sites.** §6.3 named only "call sites" for removal. But the commands
   themselves — `TaskCommandsMixin._cmd_process_plan` and `_cmd_process_task_completion`
   — were exactly those call sites: with the LLM plan parser gone (deviation 2), every
   invocation of `process_plan` created an `AWAITING_PLAN_APPROVAL` row with no draft
   subtasks and no way to populate them, which `_cmd_approve_plan` then rejected —
   an unrecoverable dead end reachable from Discord/MCP/CLI. Both `_cmd_*` methods,
   their tool definitions (`src/tools/definitions.py`), formatter registrations
   (`src/cli/formatter_registry.py`), and response models (`src/api/models/task.py`)
   were removed in a follow-up fix wave the same day. `_cmd_approve_plan` and
   `_cmd_reject_plan` were kept as the remediation path for pre-existing rows (the
   `tasks.awaiting_plan_approval` doctor check points at them); `approve_plan`'s
   error message for a row with no draft subtasks was reworded to point at
   `reject_plan`/`delete_plan` instead of the now-deleted `process_plan`.
