# Git-first backlog settlement preparation — 2026-10-05

Task: `bright-rapids-84.13`. **Live settlement acceptance is unmet.** This
worker prepared the operator checklist and reproducible replay evidence. No
operator capture of the current backlog, configured active protocol/canary or
complete live dispatch audit was supplied. No unheld task was inspected or
changed. The historical incidents may already have moved; this report does not
claim that they are still stuck.

The approved sources are the vault spec
`projects/agent-queue/specs/2026-10-04-git-first-integration-train.md`
(review `rev-fair-impact`) and plan
`projects/agent-queue/plans/2026-10-04-git-first-integration-plan-2.md`
(review `rev-keen-stone`), especially plan sections 4, 6 and 7.

## Artifacts

- [Operator checklist](../../guides/git-first-backlog-settlement.md): capture
  each concrete instance, observe the configured train, preserve checks/tree
  review/leases/holds and attach a complete structured dispatch audit.
- [Status and command-audit report](status.json): per-family missing inputs,
  separate reconstructed and live audit verdicts, explicit removed command IDs
  and their digest. Unknown live counts and dispositions are `null`.
- [Replay evidence](replay-evidence.json): declared assertions, resulting remote
  OIDs, task/batch counts and command IDs for every emitted reconstructed case.

## Source assembly and local repairs

The supervisor's task comment instructed this worker to merge current main and
the completed shared-train/replay branches. The assembled inputs are:

| Input | OID |
|---|---|
| `origin/main` | `bb674ede191fb6e150f50821120be1728d7f4342` |
| `origin/aq/bright-rapids-84.11` — shared train | `beae1acb59449a703800bbe72e840cbfa8898c61` |
| `origin/aq/bright-rapids-84.12` — replay | `32b22c78a532a57b2ca04a2eea472dc1202be1b6` |
| Assembled merge base for these checks | `adb1ff36515208fdc8d0a8d8986806e65ee1d228` |

The selection catalogue had a generated-file merge conflict. It was regenerated
from the merged sources with `scripts/regenerate-generated.sh`, then committed
with the merge. No generated file was hand-merged.

The incoming replay guard expected handlers that current main had retired and
classified `integration_reevaluate_repair` as absent although it remains. The
first two focused runs reported 41 setup errors each. The explicit
`RETIRED_CONTROLS` inventory now matches the assembled checkout; the full
`REMOVED_CONTROLS` set and both guard assertions remain. The positive guard
probe uses the present `integration_record_noop` handler, while the reference
command guard also rejects the retired `integration_adopt` ID. The final run
below checks that these guards actually fire.

The first area run passed 104 tests and failed one selection ownership check
because the incoming `tests/fixtures/integration_replay/README.md` had no rule.
Added `tests/fixtures/integration_replay/**` ownership to `epic-trains` and
regenerated the catalogue. These were assembly issues, not failures named by
the recorded 2026-09-22 full-suite baseline; no assertion was weakened or
historical gap turned into a pass.

`aq prime` returned `record.forbidden`. The local claim file and the successful
own-task detail/comments reads supplied task ownership and instructions. The
task's approved spec/plan were read from their repository-documented vault
location; no access guard was bypassed.

## Immediate replay and area evidence

The focused command was:

```bash
AQ_REPLAY_EVIDENCE=/tmp/aq-bright-rapids-84.13-replay \
  POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/test_integration_replay.py -q -rs --tb=short
```

Result: **35 passed, 6 skipped in 36.41 seconds**. Every skip is an explicit
missing historical capture. The suite emitted 32 reconstructed scenario traces
covering all six named stalls, whole-source/squash/reopen checks, check reruns,
changed-tree review, moving targets, nested epics, generated/migration conflicts,
crash/restart/push boundaries, lost candidate refs, aborted inputs, expired
leases, stale/non-holder pushes, concurrent visits, holds/rejection and slow or
unavailable CI while another target advances.

The suite drives reference train seams over real disposable Git remotes. Its
checks are deterministic supplied test facts. It does not replay the production
`IntegrationTrain` end to end or query hosted CI. Production train behavior was
checked separately with:

```bash
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/test_integration_train.py tests/test_integration_train_sources.py \
  tests/test_integration_root_scenarios.py tests/test_selection_catalogue.py -q
```

That run passed all train/source/root scenario tests; its one selection failure
is described above. After the ownership fix,
`aq test tests/test_selection_catalogue.py tests/test_docs_sync.py -q --tb=short`
passed all 44 tests in 15.83 seconds. Ruff, the generated-file drift check and
`git diff --cached --check` passed. The offline JSON check confirmed all trace
counts, the removed-ID digest and explicit unmet fields. Exact commands and
results are recorded in `status.json` and the task's closing evidence. Tests use a disposable
maintenance server and owned scratch databases. No full suite, live migration,
daemon restart or live integration operation was run by this worker.

## Structured replay command audit

All 32 emitted case traces contain **253 commands**. Comparing exact command
IDs against all **35 removed IDs** yields **zero matches**. `status.json`
publishes the full set, sorted per-command counts and the SHA-256 of the sorted
ID list encoded as newline-terminated UTF-8 lines. Counts include ordinary
repair allocation and task completion in the reference seams.

This verdict covers the emitted reconstructed traces only. The intentional
guard-probe test invokes a removed handler locally and expects rejection; it
is outside those settlement traces. No live settlement window or command
export was available, so `operator_live_audit.removed_invocations` is `null`
and its verdict is `unmet`, rather than a fabricated zero.

## Required operator inputs still missing

| Recorded family/item | Historical capture | Current settlement |
|---|---|---|
| `crisp-horizon-90` | GAP | Source/children, target, exact checks/tree review and task transition unavailable |
| `vivid-quest-44` | GAP | Start/current repair OIDs, lease, checks, publication and task transition unavailable |
| Epic verifiers | GAP | Current instances, required tree verdicts/checks and transitions unavailable |
| Stale branch owners | GAP | Current instances, ref leases/incarnations/expiry and publication unavailable |
| `brisk-beacon-64` | GAP | Its exact green/check/review/hold facts, target and transition unavailable |
| `swift-delta-90` | GAP | Its own children/head/check/review/hold facts, target and transition unavailable |

No current real conflict or explicit hold can be confirmed without those
captures. Both categories remain unknown, rather than reported empty. The
operator supplies a per-instance inventory even if every old incident is
already resolved, with evidence of the prior settlement.

Complete the checklist on the **existing settlement node**, attach sanitized
captures and replay them, and record live mode/canary, before/after OIDs,
required check and tree verdicts, ordinary task transitions and dispatch-audit
coverage. Until then the required live settlement and historical replay cases
remain unmet. Ordinary task filing, this published report and reconstructed
passes cannot substitute for them. The separate seven-day observation and
independent review remain epic obligations.
