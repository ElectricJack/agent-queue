---
tags: [tokens, budget, scheduling, observability, design]
status: draft
date: 2026-08-24
---

# Usage-Aware Concurrency — Track Real Headroom, Spend It Before Reset

## 1. Goal

Concurrency should be bounded by **how much quota is left before the next reset**,
not by how many workspaces happen to exist. The target is to finish each window
having used the quota, without hitting the wall early and stalling.

Slots stop being the constraint once worktree mode lands (see
[[plans/2026-08-24-worktree-workspace-migration]]) — creating a slot is lazy and
free. `max_concurrent_agents` then becomes the only ceiling, and it is a static
number that knows nothing about quota. This spec is about replacing that with a
signal derived from real usage.

## 2. What already exists

More than expected — the missing piece is narrower than "build usage tracking".

| Component | What it gives | Limitation |
|---|---|---|
| `token_ledger` table | Durable per-(project, task, agent, model) spend with `input_tokens` / `output_tokens` / `timestamp` | Records *our* spend. Says nothing about the provider's view or remaining quota. |
| `CommandHandler._probe_claude_rate_limit` (`system_commands.py`) | Reads `anthropic-ratelimit-unified-*` response headers: utilization, `reset`, derived `resets_in` | **On demand only.** Nothing samples it, nothing stores it. Costs a real API call. Returned HTTP 404 with the Claude Code OAuth token when tested 2026-08-24 — the API-key path is untested. |
| `_cmd_claude_usage` | Live session token totals from `~/.claude/projects/*.jsonl`, plus `stats-cache.json` cumulative model usage, plus `subscriptionType` / `rateLimitTier` from credentials | Measures **spend**, not headroom. Local files only. |
| `RateLimitWindow` (`src/tokens/tracker.py`) | Sliding-window accounting per (agent_type, limit_type) | Counts against a **configured** `max_tokens` — a local guess at the limit, not the provider's actual state or real reset time. |
| `global_token_budget_daily`, `max_daily_playbook_tokens`, `rate_limits` | Static caps | Hand-set numbers; no relationship to actual quota. |
| `PricingConfig` | model → $/Mtok | Empty by default; cost rollups unpriced. |

Two structural gaps:

1. **Nothing persists a time series.** Every source is either instantaneous
   (probe) or our-side-only (ledger). There is no way to answer "what did
   utilization look like over the last six hours".
2. **Nothing is provider-agnostic.** Only Anthropic has any headroom signal.
   `ACPXRuntime` fans out to Codex/Gemini and `chat_provider` uses Gemini, all
   with their own independent quotas, none observed.

## 3. Key insight: the cheapest sampler is not a probe

`_probe_claude_rate_limit` sends a synthetic 1-token request to read headers.
That is the wrong shape for continuous telemetry: it costs a call, it only
samples when asked, and it can drift from the account actually doing the work
(OAuth session vs API key).

**Every real API response already carries those headers.** A runtime that
records `anthropic-ratelimit-unified-*` off the responses it is already making
gets continuous, free, perfectly-attributed telemetry. Sampling frequency then
scales with activity, which is exactly when it matters.

Keep the synthetic probe as a **cold-start / idle** fallback: when nothing has
run for a while there are no responses to read, and one cheap call answers
"where do we stand before we start".

> Note the same idea already exists in this codebase and was recently repaired:
> `f514607c` ("record playbook + cache tokens in the ledger") captured usage off
> real responses rather than estimating. This extends that to the *limit*
> headers, not just the usage numbers.

## 4. Model

### 4.1 A usage sample

One row per (provider, window) observation:

```
usage_samples
  id            text pk
  provider      text        -- 'anthropic' | 'gemini' | 'openai' | ...
  account       text        -- tier / key identity, e.g. 'default_claude_max_20x'
  window_kind   text        -- provider's own window label, verbatim
  utilization   real        -- 0.0-1.0 as reported
  remaining     integer     -- when the provider gives an absolute, else NULL
  limit_value   integer     -- ditto
  reset_at      double      -- epoch, provider-reported
  observed_at   double      -- epoch, when we saw it
  source        text        -- 'response-header' | 'probe' | 'derived'
```

Append-only, like `token_ledger`. **No foreign keys to short-lived rows** — that
is exactly the bug that emptied the ledger (`b997f668`); an audit table must not
be cascade-deleted by routine lifecycle.

### 4.2 Provider abstraction

