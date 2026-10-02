# Token-efficiency epic: integration check, baseline and staged rollout

Task: `azure-vault-92.8` (epic `azure-vault-92`), 2026-10-02. Evidence directory:
`~/.agent-queue/operator-checks/usage-audit-20261001/rollout-validation/` (frozen
read-only exports, reports and the comparison, mode 0600).

## Verdict

The epic's seven completed outputs are integrated on
`aq/epic/epic-reduce-aq-token-usage-without-sacrificing-correctness` at `f813b46f6`
and pass their focused checks together. None of them is on `main` or running on the
daemon: the operator database is at `a00000000054`, and the epic's delivery waits for
its remaining children (this task and `azure-vault-92.9`). No production effect of
the epic can be measured yet, and this report claims none. It gives:

- the integrated candidate's verification, and an exact measurement of the one
  change that can be measured offline (startup prompt bytes);
- a corrected-accounting baseline for two 24-hour windows, by harness and by
  matched cohort, produced by a new read-only tool;
- a staged rollout with a check, a halt condition and a rollback for each stage.

The baseline shows why stage gates must use matched cohorts: with no epic change
deployed, the second window already trips three halt conditions (a provider quota
reset moved most routing to Codex, and a slot-reset failure loop inflated repair
churn). Neither raw token totals nor a single before/after pair can attribute a
saving here, and nothing in this report is a subscription-quota figure.

## Integrated outputs

Each child was reviewed at the head below and merged onto the epic branch by the
integration service (receipt `after` SHA). Child checks are the ones recorded in
each task's completion.

| Task | Change | Reviewed head | On epic at | Recorded checks | Measured so far (offline) |
|---|---|---|---|---|---|
| 92.1 | Claude usage counted once per API call; append-only ledger deltas; migration `a00000000055`; read-only reconciliation | `254fac723` | `2590e79f9` | 240 area + focused; migration guards | Frozen window: ledger 1.915 B vs 1.009 B call maxima (906 M excess token events) |
| 92.3 | Native worker compaction (default 160 k), checkpoint guidance, handoff state | `93da7393b` | `c3f5a057e` | 635 area | Synthetic wake 720,784 → 499 bytes |
| 92.2 | Durable waits replace polling; claim-fenced result consumption | `5efde9196` | `e2aea1d98` | 665 area, 62 focused, 1 tmux | Scenario: 2 worker commands vs 22-call polling loop |
| 92.5 | Compact startup guidance; opt-in `--save-output` | `29aaea269` | `91e8d9940` | 967 area | See startup bytes below |
| 92.4 | Bounded live provider/capacity context for routing | `54c8a6663` | `a1dccd5cc` | 560 area, e2e smoke 19/19 | Fixture 5,436 JSON bytes |
| 92.6 | Router prefers healthy Codex for routine work (reviewed bundle, operator activation) | `63307849c` | `fc96266b3` | 495 area | Replay sample of one route; no fleet claim |
| 92.7 | Plugin value study (no code) | `fc96266b3` | `f813b46f6` | — | Recommends winnow off (already off), fast-jev off for workers via 92.9, superpowers kept |

Integration repairs `c3f5a057e`, `e2aea1d98`, `91e8d9940` and `a1dccd5cc` combined
overlapping prime and routing sources. `azure-vault-92.9` (worker-only Claude plugin
overrides) is still DEFINED, gated on review `rev-nimble-torrent`.

### Combined-candidate checks (this task, `f813b46f6`)

- `POSTGRES_TEST_DSN=…:5534 PYTHONPATH=packages/aq-client aq test <the union of the
  children's focused files> -q --tb=short`: **1948 passed** in 131 s (file list in
  the task comment of 2026-10-02).
- A second sweep over the children's remaining area suites (all `test_prime_*`
  and `test_transcript_*`, CLI, profiles, routing, docs guards, single migration
  head, import cycles, usage parsers, task-session attempts, generated artifacts,
  metrics): **1361 passed, 8 skipped**.
- Found by the docs-guard area run and fixed here (the sweep includes the fix):
  `tests/test_docs_sync.py` failed on the candidate because 92.1's
  `transcript_usage_calls` table had no section in `docs/specs/database.md`; it
  would have failed the delivery's full run.
- `scripts/regenerate-generated.sh --check`: generated files current.
- One Alembic head; `a00000000055` is still the next free revision against `main`
  (`5de0a28c7`).

