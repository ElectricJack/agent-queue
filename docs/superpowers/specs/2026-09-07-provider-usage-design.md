# Provider usage in the dashboard

<!-- aq:historical -->
> **Historical design record.** This spec describes one feature as it was
> designed, not as the code stands today. Start at [the documentation
> home](../../README.md) for current behaviour; see [historical
> material](../../history/README.md).

**Date:** 2026-09-07
**Status:** design — not implemented
**Touches:** `src/sessions/transcripts/codex.py`, a new `src/providers/usage/`,
`src/commands/`, `src/api/metrics.py`, `dashboard/src/pages/metrics/`

## Problem

The dashboard charts the tokens *we* spend (`MetricsSampler`, the `tokens.*`
series). It says nothing about the quota those tokens are drawn from, so the
first sign that a plan window is exhausted is agents failing. At the time of
writing this box's Codex account is at 88–91% of its weekly window and the
Claude subscription is at 81% of the weekly Fable allowance; neither number is
visible anywhere in the product.

The two harnesses expose that number in completely different ways, and the
design follows that split rather than papering over it.

## Codex — passive, already on disk

Every `token_count` line of a Codex rollout carries the provider's own
accounting as a sibling of the `info` key the transcript reader already
parses (`src/sessions/transcripts/codex.py:216`):

```json
{"type":"token_count",
 "info":{"last_token_usage":{...},"total_token_usage":{...}},
 "rate_limits":{"limit_id":"codex","plan_type":"pro",
   "primary":{"used_percent":88.0,"window_minutes":10080,"resets_at":1789135776},
   "secondary":null,
   "credits":{"has_credits":false,"unlimited":false,"balance":"0"}}}
```

`TranscriptWatcher` polls these files every ~2s for every live session, so the
figure is already flowing past us — it is dropped because `TranscriptEntry`
has nowhere to put it. Nothing needs to be launched or scraped.

**Freshness caveat:** the number only advances while a Codex session is live.
With no Codex agent running, the last snapshot ages, and the UI must say so
rather than imply the account is idle at 88%.

## Claude — an active probe, and it is free

There is no equivalent on disk. Claude Code transcripts carry per-turn `usage`
(tokens) but nothing about the account's windows, and `~/.claude/` holds no
quota cache (`stats-cache.json` is message/session/tool counts). The `/usage`
view fetches from the server.

Verified on this box (`claude` 2.1.263):

```
$ claude -p "/usage" --output-format json
```

- returns in ~3.6s wall (`duration_ms: 1118`),
- `"is_error": false`, `"subtype": "success"`,
- **`total_cost_usd: 0`, `num_turns: 0`, every field of `usage` zero** — the
  slash command is handled locally and bills no model request, so probing
  does not consume the very quota it reports,
- `result` carries the text, whose limit lines are stable and greppable:

```
Current session: 5% used · resets Sep 7, 3:59pm (America/Los_Angeles)
Current week (all models): 45% used · resets Sep 9, 2:59pm (America/Los_Angeles)
Current week (Fable): 81% used · resets Sep 9, 2:59pm (America/Los_Angeles)
```

This is the provider's own number for the same credentials the fleet's agents
use, which is exactly what makes it worth more than an estimate derived from
our token ledger.

### Parsing

One regex per limit line:

```
^Current (?P<window>session|week \((?P<scope>[^)]+)\)): (?P<pct>\d+)% used · resets (?P<reset>.+)$
```

yielding rows of `(window, scope, used_percent, resets_at)`. Rules:

- **Never fail loudly on prose.** The wording is a CLI implementation detail
  that can change under us. A probe that matches no line is a *parse failure*:
  keep the previous snapshot, record the failure with the raw text, and let
  `aq doctor --check providers.claude_usage` report it. A blank card beats a
  wrong number.
- The scope name is whatever the plan reports (`all models`, `Fable`, `Opus`)
  — never hard-code a model list; the row is keyed by the string.
- `resets` is year-less local prose with a named zone: parse with
  `zoneinfo` and pick the next occurrence, and store the absolute epoch.
- Non-subscription auth (API key) prints a different first line and no limit
  lines: that is "not applicable", not a failure.

