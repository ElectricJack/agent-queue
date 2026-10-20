# First context-thrash continuation behind a retained writer

Task: `stark-quest-41`. Investigation and implementation on its assigned branch;
no operator database, live incident, hold, deployment or unrelated task changed.

## Finding and scope

The first guarded failure records count 1 while the task remains IN_PROGRESS.
`SessionReconciler._recover_guarded_failure` asks production pool teardown for
READY. `ClaimQueryMixin._release_claim_on` refuses release when an attached or
pending integration owner protects the writer's workspace. Teardown confirms
the process stopped and retains the session's active claim/workspace bindings.
Previously the caller still wrote “Retrying with a bounded continuation note”
and emitted `task.restarted`. The orphan sweep then saw IN_PROGRESS with a
stopped session and wrote BLOCKED / `session_not_live`.

The original execution end remained `context_thrash`. The supervisor's generic
operational recovery guard does not admit that end, independently of remaining
retry budgets. Owner release preserves work and removes resource attachments;
it does not change execution-end eligibility or authorize clearing a hold.

The old sequence is reproduced explicitly in PostgreSQL by
`test_retained_first_failure_would_reach_generic_orphan_attention`. Production
teardown tests exercise attached, pending and released owners, first and second
incidents, and unavailable stop proof. A separate test races disposition with
real `OwnerRecovery` on a real Git worktree and bare remote.

The historical snapshots and later CI owner-release comments are consistent
with this sequence. They do not contain the original transactional teardown
retention receipt or a complete transition timeline for every original worker.
This is a proven reproducible lifecycle seam and a correction to its false
retry report, not a claim that each historical owner was inspected live or that
any original incident has been recovered.

## Resulting behavior

After pool teardown, `pool_recovery_disposition` observes the actual result
under the task epoch, session instance, last claim epoch, route and exact
failure-record fences. A confirmed stopped writer still bound to its failed
claim becomes BLOCKED / `session_context_thrash_handoff_deferred` on incident 1.
Its claim, branch owner, workspace, continuation, retry count and streak remain
intact. It reports deferred continuation and queues supervisor investigation.
The orphan sweep cannot replace that diagnosed cause with `session_not_live`.
Incident 2 retains `session_context_thrash` and stops automatic retry as before.

A released, unassigned READY task reports its actual bounded retry. An
unconfirmed stop reports the uncertainty and retains resources; it does not
invent a stopped-writer proof or emit a retry. Duplicate disposition records
are silent. Late claims, session instances, manual pause/block decisions and
route changes supersede the old observation. The resume/stall metadata resets
are now transactional and fenced too. Late pool teardown checks its observed
session instance and claim before stopping or retiring a reused worker.

Failure/disposition transactions take the project hierarchy lock before the
session and task rows, matching the owner-recovery release order. The receipt
stores historical claim, branch and workspace pointers. Existing owner and
release-audit records remain canonical for fences and source proof.

## Recorded incidents and bounded operator recipe

These are historical observations from the supervisor's private evidence,
not fresh permissions or current liveness claims. All three recorded explicit
supervisor holds remain effective.

| Task | Failed session / epoch | Instance | Recorded incident | Owner/source observation |
|---|---|---|---|---|
| `nimble-forge-21` | `ae133878-f30d-4c00-934a-ed529cf50f75` / 1 | `4692d53a013b42069531cb91e5fb7141` | `recovery-c02a7b5bfcfa8742c397bcd402f04f57` | stopped, desired stopped, active binding retained; source SHA and current owner fence require fresh proof |
| `solid-meadow-84` | `35d83dc5-73a5-490e-bdb4-e4dfb96b7a2d` / 1 | `eda4768bbe6c44c39e28f89576fec6a0` | `recovery-472e4cbd0dd2e6e070b04f16458f3ee8` | owner `092a6f5a-b2fc-43d7-9599-f40d8f94bb11` reported preserved and released at `9077ddec0709327bfa9feea8f2f5888fa66b9f85` |
| `fair-zenith-45` | `869479f9-b028-4107-ac27-13d0ded9d5f5` / 1 | `b4d2242313354e10a29f2a814cfb80bd` | `recovery-853e4498239cd232aad5b416458bbf7b` | owner `15ba0a3e-4333-4849-bc51-5811d601455c` reported released; release audit/source SHA require fresh readback |

Nimble's recorded attempt is `2ac6737f724546d49ea2f4494c269238`; its
branch is `aq/epic/a-supervisor-recovery-hold-cannot-be-released-after-the`.
Solid's branch is `aq/epic/repair-source-ci-solid-bridge-15-9c61fe021e3d`;
Fair's is `aq/epic/repair-source-ci-steady-forge-22-0d1f4d752d77`.

The owning supervisor/operator must first read the current incident, its
decision timestamp, task epoch, latest attempt and exact old session instance.
Confirm runtime termination through the configured provider, not only a
stored stopped state. Check the canonical owner row/fence, workspace claim,
branch origin/base and local/remote source tip, and any active, retired or
human-required integration operation. A live operation keeps authority over
its delegate; generic recovery must not take that seat.