### Startup prompt bytes on the integrated tree

Measured with the method in [worker-startup-output](../guides/worker-startup-output.md)
(shipped worker profile Rules + tool guidance + completion protocol; task content,
AGENTS, harness system text and tool schemas excluded). The epic base `8f3e95937`
and `main` render identical bytes.

| Lifecycle / delivery | Base and main | Candidate | Change |
|---|---:|---:|---:|
| task / review | 19,327 | 14,104 | −27.0% |
| task / development | 15,632 | 13,558 | −13.3% |
| pool / review | 20,674 | 14,735 | −28.7% |
| pool / development | 16,979 | 14,189 | −16.4% |

Claude and Codex render the same sections. The candidate is 1.1–1.7 KB larger than
92.5's isolated result because integration added the durable-wait and handoff
guidance. At roughly 1.3 k fewer chars/4 per later request, this is about 1–2% of a
median Claude attempt's 4.4–5.7 M cache-read events: real but small.

## Measurement tool

`scripts/token-efficiency-report.py` (logic in `src/metrics/token_efficiency.py`) is
operator-run and makes no model call:

```bash
python scripts/token-efficiency-report.py export --since <ISO> --until <ISO> --output w.json
python scripts/token-efficiency-report.py report --export w.json --output w-report.json
python scripts/token-efficiency-report.py compare --before a-report.json --after b-report.json
```

- **export** reads `task_session_attempts`, task kinds, completion records, routes,
  per-attempt `token_ledger` sums and open attempts inside a READ ONLY transaction
  (`default_transaction_read_only=on`). It writes nothing to the database.
- **report** reads each attempt's local transcript (Claude main + subagent files by
  work-dir slug and session key; Codex rollout by session key). Claude: one call per
  `message.id`, maximum per token category. Codex: one call per `response_id`;
  uncached input = input − cached. An attempt owns its conversation from its claim
  (from session start for a session's first attempt) to the session's next claim.
  An attempt that ends in under 60 s without an outcome is churn: counted, but kept
  out of per-attempt distributions.
- **compare** matches cohorts of harness × model × intelligence class × task kind
  (untyped `repair-*` is `integration-repair`) with at least five attempts in both
  windows and lists halt reasons.

It reports calls per attempt, startup context (first call), per-call context
p50/p90/max, cache read / cache write / uncached input / output separately, command
categories (polls = sleep + status polls + output polls + inbox; claims, closes,
heartbeats, tests and durable waits listed apart), route distribution, pass rate,
churn, integration-repair share, decided-attempt duration, live attempts open
beyond the stuck threshold, and the ledger's totals against corrected transcript
totals for the same attempts. A 24-hour window reports in about 2.5 s.
Command categories are regex heuristics over tool-call text; they overlap and do
not measure avoidable work.

## Baseline: two windows, no epic change live

B = 2026-09-30 17:42:08Z → 2026-10-01 17:42:08Z (the original audit window).
R = 2026-10-01 17:42:08Z → 2026-10-02 17:42:08Z. Medians are per attempt.

| Metric | B claude | R claude | B codex | R codex |
|---|---:|---:|---:|---:|
| Attempts (tasks) | 74 (73) | 18 (18) | 30 (27) | 72 (53) |
| Measured (transcript, not churn) | 74 | 18 | 28 | 51 |
| Pass rate (decided) | 1.00 | 1.00 | 0.86 | 0.90 |
| No-outcome churn (< 60 s) | 0 | 0 | 1 | 21 |
| Calls/attempt median (mean) | 58 (87.1) | 48 (73.7) | 77 (86.4) | 41 (48.9) |
| Startup context median | 48,490 | 48,603 | 19,391 | 19,391 |
| Call context p50 / p90 / max | 147,603 / 387,023 / 616,057 | 138,758 / 344,927 / 472,763 | 132,132 / 207,647 / 247,209 | 102,222 / 178,656 / 247,698 |
| Cache read / attempt | 5,686,448 | 4,381,401 | 9,683,008 | 3,549,056 |
| Cache write / attempt | 113,836 | 100,104 | 0 | 0 |
| Uncached input / attempt | 115 | 96 | 215,358 | 130,935 |
| Output / attempt | 28,529 | 18,448 | 34,909 | 17,217 |
| Polls/attempt | 11.6 | 7.3 | 25.5 | 9.3 |
| Durable waits/attempt | 0.50 | 1.11 | 1.29 | 1.25 |
| Heartbeats/attempt | 5.5 | 3.9 | 3.4 | 2.0 |
| Test commands/attempt | 9.1 | 6.1 | 12.1 | 5.8 |
| Native subagent calls | 707 | 145 | 0 | 0 |
| Compactions | 1 | 0 | 0 | 0 |
| Decided duration median / p90 (s) | 843 / 3,503 | 558 / 2,463 | 1,533 / 4,628 | 756 / 1,963 |

Window totals (corrected token events): B Claude 1.303 B cache read, 15.0 M cache
write, 14 k uncached input, 4.4 M output; B Codex 309 M cached input, 6.7 M uncached,
1.2 M output. R Claude 244 M / 3.4 M / 2.9 k / 1.05 M; R Codex 259 M / 8.0 M / 1.3 M.
Seven OpenCode attempts in B and one in R have no transcript reader and are counted
only in outcomes, routes and latency. R includes this task's own open attempt;
every report counts transcript calls only up to its export timestamp, so it can be
reproduced from the frozen export.

**Routes:** B Claude 73, Codex 25, OpenCode 5 → R Codex 53, Claude 18, OpenCode 1.
Codex's weekly quota reading climbed from 68% to 100% during B and was reset (about
20%) by the start of R, while 92.6 is not active: the shift follows provider
pressure, not policy.