```python
class UsageProvider(Protocol):
    name: str
    def parse_response(self, headers: Mapping[str, str]) -> list[UsageSample]: ...
    async def probe(self) -> list[UsageSample]: ...   # cold-start fallback
```

- **Anthropic** — `anthropic-ratelimit-unified-*`; already parsed, needs lifting
  out of `system_commands.py` into a provider module.
- **Gemini** — no per-response quota headers; needs either the Cloud Quotas API
  or derived estimation from our own ledger. **Open question.**
- **Codex / OpenAI** — `x-ratelimit-*` headers; standard shape.

A provider that cannot report headroom still contributes *spend* from the
ledger, so it degrades to derived-only rather than being absent.

### 4.3 Derived signals

From the sample series plus the ledger:

- **headroom** = `1 - utilization` (or `remaining/limit`)
- **time_to_reset** = `reset_at - now`
- **burn rate** = tokens/minute over a trailing window, from `token_ledger`
- **projected exhaustion** = `remaining / burn_rate`
- **target burn** = `remaining / time_to_reset` ← *the number this whole spec exists to produce*

The steering signal is `target_burn / actual_burn`:
- `> 1` — under-using the window; there is room for more concurrency
- `≈ 1` — on pace
- `< 1` — will hit the wall before reset; throttle

## 5. Phasing

Deliberately observability-first. The scheduling change is not safe to design
until the data exists to validate it against.

### Phase A — Observe (no behavior change)
- `usage_samples` table + Alembic revision
- `UsageProvider` protocol + Anthropic implementation lifted from `system_commands.py`
- Runtimes record response headers on every real call
- Idle probe as cold-start fallback, rate-limited to at most one per N minutes
- `aq system usage` reporting current headroom, reset horizon, and trailing burn rate

**Exit criterion:** a full reset window observed end-to-end, with samples dense
enough to plot. Until that exists, any scheduling formula is a guess.

### Phase B — Understand
- Trend view in the dashboard
- Answer empirically: what does a task actually cost? Is variance narrow enough
  to predict from concurrency? How much does cache-read change the picture?
- Populate `PricingConfig` so cost rollups mean something

### Phase C — Steer
- `BudgetManager` gains `recommended_concurrency(project)` from §4.3
- Scheduler uses `min(max_concurrent_agents, recommended)` — the static cap
  **stays as a hard ceiling**; the budget signal may only lower it
- Hysteresis so concurrency does not oscillate tick to tick
- `worktrees` slot growth already ramps one-per-dispatch, so raising the ceiling
  is naturally gradual

### Phase D — Multi-provider arbitrage (speculative)
Route a task to whichever platform has the most headroom, via profile `runtime` /
`agent_name`. Only meaningful once every provider reports headroom, which today
none but Anthropic can.

## 6. Open questions

1. **Does the Anthropic probe work at all?** It returned 404 with the OAuth
   token. Confirm the API-key path and whether OAuth needs a different endpoint
   or beta header. If neither works, header-scraping from real responses is not
   just cheapest — it is the only option.
2. **Which account do we actually consume?** Agent sessions run the Claude Code
   CLI under OAuth (`default_claude_max_20x`); `chat_provider` uses Gemini; the
   supervisor may use an API key. These are **separate quotas**, and a single
   global number would be meaningless. Samples must be keyed by account, and the
   ledger needs to attribute spend to the same identity.
3. **Is the 5-hour window even visible?** Claude Max subscription limits are not
   necessarily expressed in the unified headers the way API rate limits are.
   Phase A must confirm what is actually observable before Phase C depends on it.
4. **Gemini headroom** — no known per-response header. Cloud Quotas API, or
   accept derived-only.
5. **What is the right objective?** "Spend the window fully" and "never hit the
   wall" conflict. A wall-hit mid-task costs a paused task and a retry; leaving
   10% unused costs nothing visible. The utility function is asymmetric and
   should probably target ~90% with a reserve, not 100%.

## 7. Relationship to slot count

Slots and quota are independent ceilings:

- **slots** cap how many agents *can* run — cheap, elastic, lazily created
- **quota** caps how much they can *spend* — the real scarce resource

Set slots comfortably above the expected quota-bounded concurrency and let the
budget signal do the limiting. `max_concurrent_agents = 4` (set 2026-08-24) is a
starting point, not a tuned value; Phase C is what makes it stop mattering.

**Caveat:** plan subtasks share one branch and git allows a branch in exactly one
worktree, so sibling subtasks serialize regardless of either ceiling
(worktree-execution design §4.4). Raising concurrency does not speed up plans —
only independent tasks.
