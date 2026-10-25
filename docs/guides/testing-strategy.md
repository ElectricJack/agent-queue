# Testing strategy

Status: draft for the A2 interview and review (`bright-ridge-12.2`). Jack's
Q1–Q3 answers are recorded; the remaining interview and strategy acceptance
are pending. This draft records the existing approved obligations; it does not
authorize a check removal or suite
cutover. H1 BUILD authoring requires A2 acceptance.

## Authority and purpose

The implementation contract is `rev-bold-summit` r1, canonical vault source
`projects/agent-queue/specs/2026-10-23-bazel-for-aq-hermetic-tests-parity-measured-cutover-and-trai.md`,
body SHA-256 `a8cd2d2db547372d36013d749abb519eb5ceea33ea4bd3873c531e115e753287`.
Approval is recorded by decision `ask-decision-e8b74e1ba69547db8d5a748d8b0b33e1`.
The task's approval reconciliation records M1–M5 deferral and transfer of
smart-batch B/C into that contract. Matter work has no AQ completion trigger.

The objective is confidence in changes before they reach frequently deployed
`dev`, with less redundant execution and lower measured delivery latency. Keep
every current pre-merge obligation and verify the exact assembled candidate.
Use valid cached actions to avoid repeated execution; an earlier head's green
status never certifies a different candidate.

The worker smart-selection contract (`rev-sharp-crest` r3, S1–S12) remains
independent: shadow defaults, static/mandatory safeguards, testmon experiment,
resource admission and its evidence/promotion gates remain in force. Worker
recommendations do not prove train verification. See
[smart test selection](smart-test-selection.md).

## Evidence consumed

A1's generator/candidate commit is
`ef21008f6835f09def984c27ecf6286c5afb0120`. The retained artifact index is
`projects/agent-queue/reports/bazel-a1-bright-ridge-12.1/README.md` in the
operator vault; artifacts live under
`~/.agent-queue/artifacts/bazel/bright-ridge-12.1/`. Supervisor receipt
`msg-4f011c2d9c1f49519243dc1fde2bc196` identifies the completed A1 task and
its artifact receipt `msg-cb71d7501b174fb0b12d461c5134dd2e`.

| Artifact | SHA-256 of uncompressed bytes |
|---|---|
| `bazel-inventory.json.gz` | `7351ab677b306c06dc09694c3e28d3ef35ebbdd3a047bb702812fe59abfc3e58` |
| `bazel-collection.json.gz` | `d8d4b898aefdfbbe1ef059d2e232593c1e4e81370abf6fc73c26d2326a60c6e7` |
| `bazel-history.json` | `c5132fa71e1944fed79e546a44851ff4efd95010c919afbe9b5be0308398c3cd` |

The consumed collection contains 37,857 nodes, 1,000 modules and 2,529 fixture
definitions. The inventory records 16 Python workflow obligations, four stateful
E2E groups and two dashboard typecheck/build commands; its initial total target
estimate is 1,005. These are a dated collection and an estimate, not a promised
final graph or the number of checks every worker should execute.

Historical aggregate timings cover 17,164 nodes; all 37,857 lack per-phase
setup/call/teardown observations. One recorded aggregate exceeds 60 seconds.
These observations cannot establish that other tests meet the complete budget.
Collect missing phases and shared-fixture attribution before budget acceptance.

Dependency analysis found PostgreSQL evidence for 35,801 nodes, git evidence
for 4,883 and tmux evidence for 48. Remaining classifications are unknown,
not proven absence of dependencies. The current global PG preflight is part of
the observed dependency picture. Pure targets require positive evidence after
the planned fixture-admission work. There are 91 collected nodes not selected
by the inventoried workflow expressions; retain and audit those identities
before deciding whether they are deliberately opt-in or an obligation gap.

The CI snapshot includes 300 completed workflow runs and one active run:
128 cancelled, 87 failed with unknown cause, 20 skipped and 65 successful.
These are workflow-run outcomes, not test failure or flake rates. No failure
has sufficient classification evidence here to justify removing coverage.

The latest recorded full-suite baseline is the operator-vault note
`projects/agent-queue/notes/full-suite-baseline-2026-09-22.md`, SHA-256
`92eb9d6b8511ef78718233310e679fe1cfb547ab4d9db4aaae8a9d80b316b369`.
It binds main `35d9605eab99480b29fd54598eb840b2a614ee09` and Tests run
`35806218584`: 45 failures, 17,587 passes, 77 skips and one xfail in the default
job; CLI inventory also failed, while migration/slow and PG integration passed.
Use that note's exact node list and assess source relevance for a focused
failure. Its age neither proves a current failure is pre-existing nor permits
weakening the assertion. Do not capture a replacement full-suite baseline as
part of an ordinary worker task.