**Completions in window:** B 100 pass / 4 fail; R 61 pass / 5 fail.
**Integration repair** share of tasks: 0.18 → 0.42. **Churn:** 21 Codex attempts on
8 repair tasks ended `slot_reset_failed` within 1–3 s (2026-10-01 01:24Z → 10-02
02:11Z), including this epic's own repair `repair-bcab5af6…-2`.

**Ledger against corrected transcripts (same attempts):**

| Harness | Window | Cache read | Cache write | Uncached input | Output |
|---|---|---:|---:|---:|---:|
| Claude | B | 1.750× | 1.654× | 1.678× | 1.925× |
| Claude | R | 1.803× | 1.744× | 1.677× | 1.951× |
| Codex | B | 0.955× | — | 0.952× | 0.948× |
| Codex | R | 0.954× | — | 0.955× | 0.967× |

The Claude duplication fixed by 92.1 is still live. Codex, which never had it,
calibrates the attribution difference between the two sources at about 0.95.

**Context reached:** 38% (B) and 44% (R) of Claude worker attempts, and 46% / 44% of
Claude calls, ran at ≥ 160 k context; Codex 66% / 25% of attempts. This is the
population 92.3's default compaction will act on.

### Matched cohorts, B → R

| Cohort | n | Pass | Calls | Polls | Duration (s) | Cache read | Output |
|---|---|---|---|---|---|---:|---:|
| claude opus std-high bugfix | 44 → 5 | 1.00 → 1.00 | 65 → 151 | 10.6 → 4.4 | 1,067 → 1,736 | 6.9 M → 38.5 M | 36 k → 127 k |
| claude opus std-high integration-repair | 18 → 11 | 1.00 → 1.00 | 33 → 36 | 7.0 → 9.0 | 343 → 385 | 2.8 M → 2.9 M | 12 k → 16 k |
| codex sol std-high bugfix | 12 → 10 | 1.00 → 1.00 | 91 → 45 | 23.9 → 8.2 | 1,899 → 772 | 13.5 M → 4.1 M | 50 k → 24 k |
| codex sol std-high feature | 7 → 13 | 0.43 → 0.69 | 46 → 50 | 10.8 → 12.5 | 930 → 1,018 | 4.3 M → 6.0 M | 15 k → 34 k |

`compare` halts R against B on: Claude std-high bugfix median duration +63%;
integration-repair share +24 pp; churn +22 pp. Each is explained without any epic
change: once routine work moved to Codex the Claude bugfix cohort shrank to five
larger tasks (151 calls each against 65), and the repair share and churn are the
slot-reset loop above. Codex
bugfix halved its calls and polls with no code change. Task mix moves these numbers
as much as any change here will, so a cohort difference is evidence only together
with the route shift, quota state and incident log of the same window.

## Uncertainty and confounders

- Task difficulty is not observable beyond kind and class; cohorts of 5–45 attempts
  carry wide intervals (92.7 estimated a per-task cost CV of 1.42).