### Cadence and where the policy lives

Mechanism in code, policy in a playbook — the house rule. The command is
`provider_usage_probe` (contracted in `src/commands/contracts/builtin.py`,
so it is callable from a playbook step); the *schedule* is a system default
playbook `provider-usage-probe.md`:

```
timer.10m → provider_usage_probe { provider: "claude" }
```

Ten minutes is ~144 probes/day of a free, 3.6s local command, which is
comfortably fine and fast enough for a window that moves in whole percent.
Codex needs no timer at all — it rides the transcript watcher. An operator who
wants a different cadence edits the playbook, not the daemon.

Each probe spawns a `claude` process that writes a zero-turn session file into
`~/.claude/projects/`. `TranscriptWatcher` only reads transcripts belonging to
live session rows, so these are inert; they are simply clutter, and worth a
note in the operator guide.

## Storage

One table, `provider_usage_snapshots`, append-only:

| column | meaning |
|---|---|
| `provider` | `codex` \| `claude` |
| `account_label` | plan/limit id, e.g. `pro`, `codex` |
| `window` | `session` \| `week` \| `primary` \| `secondary` |
| `scope` | `all models`, `Fable`, `` (Codex has none) |
| `used_percent` | float |
| `resets_at` | epoch, nullable |
| `observed_at` | epoch |
| `source` | `transcript` \| `probe` |

Append-only rather than latest-value, because the interesting question a week
in is "how fast are we burning it", which needs the series. Retention follows
the `metrics_samples` pattern (per-tier prune in the same sweep).

**Deliberately not a `metrics_samples` series.** Those are rates sampled once
a second; this is a gauge with a reset clock, arriving irregularly from two
different mechanisms. Forcing it into the sampler would mean inventing a
sample every second for a number that changes every ten minutes.

**Account identity is a known gap.** Neither source names *which* account it
measured — the Codex rollout gives `plan_type`/`limit_id`, the Claude probe
gives nothing. With one account per box that is fine. If the `CODEX_HOME`
split is ever adopted, snapshots from two accounts would be indistinguishable
and the freshest would silently win, so `account_label` should be set from the
launching profile at that point rather than from the payload.

## Surface

- `GET /api/providers/usage` — latest snapshot per (provider, window, scope)
  plus optional history for a sparkline.
- Metrics tab: one card per window — `Codex weekly · 88% · resets Thu 14:09`,
  `Claude week (Fable) · 81% · resets Tue 14:59` — with the bar colouring at
  75/90%, and an explicit `stale · last seen 4h ago` when the newest snapshot
  is older than twice its expected interval. Staleness must be visible: a
  frozen 88% and a live 88% look identical otherwise.
- `aq doctor --check providers.claude_usage` — probe failing, parse failing,
  or snapshots stale.

## Work breakdown

| Step | Scope |
|---|---|
| 1 | `rate_limits` through `TranscriptEntry` → watcher → store (Codex, passive) |
| 2 | table + queries + retention |
| 3 | `provider_usage_probe` command + parser + contract + default playbook |
| 4 | `GET /api/providers/usage` + response model + client regeneration |
| 5 | Metrics-tab cards + staleness + doctor check |

Steps 1–3 are the substance; 4–5 are conventional. Roughly a day and a half
with the tests this repo expects, and steps 1–2 are independently shippable.

## Rejected

- **Deriving Claude usage from our token ledger.** We have every turn's tokens
  and could integrate them over a rolling 5-hour window, but the subscription's
  limits are not a pure token count, and the ledger only sees sessions this
  daemon launched — not the operator's own terminals, other machines, or
  claude.ai, all of which draw on the same window. It would be an estimate
  that disagrees with `/usage` and would be trusted more than it deserves.
  The probe is cheap enough that the estimate has no reason to exist.
- **Scraping an internal HTTP endpoint with the stored OAuth token.** Fewer
  moving parts than spawning a CLI, but it is an unversioned private surface
  and a credential-handling liability; `claude -p "/usage"` is the supported
  path to the same number.
- **`claude --bare`.** Tempting for a probe, but it forces API-key auth and
  never reads OAuth — the exact credentials whose quota we are asking about.
