# Usage accounting reference

Every field, table, config key, command and outcome behind token accounting,
provider quota snapshots and LLM budgets. Exhaustive and boring on purpose; the
prose lives in [Providers, models and token
accounting](../concepts/providers.md).

## Types

### `TokenUsage` — [`src/llm/types.py`](../../src/llm/types.py)

| Field | Type | Meaning |
|---|---|---|
| `input_tokens` | `int` | Prompt tokens the provider reported. Default `0`. |
| `output_tokens` | `int` | Completion tokens the provider reported. Default `0`. |
| `reported` | `bool` | Whether a provider actually reported these counts. Default `False`. |
| `total` | property | `input_tokens + output_tokens`. |

Addition sums the counts and **ands** `reported`, so one unreported turn makes
the whole sum unreported. Zero is a valid observation; absence is not, and that
distinction is what lets a hard budget fail closed.

### `ChatResponse`, `TextBlock`, `ToolUseBlock` — [`src/llm/types.py`](../../src/llm/types.py)

The normalized response shape every adapter converts into. `ChatResponse.content`
is a list of `TextBlock(text)` and `ToolUseBlock(id, name, input)`; `usage` is a
`TokenUsage` or `None`. Helpers: `text_parts`, `tool_uses`, `has_tool_use`.
`serialize_canonical(messages)` turns those dataclasses back into Anthropic
wire-format dicts so a transcript can be JSON-logged.

### `LLMCallSpec` / `ResolvedCall` — [`src/llm/spec.py`](../../src/llm/spec.py)

| `LLMCallSpec` field | Meaning |
|---|---|
| `provider` | `anthropic` \| `google` \| `openai`; legacy `gemini`/`ollama` are normalized. `None` = use `llm.provider`. |
| `model` | Explicit model id. Wins over `intelligence_class`. |
| `intelligence_class` | Class id, resolved per provider. `None` = `llm.default_class`. |
| `max_tokens` | Output ceiling for this call. `None` = `llm.max_tokens`. |
| `caller` | Free-text label written to the call log, e.g. `playbook:<artifact>:<step>`. |

`ResolvedCall` adds the fields a call actually needs — `base_url`, `api_key`,
`extras` (the class slice minus `model`) — and a `cache_key` of
`(provider, model, base_url, extras)`, which is what `LLMClient` caches adapter
instances by.

Resolution order in `resolve_call`: `spec.model` → intelligence class →
`llm.model` → the adapter's own fallback. An unknown class, or a class with no
slice for the provider, logs a warning and falls through.

### `ProviderUsageSnapshot` — [`src/providers/snapshot.py`](../../src/providers/snapshot.py)

| Field | Type | Meaning |
|---|---|---|
| `provider` | `str` | `claude` or `codex`. Unconstrained by design. |
| `window` | `str` | `session` / `week` (Claude); `primary` / `secondary` (Codex). Verbatim. |
| `used_percent` | `float` | 0–100, as the provider reports it. |
| `observed_at` | `float` | Epoch when this value was observed. |
| `source` | `str` | `probe` or `transcript`. |
| `scope` | `str` | The parenthesised scope (`all models`, `Opus`), verbatim; `""` when absent. |
| `account_label` | `str` | The plan or limit id the provider printed (`pro`). Never AQ's own account identity. |
| `resets_at` | `float \| None` | Absolute epoch of the window reset; `None` when the source gave no usable clock. |

## Database tables

### `token_ledger` — what AQ observed being spent

| Column | Null | Notes |
|---|---|---|
| `id` | no | UUID. |
| `project_id` | yes | FK to `projects`. |
| `agent_id` | no | **Not** a foreign key — an audit row must outlive the agent. |
| `task_id` | no | **Not** a foreign key — tasks move to `archived_tasks`. |
| `tokens_used` | no | The authoritative total. |
| `model` | yes | Required for pricing; `NULL` rows roll into `unpriced_tokens`. |
| `input_tokens` | yes | Priced input. Cache tokens are **not** folded in. |
| `output_tokens` | yes | Priced output. |
| `cache_read_tokens` | yes | Billed at its own rate; not priced by `aq costs`. |
| `cache_write_tokens` | yes | Billed at its own rate; not priced by `aq costs`. |
| `timestamp` | no | Epoch. Indexed — the metrics sampler reads a trailing window. |

Append-only and unbounded. Writers:

| Writer | When | Split recorded? |
|---|---|---|
| [`src/sessions/transcripts/watcher.py`](../../src/sessions/transcripts/watcher.py) | Every assistant transcript entry carrying `usage`, once per entry uuid | Yes — model plus all four counts |
| [`src/orchestrator/sync_workflow.py`](../../src/orchestrator/sync_workflow.py) | A sync-workflow merge agent finishing with `tokens_used > 0` | No — total only, so those tokens are unpriced |

Reads: [`src/database/queries/token_queries.py`](../../src/database/queries/token_queries.py)
— `get_cost_rollup` (by project, profile or day), `get_project_token_usage`
(the scheduler's rolling window), `get_token_breakdown` (per task/project),
`get_token_audit` (per day).

### `provider_usage_snapshots` — what the provider says is left

Columns mirror `ProviderUsageSnapshot`, plus `id` and `last_seen_at`.
`CHECK (source IN ('transcript','probe'))`.

Two rules make the series honest
([`src/database/queries/provider_usage_queries.py`](../../src/database/queries/provider_usage_queries.py)):

* A reading identical (within `1e-9`) to the newest stored row for its
  `(provider, window, scope)` series writes **no** row, but advances that row's
  `last_seen_at`.
* `observed_at` never moves once written. Staleness is computed from
  `last_seen_at`, never from `observed_at`.

### `system_config` key `providers.claude_usage.last_probe`

One row, overwritten by every probe, success or failure
(`record_probe_health` / `read_probe_health`; key built by
`probe_health_key(provider)`). JSON fields: `ok`, `outcome`, `unparsed`,
`not_applicable`, `error`, `detail`, `recorded`, `ts`. The snapshots cannot
express "the CLI's wording moved" — a probe that parses nothing writes no row —
so the verdict decides wording faults and the snapshots decide staleness.

## Providers on the direct path

[`create_provider`](../../src/llm/providers/__init__.py) is the only
constructor; nothing else builds an adapter.

| Provider id | Adapter | Credential order | `reports_usage` |
|---|---|---|---|
| `anthropic` | [`AnthropicProvider`](../../src/llm/providers/anthropic.py) | Vertex (`GOOGLE_CLOUD_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID`) → Bedrock (`AWS_REGION` / `AWS_DEFAULT_REGION`) → `api_key` / `ANTHROPIC_API_KEY` → `~/.claude/.credentials.json` OAuth | `True` |
| `google` | [`GoogleProvider`](../../src/llm/providers/google.py) | `GOOGLE_GENAI_USE_VERTEXAI=true` → Vertex; else `api_key` / `GEMINI_API_KEY` / `GOOGLE_API_KEY` | `True` |
| `openai` | [`OpenAIProvider`](../../src/llm/providers/openai.py) | `api_key` / `OPENAI_API_KEY`; a non-`api.openai.com` `base_url` means local, where a placeholder key is supplied | `True` |
| — | [`FakeProvider`](../../src/llm/fake.py) | None; scripted queue for tests and dry runs | `True` only while every queued response carries reported usage |

`LLMProvider` ([`src/llm/providers/base.py`](../../src/llm/providers/base.py))
is the ABC: `create_message()`, `model_name`, `is_configured` (default `True`),
`reports_usage` (default **`False`**, so a new adapter is ineligible for a hard
total-token budget until it opts in), and `is_model_loaded()` (default `True`).

Class extras reaching each adapter:

| Extra key | Consumed by | Effect |
|---|---|---|
| `thinking` | anthropic | Mapped by level to a budget — `off` 0, `low` 1024, `medium` 4096, `high` 16000 — or passed through when already an int. A non-zero budget also raises `max_tokens` to at least `budget + 1024`. |
| `thinking_budget` | google | Passed to `ThinkingConfig`, and **added on top of** `max_output_tokens` so thinking cannot starve the visible response. Default `8192`. |
| `reasoning_effort` | openai | Sent as `reasoning_effort` when non-empty. |

Unknown keys are ignored.

### Wire-format adapters

[`openai_adapter`](../../src/llm/providers/adapters/openai_adapter.py) and
[`gemini_adapter`](../../src/llm/providers/adapters/gemini_adapter.py) convert
between the internal Anthropic-shaped messages/tools and each vendor's format,
and parse responses back into `ChatResponse` with usage attached
(`prompt_tokens`/`completion_tokens` for OpenAI, `usage_metadata` for Gemini).

Two behaviours worth knowing: a tool call whose arguments are not valid JSON is
reported to the caller as a text block rather than raising, so one malformed
payload cannot abort a tool loop; and the Gemini schema converter drops `null`
from union types and from enums, because Gemini has no union type.

## Client behaviour — [`src/llm/client.py`](../../src/llm/client.py)

`LLMClient` is owned by the orchestrator; consumers receive one.

| Method | Returns | Notes |
|---|---|---|
| `resolve(spec)` | `ResolvedCall` | Pure; no I/O. |
| `is_configured(spec)` | `bool` | Never calls the provider; swallows missing SDK / bad id. |
| `is_model_loaded(spec)` | `bool` | Only meaningful for a local OpenAI-compatible endpoint; fails open. |
| `complete(messages, system, spec)` | `LLMResponse` | One call. `LLMResponse` carries `text`, `tool_calls`, `raw`, `usage`. |
| `run_tools(...)` | `LLMRunResult` | Caller-supplied tool loop. |

`LLMRunResult.stopped_by` is one of `done`, `max_turns`, `cancelled`,
`interrupted`. Inside the loop: a tool the call did not offer returns an error
*result* rather than raising; a tool that raises becomes
`{"success": false, "error": …}`; and each completed boundary is handed to the
optional `on_tool_turn` callback as an `LLMToolTurn` carrying
`tool_call_ids`, a `results_digest` (SHA-256 of the results) and that turn's
usage. A callback that raises becomes `LLMToolTurnBoundaryError` — a durable
persistence failure, explicitly *not* a provider failure.

Every call is logged through `LLMLogger` in a `finally` block, so failures are
logged with their error and duration too.

## Budgets

### Playbook `llm` steps — the only enforced budget

`AiBudget` ([`src/playbooks/definition.py`](../../src/playbooks/definition.py))
requires all four fields; there is no unbounded AI step.

| Field | Bounds |
|---|---|
| `max_calls` | 1–50 |
| `max_output_tokens` | ≥ 1 (also becomes the call's `max_tokens`) |
| `max_total_tokens` | ≥ 1 |
| `timeout_seconds` | 1–3600 |

Enforcement ([`src/playbooks/executors/llm.py`](../../src/playbooks/executors/llm.py)):

| Check | When | Outcome |
|---|---|---|
| Adapter reports usage at all | Before the first call | `budget_exceeded`, diagnostic `provider does not report usage` |
| Calls used ≥ `max_calls` | Before each attempt, and via `stopped_by == "max_turns"` | `budget_exceeded` |
| `usage.reported` false with a total budget set | After each call | `budget_exceeded`, diagnostic `provider did not report usage` |
| `output_tokens > max_output_tokens` or `total > max_total_tokens` | After each call | `budget_exceeded` |
| `timeout_seconds` elapsed | Around the call and each tool dispatch | `timed_out` |
| Response is not one valid JSON object matching the schema | After each call | Retry with a correction turn; `invalid_output` when retries are spent |
| Outcome field missing or not a declared transition | After validation | `invalid_output` |
| A published tool was denied to the step's principal | After the loop | `unauthorized` |
| Adapter raised | Anywhere | `provider_error`, diagnostic is the exception class name |
| Run cancelled / tool turn interrupted | Anywhere | `cancelled` / `operator_decision_required` |

`retry.max_attempts` covers schema failures only. Provider errors are not
retried inside the step.

In dry-run mode no tools are published — a model-visible tool is an executable
capability, and dispatching one would bypass the preview-only boundary.

### Session-side backoff

Coding-agent sessions have no token budget. A session that dies with
rate-limit-shaped text in its final pane capture is classified `RATE_LIMIT`
([`src/sessions/exit_classifier.py`](../../src/sessions/exit_classifier.py),
patterns include `rate.?limit`, `usage limit reached`, `quota exceeded`, `429`,
`too many requests`, `overloaded_error`). The cooldown is the classifier's
default of **900 s** — the reconciler does not pass another value. Effects
([`src/sessions/reconciler.py`](../../src/sessions/reconciler.py)):

* the session row goes to `state=sleeping`, `sleep_reason=rate_limit`;
* a task-lifecycle session's task is `PAUSED` with `resume_after = now + 900`;
* a pool worker's task returns to the frontier and the **pool key** is
  quarantined for the same window, so another worker can take the work.

### Fleet and project token budgets

Two ledger-driven ceilings *are* enforced, both from the same rolling window
the scheduler already computes (`scheduling.rolling_window_hours`, default 24):

| Budget | Where it lives | Effect when reached |
|---|---|---|
| `global_token_budget_daily` | `~/.agent-queue/config.yaml` | `Scheduler.schedule` returns no assignments at all ([`src/scheduler.py`](../../src/scheduler.py)), and `task_claim` refuses with `not_admissible`, reason `budget_exhausted` ([`src/commands/claim_commands.py`](../../src/commands/claim_commands.py)) |
| A project's `budget_limit` | The `projects` table; set with `aq project update` | That project stops being eligible for assignment, and claims against it are `not_admissible` / `budget_exhausted` |

Both compare against `token_ledger` totals, so they measure **coding-agent
session** spend only — direct-path calls are not ledgered and cannot exhaust
either budget. "Daily" is the rolling window's name, not a calendar day, and
the claim-side check reads the last scheduler snapshot rather than re-querying,
so it can lag by a cycle.

The 80% / 95% budget-warning helper in the orchestrator has no caller on
`main`; do not expect a notification before a budget bites.

### `src/tokens/` — fair-share scaffolding

| Module | Status on `main` |
|---|---|
| [`src/tokens/budget.py`](../../src/tokens/budget.py) (`BudgetManager`) | Constructed by the orchestrator from `global_token_budget_daily`, but no production caller reads its ratios, deficits or exhaustion predicates. The scheduler takes `global_budget` and `global_tokens_used` from config and the ledger directly. |
| [`src/tokens/tracker.py`](../../src/tokens/tracker.py) (`RateLimitWindow`) | No production importer. |

Both are covered by `tests/test_budget.py` and are documented here so nobody
mistakes them for the live path. Treat their behaviour as unenforced until a
caller appears.

## Configuration keys

| Key | Default | Reload | Meaning |
|---|---|---|---|
| `llm.provider` | `anthropic` | restart | Provider for the direct path. Must be one of `anthropic`, `google`, `openai`. |
| `llm.model` | `""` | restart | Explicit model; empty means the intelligence class decides. |
| `llm.api_key` | `""` | restart | Optional; environment variables are usually better. |
| `llm.base_url` | `""` | restart | `openai` only. Anything other than `api.openai.com` is treated as a local endpoint. |
| `llm.max_tokens` | `4096` | restart | Default output ceiling. |
| `llm.default_class` | `""` | restart | Intelligence class used when a call names none. |
| `providers.claude.usage_probe_enabled` | `true` | hot | `false` makes the probe a healthy no-op, reported `disabled`. |
| `providers.claude.binary` | `claude` | hot | Point at a shim when the real CLI is not first on `PATH`. |
| `providers.claude.stale_after_seconds` | `1500` | hot | One horizon shared by the API, the card and the doctor check. Must be > 0. |
| `providers.codex_stale_after_seconds` | `14400` | hot | Codex readings only advance while a Codex session runs. Must be > 0. |
| `pricing[].model` | — | hot | `fnmatch` glob, matched in declaration order, first match wins. |
| `pricing[].input_per_mtok` / `output_per_mtok` | `0.0` | hot | USD per million tokens. The table ships empty. |
| `llm_logging.enabled` | `true` | hot | Write `logs/llm/**`. |
| `llm_logging.retention_days` | `30` | hot | Date directories older than this are removed hourly. Must be > 0 when enabled. |
| `global_token_budget_daily` | `null` | hot | Fleet-wide ceiling on ledger tokens in the scheduler's rolling window. Reaching it stops assignment and makes every claim `not_admissible`. |

A legacy `chat_provider:` block is accepted in place of `llm:` with a
deprecation warning, mapping `gemini` → `google` and `ollama` → `openai` (and
defaulting `base_url` to `http://localhost:11434/v1` for the latter). When both
are present, `llm:` wins.

## Commands and endpoints

| Surface | What it does |
|---|---|
| `provider_usage_probe` ([`src/commands/provider_commands.py`](../../src/commands/provider_commands.py)) | Runs one probe and stores what it learned. `provider` defaults to `claude`; only `claude` is probeable. |
| `get_costs` / `aq costs` ([`src/commands/ops_commands.py`](../../src/commands/ops_commands.py)) | Priced rollup. Args: `project_id`, `since` (`7d` / `12h` / `YYYY-MM-DD`), `group_by` (`project` \| `profile` \| `day`). |
| `claude_usage` ([`src/commands/system_commands.py`](../../src/commands/system_commands.py)) | Local, machine-scoped Claude Code stats read from `~/.claude/` — separate from the snapshot feed. |
| `GET /api/providers/usage` ([`src/api/providers.py`](../../src/api/providers.py)) | Newest snapshot per series; `?provider=` filters, `?since=` adds history (max 2000 points per series). |
| `GET /api/metrics/series` ([`src/api/metrics.py`](../../src/api/metrics.py)) | Includes the `tokens` block: per-minute rates overall and `by_model`, plus `unattributed_per_min`. |
| `aq doctor --check providers.claude_usage` ([`src/doctor/provider_checks.py`](../../src/doctor/provider_checks.py)) | Report-only health verdict. No `--fix`. |

### Probe outcomes

| Outcome | `success` | Snapshot written | Meaning |
|---|---|---|---|
| `probed` | `True` | yes | At least one limit line was read. |
| `unparsed` | `True` | no | The CLI answered; no limit-line regex matched. The wording moved. |
| `not_applicable` | `True` | no | An API-key account has no subscription window. |
| `unavailable` | `True` | no | No `claude` binary, or it did not answer within 20 s. |
| `disabled` | `True` | no | `providers.claude.usage_probe_enabled` is false. |
| `cli_error` | `False` | no | Non-zero exit, or an envelope carrying `is_error`. |
| `malformed` | `False` | no | Output was not the JSON envelope that was asked for. |

Every path records a probe verdict; no failure path writes a snapshot, so the
last good reading always survives.

### Doctor severity ladder

| Severity | Condition |
|---|---|
| `INFO` | No database; the CLI is unavailable; the account has no window. |
| `OK` | The probe is disabled; or a `probe` snapshot inside the horizon (detail carries the current percentages). |
| `WARN` | No probe has ever run; the last body did not parse; the last probe failed; no snapshot stored; or the newest snapshot is older than the horizon. |

Wording faults are reported before staleness: when a probe is both stale and
unparsed, "the regex needs a human" is the sentence that gets it fixed.

## Log files — [`src/llm_logger.py`](../../src/llm_logger.py)

Under `<data_dir>/logs/llm/<YYYY-MM-DD>/`, append-only JSONL:

| File | One entry per | Contains |
|---|---|---|
| `llm.jsonl` | Direct `LLMClient` call | `caller`, `model`, `provider` (the adapter class name), `duration_ms`, `prompt_fingerprint`, full `input` (system, messages, tools, `input_tokens_est`), `output` (text parts, tool uses, `output_tokens_est`), `error` |
| `claude_agent.jsonl` | Agent session logged via `log_agent_session` | Task id, session id, model, prompt, output summary, optional transcript |
| `tasks/<task_id>.jsonl` | Same entries, per task | As above |
| `prompt_analytics.jsonl` | Hourly flush | Per `caller:provider:model`: call count, estimated token sums, total duration, error count, plus derived `avg_duration_ms`, `token_efficiency`, `error_rate` |

`*_est` fields are characters ÷ 4. They never enter the ledger and are never
priced. `prompt_fingerprint` is an MD5 of the first 100 characters of the system
prompt plus the sorted tool names — an A/B grouping key, not a security control.

Writes are synchronous. `cleanup_old_logs()` and `flush_analytics()` run about
once an hour from the orchestrator cycle; analytics accumulated since the last
flush are lost on restart.

## Related pages

* [Providers, models and token accounting](../concepts/providers.md) — the
  concepts and the diagrams.
* [Setting up LLM providers](../guides/llm-providers.md) — how to configure all
  of the above.
* [Module catalog — providers](modules/providers.md) — one row per module.
* [Harness reference](harnesses.md) — where transcript usage comes from.

## Tests

```bash
aq test tests/llm tests/test_llm_usage.py tests/test_llm_logger.py \
    tests/test_llm_executor.py tests/test_provider_usage_probe.py \
    tests/test_provider_usage_queries.py tests/test_api_provider_usage.py \
    tests/test_claude_usage_parser.py tests/test_provider_doctor.py \
    tests/test_budget.py
```