- Provider mix follows quota resets and pressure; compare within a harness/model.
- Cache reads dominate the event count but are priced and rate-limited differently
  from uncached input and output; no sum here is a quota percentage. Codex's
  `rate_limits.primary.used_percent` is the only direct quota reading, it covers the
  whole account, and it resets.
- Attempts are selected by claim time; a long attempt's later calls count in its
  window. Pre-claim polling and start-up belong to the session's first attempt.
- Integration incidents (slot resets, repair loops) change churn and latency
  independently of worker prompts.

## Staged rollout

Each stage starts only after the previous one passed. **Before each stage**, export a
"before" window of at least 24 h on the code and configuration about to change. **To
pass**, after at least 24 h export the "after" window and run `compare`; every
matched cohort with at least five attempts per window must clear the halt
conditions. Any halt stops expansion until its cause is named in the task or
incident record; a halt caused by the stage is rolled back. The comparison is
advisory evidence; it never changes configuration.

| # | Stage | Activation | Pass check | Rollback |
|---|---|---|---|---|
| 0 | Deliver | Integration owner delivers the epic to `main`. Operator sets `sessions.worker_context_compact_tokens: 0` **before** restarting, runs `aq db upgrade` (`a00000000055`), then `aq restart --no-dashboard`. | Daemon healthy; `aq db current` = head; no new `tick failed` or transcript-watcher errors | Previous release; `alembic downgrade` drops only the call-progress table (ledger rows survive) |
| 1 | Accounting (92.1) | Live at stage 0 | Claude ledger/transcript ratio falls from ~1.75–1.95 to the Codex calibration (0.9–1.05) per category on attempts started after the restart | As stage 0 |
| 2 | Startup, waits, handoff guidance (92.5, 92.2, 92.3 guidance) | Live for sessions launched after stage 0 | Polls/attempt falls and durable waits rise in matched cohorts; no halt. The prime reduction (~1.3 k tokens on each later call) is below the per-call context noise; its evidence is the byte measurement above | Revert the delivered commits through normal integration |
| 3 | Worker compaction (92.3 default) | Only after 92.9 is delivered **and** `{"fast-jev-compaction@fast-jev-compaction": false}` is configured for workers: remove the `0` override (or set 160000) | Compactions appear only above 160 k; context p90 and cache read per attempt fall in Claude cohorts; pass rate, repair share and durations clear the halts; no task re-asks for state the checkpoint should have preserved | Set `worker_context_compact_tokens: 0` (next launches); remove the plugin override to restore fast-jev |
| 4 | Routing (92.4, 92.6) | Operator activates the reviewed `default-assignment-routing` bundle per [routine-routing-preference](../guides/routine-routing-preference.md); record the previous active hash | Route distribution moves routine kinds to Codex when Codex has headroom; Codex cohorts keep pass rate; no provider-hold or quota spikes | `aq playbook activate` the previously active hash |

Stage 3 is the largest lever: about 45% of Claude calls run at ≥ 160 k context. It is
also the largest quality risk (summaries lose detail; the fast-jev judge measured by
92.7 is degenerate), which is why it is separated from delivery. On every install
whose operator has not set the override, delivering 92.3 enables 160 k compaction
for all new worker launches.

### Halt thresholds (defaults; `report --threshold NAME=VALUE`)

| Condition | Default |
|---|---|
| Matched cohort pass-rate drop | ≥ 10 pp |
| Matched cohort median decided-attempt duration | ≥ +50% |
| Integration-repair share of tasks | ≥ +10 pp |
| No-outcome churn share of attempts | ≥ +10 pp |
| Live attempt open longer than | 4 h (any) |
| Decided attempts longer than 4 h | any increase |
| Minimum attempts per cohort per window | 5 |

## Remaining work

1. Deliver the epic (integration owner) after `azure-vault-92.9` completes; the
   operator applies stage 0 with compaction deferred.
2. Run the stage 1–4 comparisons above and record each window's export, report
   and verdict beside this note; then claim any measured impact.
3. Decide on a reconciliation for historical inflated Claude ledger rows (92.1's
   3,962 dry-run proposals); nothing has been applied.
4. Explain the `slot_reset_failed` loop on integration-repair claims (2026-10-01/02)
   before using repair share or churn as a gate.
5. OpenCode attempts have no transcript reader in this tool.
6. Codex `rate_limits` quota readings show resets within a day; confirm account
   scope before using them as a quota signal.