For Nimble, if fresh evidence still shows a stranded attached owner, the
supported preview is:

```sh
aq integration release-owner --task-id nimble-forge-21 --dry-run
```

Applying the same command without `--dry-run` is an explicit operator action
after examining that preview. It must return its own exact stop, source,
preservation and released-fence receipt. An unsuccessful or changed proof
leaves the attachment retained. No Nimble release was executed here.

For Solid, read the existing recovery audit for owner `092a6f5a-…` and confirm
that the preserved branch still contains `9077ddec…`, matches the canonical
lineage and has no newer writer. For Fair, read the existing audit for owner
`15ba0a3e-…` and obtain the exact local/remote SHA; the word “released” alone
does not establish a source. Do not repeat a release against a different or
new owner. If a binding remains, use the same supported release-owner preview
for the exact task/owner and let its guards decide eligibility.

After release, all three still have a bounded control gap: generic recovery
rejects `context_thrash`, and the audited operator-handoff path currently
qualifies only `productive_death` or `drained` execution ends. An ordinary
`restart_task` clears the streak, so it is not an equivalent way to preserve
this bounded continuation. `resume` or metadata edits must not bypass those
guards. Keep the current holds and report the gap. If a future qualified
recovery path is approved, release only the freshly read exact hold through
`aq task recover --decision retry --incident-id <current-id>
--expected-hold-at <current-decided-at> --task-id <task> --reason <diagnosis>`;
its current guard refusal remains authoritative until then. No budget, cause,
route or source evidence may be reset to manufacture eligibility.

## Native transcript refill

Nimble's recorded exact-session transcript contains the synthetic harness
diagnostic: context refilled within three turns of compact, three consecutive
cycles. Request input (input plus cache creation plus cache read) was already
55,037 tokens before the prime result. Subsequent measured requests were
56,393, 59,705 and 58,097 tokens. Prime returned 17,189 characters; the
debugging skill added 9,435; subsequent targeted source results were 1,372,
5,798 and 11,804 characters. Compact continuation summaries grew from 5,594
to 8,580 to 10,108 characters. The diagnosis and recorded measurements agree
that repeated compact did not create sufficient headroom for these next turns.

This excerpt does not identify the effective native threshold or partition
the static system/plugin context from the retained history. It does not prove
that a single file, a model context limit or the prime alone caused the failure.
The initial large context predates the shown prime response. Changing native
caps, routes or providers would be an unsupported inference from these data.

The new deferred path prevents a blind unchanged retry when teardown did not
release the writer. The saved continuation remains bounded and contains
checkout/progress observations rather than transcripts. A qualified successor
should use that checkpoint, avoid rereading unchanged global skill/catalogue
content, page required canonical task fields and inspect targeted source ranges;
it must still load required role/rules, deliverables and completion controls.
No prime-overflow implementation or acceptance criterion from the separate
paused `grand-harbor-65` task is claimed here.

Evidence inputs: `/tmp/aq-messageba94-current.json`,
`/tmp/aq-messageba94-diagnostics.json`, `/tmp/aq-messageba94-holds.json` and
`/tmp/aq-messageba94-final-blockers.json`. The supervisor's existing audit
receipt identifies project note `notes/patrols/message-1791933148.json`.
Private transcript bodies are not copied into this report.

## Validation

The final focused recovery checks passed 25 tests
(`/tmp/stark-quest-41-focused-latest.log`). The area check ran 1,154 tests,
with 1,150 passes and four failures (`/tmp/stark-quest-41-area.log`). Two
pool-retention fixtures passed a pre-claim launch snapshot to teardown; they
now refresh the activated session before testing retention. Their original
assertions remain intact. The complete pool-area rerun passed all 102 tests
(`/tmp/stark-quest-41-pool-final.log`).

The other two failures are
`test_integration_owned_surface_is_inventoried` and
`test_no_new_metadata_or_operation_event_journal`. Source inventory at the
starting commit `55af0ca8c086fbe19f6938fac578f77303c232ff` confirms the same
three unclassified definitions in `claim_queries.py` / `worktree_manager.py`
and six unreviewed writers in `claim_commands.py`
(`/tmp/stark-quest-41-head-inventory.json`). Those sources and inherited
classifications are unchanged. This change classifies its own fenced resume-key
deletion without changing any pinned baseline, cap or allowance. Ruff and
`git diff --check` passed.

The final-source disposable `scripts/e2e-env.sh --reset` followed by
`scripts/e2e-smoke.sh` passed all 20 scenarios and stopped its daemon cleanly
(`/tmp/stark-quest-41-e2e-final.log`). This includes real train delivery of
`solid-dune@2ed536b01a62ae0ecfe10ca0b8bb18b6f1eac9b3` to its disposable
remote `main@c84fa76eecd72b3295d67ed88e2335b40e2195b0`. No real LLM,
operator daemon or external messaging was exercised. Disposable swarm uses
`/tmp/aq-e2e-stark-quest-41`, API port 8196 and database
`aq_e2e_stark_quest_41` on the separate test PostgreSQL service at port 5534.
Only observed passing runs count as evidence; unavailable or refused checks
do not count as passes.