The live 14-day delivery baseline and continuous samples are pending under
`bright-pinnacle-59` (A1-OPS). No controlled speedup, landing latency, billed
compute, red-dev rate or worker wait improvement is established by A1. All
performance acceptance and retirement decisions needing those comparisons stay
gated on that evidence.

## Interview and decision record

Ask Jack one question per exchange. Preserve the exact question, verified reply
reference, date and resulting strategy decision. An unanswered question or an
agent recommendation is not Jack's acceptance.

| Exchange | Question | Evidence and disposition |
|---|---|---|
| Q1 — confidence | When a change is green, what do you need that green result to tell you before it lands on the frequently automatically deployed dev branch? | Jack's verified answer on 2026-10-24: “If it’s green and it’s an epic (complete feature) I want it merged, and that should be all tasks at the root level”. Discord input `cinput-6970a5ab` at 19:54Z; supervisor relay `msg-80ee66dc374447e98cca5fea1c8073ab`. Identical confirmation `cinput-6d17114e` at 20:46Z, relayed in `msg-6a5de3917fd145eeb213e87a315edff2`. Treat these as one answer. |
| Q2 — blocking failures | Which failures must block a root epic from merging to dev, even when its focused feature tests pass? | Jack's verified answer on 2026-10-24 at 22:19:25Z: “Only failures in areas the epic touches”. Discord input `cinput-c73d3c964604480fb20caaa550afda37`, conversation `conv-7b812291-a40d-465b-9a86-21da0d9c3a7e`; supervisor relay `msg-72a475a9aa044072b5f23e7ddd89ee10` (sequence 136547), backed by task comment `comment-6b37662673c54f67aa91a59b5f839b85`. |
| Q3 — affected-area boundary | Should "areas the epic touches" include downstream behavior that depends on changed code, even when that area’s files are unchanged? | Jack's verified answer on 2026-10-24 at 22:57:33Z: “I think yes, downstream that depends on code the epic changed should be tested”. Discord input `cinput-fff40414137f47f0bbf065677974744b`, conversation `conv-7b812291-a40d-465b-9a86-21da0d9c3a7e`, actor `human:discord:395982012789293056`; supervisor relay `msg-8d6ea6b7bd6c45619b9d99fba16ceb81`. Duplicate relay `msg-d54da5f71f254256b2941a331d1971cf` is the same answer. |
| Q4 — redundant coverage | What evidence would make you comfortable consolidating tests that appear to cover the same behavior? | Requested once through supervisor in `msg-e982a7a22cf54f809cf6bf7766d61ef1`, thread `bright-ridge-12.2-interview-q4`; verified answer pending. |

Q1 establishes Jack's desired delivery outcome: merge a green, complete feature
at the root level. It does not yet specify which failures must block that green
result, authorize a change to task hierarchy or merge policy, or accept this
strategy. The earlier supervisor status
`msg-8c59e8fbec3e4a83b91dd0bc0ae20d14` only confirmed relay; the verified
human inputs above supply the answer.

Q2 records Jack's preference to block on failures in areas the epic touches.
Q3 includes downstream behavior that depends on changed code within that
boundary, even when its own files are unchanged. Assess dependency impact as
well as edited paths when reviewing affected-area evidence; unknown impact
requires conservative verification. Retain the current required gates pending
the reviewed implementation and its evidence. These answers alone do not
establish which observed failures are unrelated or supply replacement coverage
evidence.

Continue with redundant assertions and the risk of frequent automatic
deployment, adapting each next question to Jack's
answer. Record the accepted strategy review's ID, revision, content hash and
decision receipt here before claiming A2 acceptance. Moving protection past
deployment needs a separate approved policy amendment even if an interview
answer recommends it.

## Required protection and execution

Until an individually approved replacement passes parity, retain the existing
workflow matrix and its marker expressions, exclusions and intentional overlap:

| Obligation | Retained verification |
|---|---|
| CLI command surface | CLI inventory and conformance arm, plus the separate CLI startup budget |
| Default Python | All eight current timing-balanced splits with the current default deselections |
| Database and longer-running behavior | Explicit migration/slow and PostgreSQL integration/performance arms |
| Stateful system behavior | All four E2E groups: claims, CLI, graphs and failover; preserve scenario prerequisites and resets |
| Dashboard | Existing typecheck and build, including generated TS-client inputs |
| Generated artifacts and publication | Existing generation/drift obligations, required candidate checks and trusted attestations bound to the exact candidate |
| Additional developer checks | Keep opt-in tmux/performance paths and dashboard test commands available; their existence does not make them new required CI arms |

