# Module catalog — providers

LLM providers, usage accounting, tokens and budgets: the 22 production modules
that choose a model, make a direct API call, count what it cost, and record
what the provider says is left.

Prose for everything here lives on three pages:

* [Providers, models and token accounting](../../concepts/providers.md) — the
  concepts, the two paths, and what the numbers mean.
* [Setting up LLM providers](../../guides/llm-providers.md) — credentials,
  classes, the quota probe, prices.
* [Usage accounting reference](../usage-accounting.md) — every field, table,
  key and outcome.

> **Scope note.** This shard covers the *direct* LLM path and the accounting
> around it. Coding-agent sessions — the CLIs that do repository work — belong
> to the [sessions shard](sessions.md), and the transcript watcher that writes
> the token ledger is one of its modules.

## The direct LLM path

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/llm/__init__.py`](../../../src/llm/__init__.py) | Re-exports the direct path's public surface — client, spec, response and turn types — so callers never import from submodules. | [concepts/providers.md](../../concepts/providers.md) | The package docstring names the design spec the path implements. `tests/llm/test_client.py` |
| [`src/llm/client.py`](../../../src/llm/client.py) | `LLMClient`: resolves a spec, caches one adapter per resolution, and offers exactly two calls — `complete()` and the `run_tools()` loop. | [concepts/providers.md](../../concepts/providers.md) | Owned by the orchestrator; a consumer receives one and never builds it. Every call is logged in a `finally`, errors included. `tests/llm/test_client.py`, `tests/llm/test_run_tools.py` |
| [`src/llm/spec.py`](../../../src/llm/spec.py) | `LLMCallSpec`, `ResolvedCall` and `resolve_call` — the pure resolution of what a caller asked for against config and intelligence classes. | [reference/usage-accounting.md](../usage-accounting.md) | Order is `spec.model` → class → `llm.model` → adapter default; an unknown class warns and falls through rather than failing. `tests/llm/test_spec.py` |
| [`src/llm/types.py`](../../../src/llm/types.py) | The normalized response vocabulary: `TokenUsage`, `TextBlock`, `ToolUseBlock`, `ChatResponse`, and `serialize_canonical`. | [reference/usage-accounting.md](../usage-accounting.md) | `TokenUsage.reported` is separate from the counts on purpose: zero is an observation, absence is not. `tests/test_llm_usage.py` |
| [`src/llm/fake.py`](../../../src/llm/fake.py) | `FakeProvider`: a FIFO queue of scripted responses that records every call it received. | [concepts/providers.md](../../concepts/providers.md) | Its `reports_usage` mirrors whatever was scripted, which is how hard-budget behaviour is tested both ways. `tests/llm/test_providers.py` |
| [`src/llm/tool_conversion.py`](../../../src/llm/tool_conversion.py) | Backwards-compatible re-export of `anthropic_tools_to_openai` after the conversion logic moved into the adapters package. | [reference/usage-accounting.md](../usage-accounting.md) | Ten lines, no logic — a shim kept so existing imports resolve. `tests/llm/test_openai_adapter.py` |

## Provider adapters

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/llm/providers/__init__.py`](../../../src/llm/providers/__init__.py) | `create_provider`, the single constructor, and the mapping of class extras (`thinking`, `thinking_budget`, `reasoning_effort`) onto each adapter. | [reference/usage-accounting.md](../usage-accounting.md) | Holds the anthropic thinking-level → token-budget table; unknown extras are ignored, and an unknown provider id raises. `tests/llm/test_providers.py` |
| [`src/llm/providers/base.py`](../../../src/llm/providers/base.py) | The `LLMProvider` ABC: `create_message`, `model_name`, `is_configured`, `reports_usage`, `is_model_loaded`. | [reference/usage-accounting.md](../usage-accounting.md) | `reports_usage` defaults to `False`, so a new adapter is ineligible for a hard total-token budget until it opts in. `tests/llm/test_providers.py` |
| [`src/llm/providers/anthropic.py`](../../../src/llm/providers/anthropic.py) | The Anthropic adapter, with four credential paths tried in a fixed order: Vertex, Bedrock, API key, Claude Code OAuth. | [guides/llm-providers.md](../../guides/llm-providers.md) | The fixed order is why a stray `AWS_REGION` silently routes to Bedrock. A thinking budget also raises `max_tokens` to `budget + 1024`. `tests/llm/test_provider_requests.py` |
| [`src/llm/providers/google.py`](../../../src/llm/providers/google.py) | The Gemini adapter over `google-genai`, in either API-key or Vertex mode. | [guides/llm-providers.md](../../guides/llm-providers.md) | Adds the thinking budget *on top of* the caller's `max_output_tokens`, because Gemini counts thinking against that ceiling. `tests/llm/test_provider_requests.py` |
| [`src/llm/providers/openai.py`](../../../src/llm/providers/openai.py) | The OpenAI adapter, also used for any OpenAI-compatible local endpoint; adds keep-alive hints and an `/api/ps` residency probe in local mode. | [guides/llm-providers.md](../../guides/llm-providers.md) | `is_model_loaded` fails open — a probe that cannot answer never blocks a caller. `tests/llm/test_provider_requests.py` |
| [`src/llm/providers/adapters/__init__.py`](../../../src/llm/providers/adapters/__init__.py) | Package marker for the wire-format adapters; deliberately empty so both adapters can be imported without pulling in a vendor SDK. | [reference/usage-accounting.md](../usage-accounting.md) | No code. The lazy-import discipline lives in the adapter modules themselves. |
| [`src/llm/providers/adapters/openai_adapter.py`](../../../src/llm/providers/adapters/openai_adapter.py) | Translates Anthropic-shaped tools and messages into OpenAI function-calling format, and parses completions back into `ChatResponse`. | [reference/usage-accounting.md](../usage-accounting.md) | A tool call with non-JSON arguments becomes a text block, never an exception — one malformed payload must not abort a tool loop. `tests/llm/test_openai_adapter.py` |
| [`src/llm/providers/adapters/gemini_adapter.py`](../../../src/llm/providers/adapters/gemini_adapter.py) | The same translation for Gemini `Content`/`Part`/`FunctionDeclaration` objects, including JSON Schema → `types.Schema`. | [reference/usage-accounting.md](../usage-accounting.md) | Resolves tool-result ids back to function names (Gemini needs the name), and drops `null` from unions and enums, which Gemini cannot express. `tests/llm/test_gemini_adapter.py` |

