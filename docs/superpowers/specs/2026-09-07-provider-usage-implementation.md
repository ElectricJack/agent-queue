# Provider usage — implementation spec

<!-- aq:historical -->
> **Historical design record.** This spec describes one feature as it was
> designed, not as the code stands today. Start at [the documentation
> home](../../README.md) for current behaviour; see [historical
> material](../../history/README.md).

**Date:** 2026-09-07
**Status:** ready to build
**Design:** `docs/superpowers/specs/2026-09-07-provider-usage-design.md`
**Shape:** one epic, seven child tasks, all `standard-medium-claude`.

The design settles *what* and *why*; this settles *where* and *in what order*.
Every task below is written to be executed without reading the others: it
names its files, its tests, and what "done" means.

## Global constraints (apply to every task)

> **Read [Amendments](#amendments--2026-09-07-re-verified-against-the-box-and-the-tree)
> at the end of this file before starting your section.** They are corrections
> found by re-running the probe and reading the code these tasks touch, and where
> one contradicts a section above, the amendment wins. A1–A2 change T3, A3 changes
> T1/T5/T6/T7, A4 changes T5, A5–A6 change T2/T1, A7–A8 change T4.

- Python 3.12, ruff line-length 100. `ruff check <touched paths>` before close.
- Commands return `{"success": bool, ...}` and go through `CommandHandler`.
- Async only — `GitManager`'s `a`-prefixed API, `asyncio.create_subprocess_exec`,
  never `subprocess.run()` in production code.
- **Never** run `alembic upgrade` / `aq start` inside a worktree slot. Generate
  the revision, review it, and let the operator apply it. `run_schema_setup`
  refusing to migrate the production URL is the guard working.
- Tests: `aq test tests/test_<area>.py` for what you touched; one area-wide run
  at the end. Never bare `pytest tests/`, never raise `-n`.
- A `CheckConstraint` must be named `ck_<table>_<what>`; `server_default` takes
  the bare value; booleans use `sa.false()` / `sa.true()`.

## Vocabulary

A **snapshot** is one observation of one limit window: `(provider, window,
scope) → used_percent, resets_at` at `observed_at`. Codex snapshots arrive
passively off transcripts; Claude snapshots arrive from a probe. Nothing in
the storage or API layer cares which.

---

## T1 — storage

**Files:** `src/database/tables.py`, `migrations/versions/<new>.py`,
`src/database/queries/provider_usage_queries.py` (new),
`src/database/base.py` (mix the new class in), `tests/test_provider_usage_queries.py` (new).

Table `provider_usage_snapshots`, append-only:

| column | type | notes |
|---|---|---|
| `id` | Integer PK | |
| `provider` | Text, not null | `codex` \| `claude` |
| `account_label` | Text, not null, `server_default=""` | plan/limit id |
| `window` | Text, not null | `session` \| `week` \| `primary` \| `secondary` |
| `scope` | Text, not null, `server_default=""` | `all models`, `Fable`, `` |
| `used_percent` | Float, not null | 0–100 |
| `resets_at` | Float, nullable | epoch |
| `observed_at` | Float, not null | epoch |
| `source` | Text, not null | `transcript` \| `probe` |

- `CheckConstraint("source IN ('transcript','probe')", name="ck_provider_usage_snapshots_source")`.
- Index `(provider, window, scope, observed_at DESC)` — every read is
  "newest per series".

`ProviderUsageQueryMixin`:

- `record_provider_usage(snapshots: Sequence[ProviderUsageSnapshot]) -> int`
  — one executemany insert; returns rows written. **Drops a snapshot whose
  `(provider, window, scope, used_percent, resets_at)` matches the newest
  stored row for that series** so an idle fleet re-reporting 88% every two
  seconds does not fill the table; a changed percent always writes.
- `latest_provider_usage(provider: str | None = None) -> list[dict]` — newest
  row per `(provider, window, scope)`.
- `provider_usage_series(provider, window, scope, since: float) -> list[dict]`.
- `purge_provider_usage(before: float, *, limit: int = 1000) -> int` — called
  from the retention sweep beside the metrics prune. Keep 90 days.

**Acceptance:** dedup proven (same reading twice → one row; changed percent →
two); `latest_provider_usage` returns one row per series with the newest
`observed_at`; migration autogenerates clean on both backends (`aq db current`
untouched — do not apply it).

---

## T2 — Codex passive capture

**Files:** `src/sessions/transcripts/base.py`, `src/sessions/transcripts/codex.py`,
`src/sessions/transcripts/watcher.py`, `tests/test_transcript_codex.py`,
`tests/test_transcript_watcher.py`. **Depends on T1.**

`payload.rate_limits` sits beside `payload.info` on the `token_count` line that
`_entry_from_line` (`src/sessions/transcripts/codex.py:216`) already handles:

```json
{"limit_id":"codex","plan_type":"pro",
 "primary":{"used_percent":88.0,"window_minutes":10080,"resets_at":1789135776},
 "secondary":null,"credits":{"has_credits":false,"balance":"0"}}
```

1. Add `rate_limits: dict | None = None` to `TranscriptEntry` (`base.py`),
   defaulted so the Claude reader and every existing construction are untouched.
2. In `codex.py`, add `_rate_limits_from_payload(payload) -> dict | None`
   returning `None` unless `primary` carries a numeric `used_percent`, and
   attach it to the entry the `token_count` branch already returns.
   `primary` → `window="primary"`, `secondary` → `window="secondary"`;
   `account_label` from `plan_type` (fall back to `limit_id`); `scope` is `""`.
3. In `watcher.py`, where the entry's `usage` is charged to the ledger, also
   call `record_provider_usage` when `rate_limits` is present. Same idempotency
   rule as usage: **charge once per assistant uuid**, so a re-tick that re-reads
   a line records nothing new.

**Do not** invent a snapshot when a session is idle — absence of new lines is
what makes the number stale, and T6 renders staleness honestly.

**Acceptance:** a fixture rollout line with `rate_limits` produces exactly one
snapshot with `used_percent=88.0`, `window="primary"`, `account_label="pro"`;
a line without `rate_limits` produces none; replaying the same line twice
still yields one row.

---

## T3 — Claude `/usage` parser (pure, no I/O)

**Files:** `src/providers/__init__.py`, `src/providers/claude_usage.py` (new),
`tests/test_claude_usage_parser.py` (new). **No dependencies — start immediately.**

`parse_usage_text(text: str, *, now: float, tz_default: str = "UTC") -> UsageParse`
where `UsageParse` carries `snapshots: list[ProviderUsageSnapshot]` and
`unparsed: bool`.

Input is the `result` field of `claude -p "/usage" --output-format json`.
Verified live output (claude 2.1.263):

```
You are currently using your subscription to power your Claude Code usage

Current session: 5% used · resets Sep 7, 3:59pm (America/Los_Angeles)
Current week (all models): 45% used · resets Sep 9, 2:59pm (America/Los_Angeles)
Current week (Fable): 81% used · resets Sep 9, 2:59pm (America/Los_Angeles)
```

Rules, all of which have a test:

- One regex per limit line:
  `^Current (?P<window>session|week \((?P<scope>[^)]+)\)): (?P<pct>\d+)% used · resets (?P<reset>.+)$`
  → `window` normalised to `session` / `week`; `scope` is the parenthesised
  string verbatim (`all models`, `Fable`) or `""` for a session line.
- **Never hard-code the model names.** The scope is data; a plan reporting
  `Opus` or a name we have never seen must round-trip untouched.
- `resets` is year-less local prose with a named zone: parse with `zoneinfo`,
  choose the next occurrence at or after `now`, store the epoch. An
  unparseable reset yields the snapshot with `resets_at=None` — a percentage
  without a clock still beats nothing.
- Text matching no limit line → `unparsed=True` and **zero** snapshots. The
  caller keeps the previous reading. This is the whole robustness story: the
  wording is a CLI implementation detail that will change under us.
- An API-key (non-subscription) preamble with no limit lines is
  `unparsed=False`, zero snapshots — "not applicable", not a fault.

**Acceptance:** the verified sample parses to three snapshots with the right
scopes and monotonically sane resets; a mangled/garbage body sets `unparsed`;
a `Current week (Opus): 3% used` line parses without code changes; a
year-boundary reset (`Dec 31` observed on `Jan 1`) resolves forward, not back.

---

## T4 — the probe command and its playbook

**Files:** `src/providers/probe.py` (new), `src/commands/provider_commands.py` (new),
`src/commands/handler.py`, `src/commands/contracts/builtin.py`,
`src/tools/definitions.py`, `src/config.py`,
`src/prompts/default_playbooks/provider-usage-probe.md` (new),
`tests/test_provider_usage_probe.py` (new). **Depends on T1, T3.**

`_cmd_provider_usage_probe(args)` — `{"provider": "claude"}` for now:

- Runs `claude -p "/usage" --output-format json` via
  `asyncio.create_subprocess_exec` with a **20s timeout** and a fixed, safe
  cwd (the daemon's data dir). Verified cost: `total_cost_usd: 0`,
  `num_turns: 0`, ~3.6s wall — the probe bills none of the quota it reports.
- Binary from config `providers.claude.binary` (default `claude`), so a box
  with a harness shim can point at it. **Never pass `--bare`** — it forces
  API-key auth and would report the wrong account entirely.
- Feeds `result` to `parse_usage_text`, writes snapshots with `source="probe"`.
- Returns `{"success": True, "snapshots": [...], "unparsed": bool}`. A
  timeout, a non-zero exit, or `is_error: true` returns
  `{"success": False, "error": ...}` **without** raising and without touching
  stored state. The last good snapshot survives every failure mode.
- Contract it in `src/commands/contracts/builtin.py` so a playbook step may
  call it.

Config block:

```yaml
providers:
  claude:
    usage_probe_enabled: true
    binary: claude
```

Policy lives in a playbook, not the orchestrator — the house rule. Ship
`src/prompts/default_playbooks/provider-usage-probe.md`: `timer.10m` →
`provider_usage_probe {provider: claude}`. Ten minutes is ~144 probes/day of a
free local command. Follow `blocked-task-escalation.md` for the file shape and
build the reviewed bundle under `tests/fixtures/playbooks/v2/provider-usage-probe/`
with `scripts/rebuild-reviewed-playbook-artifacts.py`.

Each probe leaves a zero-turn session file in `~/.claude/projects/`.
`TranscriptWatcher` only reads transcripts of live session rows, so these are
inert — note it in the operator guide, do not try to suppress it.

**Acceptance:** a faked subprocess returning the verified JSON writes three
snapshots; a timeout and a non-zero exit each return `success: False` and
write nothing; the playbook compiles and its artifact digest is committed.

---

## T5 — read API

**Files:** `src/api/providers.py` (new router) or an addition to
`src/api/metrics.py`, `src/api/models/provider.py` (new) registered in that
module's `RESPONSE_MODELS`, `openapi.json`, `packages/aq-client/**`,
`packages/aq-ts-client/**`, `tests/test_api_provider_usage.py` (new).
**Depends on T1.**

`GET /api/providers/usage` → `{"snapshots": [...], "series": {...}}`:
latest per `(provider, window, scope)`, each with `observed_at`, `resets_at`,
`stale: bool`, plus optional history when `?since=` is given.

`stale` is computed **server-side** so every client agrees: a snapshot is
stale when `now - observed_at` exceeds twice its expected interval (20 min for
`probe`, 10 min for `transcript`).

Regenerate both clients — `./scripts/regenerate-api-client.sh --offline` then
`./scripts/regenerate-ts-client.sh --from-file`. Never hand-edit anything under
`packages/aq-client/`.

**Acceptance:** `test_committed_openapi_json_matches_the_live_app_surface`
passes; a seeded stale row reports `stale: true`; an empty table returns an
empty list, not an error.

---

## T6 — dashboard

**Files:** `dashboard/src/pages/metrics/ProviderUsage.tsx` (new) + wiring in the
metrics page, `dashboard/src/api/hooks.ts`,
`dashboard/src/pages/metrics/__tests__/ProviderUsage.test.tsx` (new).
**Depends on T5.**

One card per window:

```
Codex · weekly          88%  ████████▊·  resets Thu 14:09
Claude · week (Fable)   81%  ████████··  resets Tue 14:59
Claude · session         5%  ▌·········  resets today 15:59
```

- Bar colouring: neutral < 75%, amber ≥ 75%, red ≥ 90%.
- **A stale card must say so** — `stale · last seen 4h ago` — and mute the bar.
  A frozen 88% and a live 88% are visually identical otherwise, and the Codex
  number only advances while a Codex session is live.
- No data at all → "No provider usage recorded yet", never a 0% bar.
- Follow the repo's theme tokens; the existing Metrics tab is the reference.

**Acceptance:** vitest covers fresh, stale, threshold colouring, and empty;
`npx tsc -b --noEmit` and `npx eslint` clean.

---

## T7 — doctor check

**Files:** `src/doctor/provider_checks.py` (new), `src/doctor/__init__.py`,
`tests/test_provider_doctor.py` (new). **Depends on T1, T4.**

`aq doctor --check providers.claude_usage`:

- WARN when the newest `probe` snapshot is older than 30 minutes while
  `usage_probe_enabled` is true;
- WARN when the last probe returned `unparsed` (the CLI's wording moved —
  this is the check that tells us to fix the regex);
- OK, with the current percentages in the detail line, otherwise.

Report-only. Never `--fix`: there is nothing safe to repair automatically.

**Acceptance:** all three states covered by tests; a disabled probe is OK, not
WARN.

---

## Order

T1 and T3 start together (T3 touches nothing shared). T2, T4 and T5 unblock on
T1; T6 waits for T5; T7 waits for T4. T1+T2 alone are shippable: they light up
the Codex half with no probe and no new surface.

```aq-graph
version: 1
defaults:
  profile: standard-medium-claude
  intelligence_class: standard-medium
  labels: [provider-usage]
parent:
  title: "Provider usage in the dashboard"
  description: |
    Surface each provider's own quota — not our token ledger's estimate of it —
    in the dashboard. Codex publishes rate_limits on the token_count lines the
    transcript watcher already reads; Claude has no local equivalent, so a
    periodic `claude -p "/usage" --output-format json` probe supplies it (that
    call is verified free: num_turns 0, total_cost_usd 0).

    Design: docs/superpowers/specs/2026-09-07-provider-usage-design.md
    Implementation spec: docs/superpowers/specs/2026-09-07-provider-usage-implementation.md
    Read the implementation spec's "Global constraints" before starting, and
    the section named for your task. Do not run alembic against the operator
    database from a worktree slot.
  labels: [provider-usage, epic]
  priority: 120
nodes:
  - key: storage
    title: "Provider usage: snapshot table and queries"
    description: |
      Implement T1 of docs/superpowers/specs/2026-09-07-provider-usage-implementation.md.
      Append-only provider_usage_snapshots table, ProviderUsageQueryMixin with
      record/latest/series/purge, and the alembic revision (generate and review
      it; do NOT apply it to the operator database).
      Dedup is the point of record_provider_usage: an unchanged reading must
      not write a row, a changed percent always must.
    acceptance:
      - "provider_usage_snapshots exists in tables.py with a named check constraint and the (provider, window, scope, observed_at) index"
      - "record_provider_usage drops an unchanged repeat of the newest row in a series and writes a changed one"
      - "latest_provider_usage returns exactly one row per (provider, window, scope)"
      - "a reviewed alembic revision is committed but NOT applied"
      - "aq test tests/test_provider_usage_queries.py passes on sqlite and postgres"
    priority: 130
  - key: codex-capture
    title: "Provider usage: capture Codex rate_limits from transcripts"
    description: |
      Implement T2. Carry payload.rate_limits through TranscriptEntry and the
      codex reader into a snapshot written by TranscriptWatcher, charged once
      per assistant uuid exactly like token usage. Never synthesise a snapshot
      for an idle session.
    needs: [storage]
    acceptance:
      - "a token_count fixture line with rate_limits yields one snapshot: used_percent 88.0, window 'primary', account_label 'pro'"
      - "a token_count line without rate_limits yields no snapshot"
      - "replaying the same line twice still yields one row"
      - "aq test tests/test_transcript_codex.py tests/test_transcript_watcher.py passes"
    priority: 125
  - key: claude-parser
    title: "Provider usage: parse claude /usage output"
    description: |
      Implement T3 — a pure parser, no subprocess and no database. The verified
      sample text is in the spec. Unrecognised text must yield zero snapshots
      and unparsed=True rather than a guess or an exception.
    acceptance:
      - "the verified three-line sample parses to session/week(all models)/week(Fable) with correct percentages"
      - "an unseen scope such as 'Opus' parses with no code change"
      - "garbage text sets unparsed and returns no snapshots"
      - "a Dec 31 reset observed on Jan 1 resolves forward"
      - "aq test tests/test_claude_usage_parser.py passes"
    priority: 125
  - key: probe-command
    title: "Provider usage: probe command, config and timer playbook"
    description: |
      Implement T4. Async subprocess with a 20s timeout, the parser from T3,
      a builtin contract, config under providers.claude, and the timer.10m
      default playbook with its reviewed bundle. Every failure path returns
      success false and leaves the last good snapshot untouched. Never pass
      --bare to claude.
    needs: [storage, claude-parser]
    acceptance:
      - "a faked subprocess returning the verified JSON writes three snapshots with source 'probe'"
      - "timeout and non-zero exit each return success false and write nothing"
      - "provider_usage_probe is contracted in src/commands/contracts/builtin.py"
      - "the default playbook compiles and its reviewed artifact bundle is committed"
      - "aq test tests/test_provider_usage_probe.py passes"
    priority: 120
  - key: read-api
    title: "Provider usage: GET /api/providers/usage"
    description: |
      Implement T5. Response model registered in RESPONSE_MODELS, staleness
      computed server-side, both generated clients regenerated with the pinned
      generator (never hand-edit packages/aq-client).
    needs: [storage]
    acceptance:
      - "GET /api/providers/usage returns latest-per-series with stale computed server-side"
      - "an empty table returns an empty list rather than an error"
      - "openapi.json and both generated clients are regenerated and committed"
      - "aq test tests/test_api_provider_usage.py tests/test_api_client_contract.py passes"
    priority: 115
  - key: dashboard-card
    title: "Provider usage: dashboard cards on the Metrics tab"
    description: |
      Implement T6. One card per window with percentage, bar, reset time, and
      an explicit stale treatment — a frozen number must never look live.
    needs: [read-api]
    acceptance:
      - "cards render provider, window, percent, bar and reset time"
      - "a stale snapshot renders 'stale · last seen ...' with a muted bar"
      - "thresholds colour at 75% and 90%; no data renders an explicit empty state"
      - "vitest passes and npx tsc -b --noEmit / npx eslint are clean"
    priority: 110
  - key: doctor-check
    title: "Provider usage: doctor check for probe health"
    description: |
      Implement T7 — providers.claude_usage, report-only, covering stale
      snapshots, a probe whose output stopped parsing, and the healthy case.
    needs: [probe-command]
    acceptance:
      - "stale probe snapshots WARN; an unparsed last probe WARNs; healthy reports the percentages"
      - "a disabled probe reports OK rather than WARN"
      - "the check is report-only with no --fix path"
      - "aq test tests/test_provider_doctor.py passes"
    priority: 105
```

---

## Amendments — 2026-09-07, re-verified against the box and the tree

Everything above stands. These are corrections found by re-running the probe
and reading the code the tasks touch; each one would otherwise be discovered
as a failing test or, worse, as a wrong number on a card. Where an amendment
contradicts a section above, **the amendment wins**.

### A1 — `/usage` drops `:00` on the hour (affects T3)

The sample in T3 was taken at `3:59pm`. Re-run at 13:33 the same day, the
same command prints:

```
You are currently using your subscription to power your Claude Code usage

Current session: 6% used · resets Sep 7, 4pm (America/Los_Angeles)
Current week (all models): 46% used · resets Sep 9, 3pm (America/Los_Angeles)
Current week (Fable): 81% used · resets Sep 9, 3pm (America/Los_Angeles)
```

`4pm`, not `4:00pm`. A reset parser that only accepts `%b %d, %I:%M%p` fails on
the majority of real readings — weekly windows reset on the hour. Accept both
`%b %d, %I%p` and `%b %d, %I:%M%p`, case-insensitively for the meridiem. Both
spellings need a test.

### A2 — the `result` body is full of other percentages (affects T3)

The verified sample above is only the head of `result`. What follows it is:

```
What's contributing to your limits usage?
Approximate, based on local sessions on this machine — ...

Last 24h · 2240 requests · 72 sessions
  64% of your usage was at >150k context
  32% of your usage came from subagent-heavy sessions
  Top subagents: fork 14%, general-purpose 6%
```

Those lines are not limit lines and must contribute nothing. The `^Current `
anchor plus `re.MULTILINE` is what keeps them out — so put this block in the
fixture and assert three snapshots, not "a body that happens to contain only
limit lines". Untested anchoring is the failure mode that puts `64%` on a card.

Also make the `· resets …` tail **optional** in the regex: a limit line without
a reset clause is a snapshot with `resets_at=None`, not a parse failure. T3
already says that for an unparseable reset; it must also hold for an absent one.

### A3 — dedup and staleness contradict each other; add `last_seen_at`
(affects T1, T5, T6, T7)

T1 drops an unchanged reading, T5 computes `stale` from `observed_at`, and T7
WARNs when the newest probe snapshot is older than 30 minutes. Put together,
a perfectly healthy Claude week window sitting at 81% for six hours writes no
row for six hours and is then reported stale by the API, rendered muted by the
card, and WARNed about by the doctor — while the probe is succeeding every ten
minutes. That is exactly the "frozen and live look identical" failure the
design set out to prevent, inverted.

Fix it in T1 with one more column:

| column | type | notes |
|---|---|---|
| `last_seen_at` | Float, not null | when this value was last **confirmed** |

- `record_provider_usage` on a duplicate updates that row's `last_seen_at` to
  `max(last_seen_at, observed_at)` and still returns "no row written".
- An insert sets `last_seen_at = observed_at`.
- `observed_at` keeps its meaning — when the value first appeared — so the
  sparkline series is unchanged.
- **T5, T6 and T7 compute staleness from `last_seen_at`, never from
  `observed_at`.**

Two more rules for `record_provider_usage` while it is being written:

- Compare `used_percent` with a tolerance (`abs(a - b) < 1e-9`), not `==` on
  floats, and treat `None`/`None` resets as equal.
- Drop an observation older than the newest stored row for its series
  entirely — write nothing, not even `last_seen_at`. A rewound or replayed
  transcript must not be able to walk a percentage backwards.

`purge_provider_usage` must keep the newest row of every series regardless of
age: pruning a card's only value turns a known-but-idle account into "no data".

### A4 — T5 does not use `RESPONSE_MODELS`

`RESPONSE_MODELS` maps **command name → model** for the auto-generated
CommandHandler endpoints (`src/api/models/__init__.py:get_all_response_models`).
A hand-written codegen router is not in it: `src/api/models/metrics.py`, the
closest precedent and the one to copy, has no `RESPONSE_MODELS` at all.
Register the router in `src/api/app.py` with `include_router` and nothing else.

Also: `packages/aq-ts-client/src/` is gitignored and generated, so "both
clients committed" means `openapi.json` plus `packages/aq-client/**`. Run
`./scripts/regenerate-ts-client.sh --from-file` anyway — the dashboard build
in T6 needs the types.

### A5 — the `token_count` branch returns `None` today without usage
(affects T2)

`_entry_from_line` currently does:

```python
usage = _usage_from_token_count(info) if isinstance(info, dict) else None
if not usage:
    return None
```

so a line carrying `rate_limits` but no usable `info` is dropped before
anything can attach a rate limit to it. T2 step 2 says "attach it to the entry
the `token_count` branch already returns" — there may not be one. Change the
gate to `if not usage and not limits: return None` and carry the two
independently.

The watcher block needs the same treatment, and must **not** be gated on
`agent_id`: a provider quota is an account fact, not an agent fact, and the
existing code skips `_record_usage` entirely when the agent cannot be
resolved.

```python
if entry.type == "assistant" and (entry.usage or entry.rate_limits):
    if entry.uuid in state.charged_uuids:
        continue
    if entry.usage and agent_id:
        await self._record_usage(row, entry, agent_id=agent_id)
    if entry.rate_limits:
        await self._record_provider_rate_limits(row, entry)
    state.charged_uuids.add(entry.uuid)
    state.last_charged_uuid = entry.uuid
```

Idempotency is sound as specified: the codex reader's uuid is
`f"{stem}:{line_start}"`, a byte offset, so a replayed line yields the same
uuid and `charged_uuids` rejects it. Wrap the write in `try/except Exception`
with a `logger.debug`, exactly like `_record_usage` — a malformed
`rate_limits` block must never abort a watcher tick.

Codex's `resets_at` is an absolute epoch in the observed payload. Accept
`resets_in_seconds` as a fallback (`observed_at + value`) and `None` when
neither is present.

### A6 — `window` is a reserved word in PostgreSQL (affects T1)

SQLAlchemy quotes it in both the DDL and every generated statement, so the
column name is safe as specified. Add one PostgreSQL test that inserts and
reads a row so a future hand-written SQL string cannot regress it silently.

### A7 — T4 must persist the probe's own health for T7

T7 WARNs when "the last probe returned `unparsed`", which nothing in T4
currently records: a failed probe writes no snapshot, so the doctor cannot
tell "the wording moved" from "nobody has probed lately". T4 must write a
`system_config` row under `providers.claude_usage.last_probe`:

```json
{"ok": false, "unparsed": true, "not_applicable": false,
 "error": null, "ts": 1789135776.0, "recorded": 0}
```

on **every** probe, success or failure. T7 reads exactly that key. Follow
`_DAEMON_STARTS_KEY` in `src/database/queries/metrics_queries.py` for the
`system_config` read/write idiom.

Give the probe's staleness horizon a config field —
`providers.claude.stale_after_seconds`, default 1500 (twice the playbook's
ten-minute cadence plus slack) — and have T5, T6 and T7 all read that one
number, plus a separate `providers.codex_stale_after_seconds` (default 4h,
since Codex only advances while a Codex session is live). Three
independently hard-coded horizons would eventually disagree about what the
same card means.

### A8 — T4 contract details

`EffectSubject` has no member that fits; add `PROVIDER_USAGE = "provider_usage"`
to `src/commands/contracts/models.py` and use
`CreateClause(subject=EffectSubject.PROVIDER_USAGE)`. A `PRESENTATIONS` entry
is mandatory, not optional — `test_presentation_labels_name_real_fields`
requires every `arg_labels`/`result_labels` key to name a real model field and
every `subject_labels` key to name a subject the command's own clauses use.

Make `unavailable` (the `claude` binary is missing or timed out) a **success**
outcome alongside `probed` and `unparsed`. A box without the CLI is a fact
about the box, not a broken playbook step, and a failing step every ten
minutes would fill the run overlay with noise the operator cannot act on.