Changing which failures block is a reviewed policy decision. A known baseline
failure does not justify disabling a required check. Missing, cancelled, skipped
or untrusted verification does not certify a candidate. Unknown failures need
investigation before assigning product, flake, infrastructure or timeout blame.

Workers run focused checks and one relevant area check through `aq test`, with
the existing box-wide slots, worker caps and marker defaults. Explicit marker
arms and frontend obligations remain separate. Whole-suite execution belongs to
CI or a task explicitly about the suite. Do not raise `-n`. See
[testing](../contributing/testing.md) and [resource gating](resource-gating.md).

Retain pytest/npm developer paths throughout Linux-first Bazel coexistence.
Shared-box Bazel execution must use the admitted `aq test` backend once that
backend exists; direct Bazel is for isolated CI runners. Initial caching stays
local disk/actions cache, with trusted writers separated from untrusted inputs.
No remote cache/execution, paid runner or post-merge testing tier is selected.

## Replacement coverage ledger

No test, assertion, required check or execution arm is removed by A2. There are
zero proposed replacement/removal rows at this stage. The obligation table
above records what remains; an empty removal ledger grants no future waiver.

Before removing or consolidating coverage, append a row with all of:

| Required field | Evidence needed |
|---|---|
| Original protection | Exact former node ID, assertion/behavior, fixtures and workflow arm |
| Replacement | Exact replacement node/assertion and required pre-merge arm; explain every retained behavior and boundary |
| Former failing condition | Reproducer, historical bug or controlled mutation with its exact source revision |
| Failing-condition proof | Show the replacement fails on that condition and passes with the fix; retain commands, identities and results |
| Independence and timing | Account for integration/reset/concurrency coverage and full setup/call/teardown cost; a lower-level test must prove the same boundary |
| Decision | Review revision/hash, acceptance reference, owner and date; incomplete rows prohibit removal |

Similar names, matching pass counts, mocked lower layers or a currently red
check do not establish redundancy. Splitting/sharding must account for every
expected node exactly once and preserve independent fixture/scenario state.
Moving protection beyond deployment also requires the separate amendment above.

## Bazel acceptance and delivery evidence

Mirror obligations before changing required gates. Each suite needs 20
consecutive exact-dev-commit comparisons with identical full node/outcome
multisets (including collection/setup errors, skips, xfail/xpass strictness and
all shards). Missing samples or differences reset the counter. JUnit needs the
outcome sidecar to preserve distinctions. Identically failing backends do not
make a required check green.

Unchanged repeats must serve 100% of eligible test results from cache, including
a repeat with disk cache retained but server/output state discarded. Resolve
graph under-declaration and flaky/no-cache quarantine before suite cutover;
quarantine stays visible and cannot pass that gate. No hidden retry turns red
into green. Prove fixture, prompt, migration, generated-input, rename/deletion
and package-initializer invalidation rather than trusting a small target set.

Measure every test's setup/call/teardown against 60 seconds, including charged
shared setup. Target wall time, bootstrap and cleanup included, must be at most
300 seconds. Exceeding a deadline fails and cleans up the target. Controlled
budget measurements use the quiet serial performance path; historical totals
do not substitute for these measurements.

Switch suites in the approved order: dashboard, CLI, default Python, PostgreSQL,
migration/slow, then E2E. Keep CLI startup and generated-artifact gates required
throughout. Required checks, train reporting, trusted check versions and
attestation policy change together only after exact-candidate evidence exists.
Missing or untrusted attestations retain the full-test fallback. Installed
policy/protection activation requires explicit operator receipts.

Keep each legacy arm dispatchable for at least two weeks after its switch.
Compare a week's measurements with the 14-day baseline, with at least 20
comparable landings; insufficient samples extend the hold. Report p50/p90 and
missing denominators for push-to-green (queue/run separated), first task commit
to dev, worker time blocked on checks (overlaps per worker counted once),
rebase/rerun events and billed minutes per landing. Deduplicate red-dev incidents
by root cause; include train starts/lands/aborts, ejections, refreshes,
evaluated/eligible candidates and green mergeable PRs waiting over two hours.
Bind observations to task/head, base, candidate revision, exact commit,
workflow attempt and suite version; keep failed/cancelled/queued observations.

Roll back the suite's required checks and attestation version if push-to-green
does not improve, rebases/reruns increase or missing verification causes a
red-dev incident. Queue regressions count even if test execution gets faster.
Retire a legacy arm only after its fallback window, successful measured
comparison, rollback rehearsal and operator acknowledgment. Retiring duplicate
CI execution does not retire pytest, worker smart selection or testmon.