## Provider usage observation

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/providers/__init__.py`](../../../src/providers/__init__.py) | Exports the snapshot type and the `/usage` parser, and documents why the two providers are observed by completely different mechanisms. | [concepts/providers.md](../../concepts/providers.md) | Codex publishes quota passively on transcript lines; Claude has no local equivalent and must be probed. `tests/test_provider_usage_probe.py` |
| [`src/providers/snapshot.py`](../../../src/providers/snapshot.py) | `ProviderUsageSnapshot`: one reading of one `(provider, window, scope)` limit window. | [reference/usage-accounting.md](../usage-accounting.md) | A plain value object with no database or I/O coupling, which is what lets the parser be a pure function. `tests/test_provider_usage_queries.py` |
| [`src/providers/claude_usage.py`](../../../src/providers/claude_usage.py) | Pure parser for `claude -p "/usage"` output: one regex per limit line, plus year-less reset-clause resolution. | [concepts/providers.md](../../concepts/providers.md) | Text with no limit line yields zero snapshots and `unparsed=True` — except the API-key preamble, which is an answer, not a fault. `tests/test_claude_usage_parser.py` |
| [`src/providers/probe.py`](../../../src/providers/probe.py) | Runs the probe subprocess under a 20-second ceiling and turns every failure mode into a typed `ProbeResult` instead of an exception. | [guides/llm-providers.md](../../guides/llm-providers.md) | Never passes `--bare` (it would read a different account); a timed-out child is killed *and reaped*, or a ten-minute timer accumulates zombies. `tests/test_provider_usage_probe.py` |

## Token accounting and budgets

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/llm_logger.py`](../../../src/llm_logger.py) | `LLMLogger` and `PromptAnalytics`: date-partitioned JSONL for every direct call and agent session, plus hourly aggregates and retention cleanup. | [reference/usage-accounting.md](../usage-accounting.md) | Its token figures are characters ÷ 4 **estimates**, named `*_est`, and never reach the ledger or a price. `tests/test_llm_logger.py` |
| [`src/tokens/__init__.py`](../../../src/tokens/__init__.py) | Package marker for the token-accounting helpers; empty, so importing a submodule pulls in nothing else. | [reference/usage-accounting.md](../usage-accounting.md) | No code. |
| [`src/tokens/budget.py`](../../../src/tokens/budget.py) | `BudgetManager`: target token ratios from per-project credit weights, deficit scores, and global/per-project exhaustion predicates. | [reference/usage-accounting.md](../usage-accounting.md) | Constructed by the orchestrator, but no production caller reads its results — the scheduler takes the global budget and ledger usage directly. Unenforced until a caller appears. `tests/test_budget.py` |
| [`src/tokens/tracker.py`](../../../src/tokens/tracker.py) | `RateLimitWindow`: a sliding per-minute/hour/day token window with `is_exceeded()` and `seconds_until_reset()`. | [reference/usage-accounting.md](../usage-accounting.md) | No production importer on `main`. Session-side backoff is done by the exit classifier instead. `tests/test_budget.py` |

## Where the rest of the story lives

These modules are documented by other shards, but this subsystem is hard to
follow without knowing they exist:

| Module | Shard | What it contributes |
|---|---|---|
| `src/sessions/transcripts/watcher.py` | [sessions](sessions.md) | The only routine writer of `token_ledger`, and the passive source of Codex quota snapshots. |
| `src/database/queries/token_queries.py`, `provider_usage_queries.py` | `database.md` — **planned** | The ledger and snapshot readers and writers, including the duplicate-reading rule. |
| `src/api/providers.py`, `src/api/metrics.py` | `api.md` — **planned** | Server-side staleness verdicts and the per-model token rates. |
| `src/commands/provider_commands.py`, `ops_commands.py` | `cli.md` — **planned** | `provider_usage_probe` and `get_costs`. |
| `src/doctor/provider_checks.py` | `operations.md` — **planned** | `providers.claude_usage`, the report-only health check. |
| `src/playbooks/executors/llm.py` | `playbooks.md` — **planned** | The only place a token budget is enforced. |
| `src/intelligence_classes/` | `vault.md` — **planned** | The class files that decide which model a tier means. |

## Tests

```bash
aq test tests/llm tests/test_llm_usage.py tests/test_llm_logger.py \
    tests/test_provider_usage_probe.py tests/test_claude_usage_parser.py \
    tests/test_provider_usage_queries.py tests/test_api_provider_usage.py \
    tests/test_provider_doctor.py tests/test_budget.py
```
