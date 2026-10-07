# Exact completion provenance in Git

Implements shared-contract items 4–6 of the approved
`projects/agent-queue/plans/2026-09-27-development-publisher-delivery-truth.md`.
The development publisher and the train's Git delivery checks share this primitive.
Every delivery consumer reads it through `src/integration/delivery_truth.py`; the legacy
receipt table and every compatibility locator were retired by revision
`a00000000038` (see [Legacy migration primitive](#legacy-migration-primitive)).

## Identity and retention

For a leaf, the immutable generation is the existing `task_completion_records.id`. A
completion binds project, repository, task, generation and the exact full final
source OID. `claim_epoch` supplements that identity; it does not replace it.

A verified integration parent has no leaf close row. Its generation is
`parent:<integration_parent_verifications.id>`, backed by the durable
`integration_parent_operation_completions` binding. The shared delivery reader
accepts that identity only for a COMPLETED task whose designated project/repository,
branch, current episode, checkpoint generation/head and current verification match
the completed parent operation and its successful verification. The episode's initial
generation may precede the checkpoint generation. A reopened task, newer close,
unfinished operation or changed binding has unknown delivery, even if an older Git
completion ref remains. Guarded consumers recheck this entire binding.

That durable parent identity locates the exact source without a provenance migration;
Git ancestry of the full source still decides containment on the requested target.
Operator adoption retains this same identity in the existing provenance namespace
and uses ordinary explicit replacement records for accepted equivalents. Adoption
rechecks the parent binding and repository/target before writing. It creates no leaf
completion, train delivery receipt or CI attestation. Root scheduling uses the same
delivery answer and requires the checkpoint to name the exact source.

Ordinary `aq git commit` / `aq git commit-changes` messages carry `AQ-Task`.
`GitManager.acommit_all` also reads the worktree claim file. For direct shell
commits, use `git commit --trailer 'AQ-Task: <task-id>'`. Existing hooks run as
configured. A trailer identifies authorship, never complete delivery.

Every merge an integrator makes carries one `AQ-Source: <task-id>@<full-source-sha>`
trailer per applied member, written only after that member's complete source was
applied (`src/integration/source_trailer.py`; the writers are
`candidates._message`, `promotion._message`, `GitOperations.merge_members` and
`development.merge_member`). It names the whole generation, never one commit of a
multi-commit task, and never rewrites or duplicates a trailer, so the pre-existing
`AQ-Task`, `Reviewed-head`, `Review-evidence`, `AQ-Receipt` and `AQ-Intent` trailers
survive until their readers retire. A fast-forward or an already-contained source
creates no merge commit to amend, and its source is an ancestor of the target
instead. A reader matches the whole identity — task id *and* full OID — on a commit
reachable from the fetched target: an abbreviated SHA, a substring, one commit of a
multi-commit source and the previous generation of a reopened task are each a
different identity and prove nothing.

The namespace is `refs/heads/aq-provenance/completions/<subject-hash>/<generation-hash>`.
The subject hash covers project/repository/task; the generation hash covers the
completion ID. The metadata commit has exactly one parent (the final source),
the source's tree, and a bounded JSON body:

```json
{"version":1,"kind":"completion","identity":{"project_id":"p","repository_id":"r","task_id":"t","generation":"completion-id"},"source_oid":"<40-char-oid>","claim_epoch":1,"artifact":true}
```

Metadata objects are deterministic, immutable and independently published with
an exact absent-ref lease. Conflicting bindings fail closed. They retain source
objects through archive and deletion of `aq/<task-id>`; cleanup must preserve
the `aq-provenance/` namespace. Normal clone/fetch head refspecs fetch the evidence.
No DB delivery flag or new Git mapping is introduced.

Close allocates its completion ID before verification, verifies the clean,
exactly pushed final task source, publishes the provenance ref, verifies that
exact remote OID, and rechecks the source before terminal transition. It refuses
an explicit `--commit` naming an older or abbreviated commit. A publication error
or changed source retains the task/session/workspace for retry. Already pushed
history is never amended. A final head equal to the recorded origin base records
an empty completion commit list while retaining that exact head. A proven empty,
explicitly code-free Git source has `artifact:false`. Explicit vault-only outcomes
also retain their clean owned source slot at its exact recorded origin base,
without requiring a published task branch. Missing base, changed source or failed
provenance publication refuses close with the claim and workspace retained.
Branchless and non-Git outcomes keep their existing completion behavior.

For an unheld Git root, `aq integration record-root-noop TASK_ID` previews an
explicit no-artifact completion. A local operator or a live named supervisor of
the owning project can apply it with the exact previewed `--head` and a nonblank
`--reason`. The published source must equal its recorded, materialized branch origin base.
An empty commit or a reverted change with the same tree still differs from
that base and requires the normal completion path. No live writer, claimed
workspace, branch owner, child, required deliverable or open batch may be bypassed.
Apply retains an immutable `artifact:false` Git completion and records a passing
`no-op` completion atomically with COMPLETED status, preserving the branch/origin.
It also repairs a COMPLETED root with no completion record, or retains an existing
passing no-op generation; it never replaces an artifact completion. Repeating
apply is idempotent. Unknown or changed evidence refuses without terminal writes.
Administrative `set-status COMPLETED` refuses a new completion of every task
with a branch or an active recorded origin, including children and projects
outside train modes, and names normal close or this explicit no-op control.
Clearing a branch name does not remove its origin or supply completion evidence.

For work delivered by an external operator, `integration_record_delivered` is
local-operator-only and defaults to preview. Apply requires an explicit project,
task, full source and base OIDs, and a nonblank reason. The source must differ
from and descend from the base, and be contained in the fetched designated
repository's actual default branch. An existing materialized origin must match
that repository, canonical branch and base; a legacy task without an origin gets
an explicit materialized origin from these verified inputs. Required deliverables,
open children or checklist subtasks, live sessions, claims, workspaces, active
batches and manual holds must first be resolved through their supported controls.
A detached reservation belonging to this task may be released atomically, with a
fresh fence and audit event, after proving no writer or external mutation relies
on it. Another task's reservation or an attached writer is refused. Apply publishes
immutable `artifact:true` provenance, rechecks the remote default tip, and records
a passing shipped completion with the supplied checks and source commit in the
same transaction as COMPLETED. Existing different completion generations remain
intact; repeating this exact operator completion is idempotent. A failed proof or
publication leaves task state and ownership unchanged. This command records
observed delivery without starting an integration runtime.

Unknown delivery evidence withholds that source and work depending on it. Its
task-scoped blocker remains visible while unrelated eligible sources can seal,
check and publish, including an already sealed batch on the same root target.

This close contract applies to Git-using passing completions in every train mode:
`train`, `hierarchy` and `development`. Under `integration.git_first: active`, an
unretained completion cannot form a batch. A visit with only unknown completions
reports `blocked` and projects the delivery reason, including
`missing_git_provenance`, with its task and target in integration status. Legacy
generations still require explicit provenance migration; branch observation is
never substituted for immutable completion evidence.

The git-first evaluator reports an explicit `no_change` state for a passing
completion whose recorded commit list is empty, whose immutable retained Git
source equals its recorded branch-origin base, and whose base is an ancestor of
the fetched target. This proves the completion added no work owed to that
target; `COMPLETED` alone never proves it. The completion outcome and commit
list, exact source generation, recorded base and target OID are all part of
the observation and guarded recheck. A missing completion or provenance record
remains unknown with a named blocker. A no-change base absent from the target
remains pending. Every completion that does not meet the no-change conditions,
including an empty commit list with a different head or missing base, uses the
existing exact whole-source delivery proof. An empty descriptive commit list
cannot override Git containment of its retained source.

Cross-epic prerequisites use this same `no_change` answer against the default
branch in scheduler readiness, pool demand, explain and claim activation. Root
train admission treats it as satisfied, so it creates neither a batch member nor
a `missing_git_provenance` blocker. Epic collection also treats such children as
satisfied without attempting to merge an artifact they did not produce.

Failed required Git delivery probes remain `unknown`. The blocker detail and daemon log
name the failing proof step, exception class and a bounded safe message; raw
Git diagnostics, paths, credentials and repository content are never projected.
Whole-source patch comparisons stream the complete binary diff into stable
`git patch-id`, including historical target changes above the general command
stdin limit. Both subprocesses must succeed; an incomplete diff, timeout or
missing object cannot supply a pending or contained answer on its own.

## Complete replacements

The git-first train may additionally prove a whole-source change through a
stable patch ID or reachable full-tree equality. Complete diffs above the general
Git runner's stdin bound are streamed for source and historical target comparisons.
A failed individual historical patch probe does not stop later probes or full-tree
proof. If no proof matches after a probe failure, delivery remains unknown and
projects the safe failure detail; that failure is never cached as a negative proof.
If all probes succeed without a match, the resolved source remains pending.
Invalid source/base identity and unavailable repository observations still fail
closed as unknown.

Whole-source patch IDs omit paths whose `merge` attribute is `aq-generated` in
the pinned target commit. Apply that same attribute view to the entire source
diff and every historical target range, including generated files absent from
the target tree. Search history using only the source's non-generated paths;
generated-only differences cannot consume the historical patch probe budget.
Attribute lookup uses Git's pattern and override rules, independent of the
observer checkout's current branch. Exact ancestry and full-tree proofs stay exact.

The no-op merge proof also compares modulo those generated paths. A merge whose
tree differs from the target only in generated artifacts proves delivery, even
when those artifacts conflict. Any non-generated difference or conflict prevents
that proof. The legacy delivery backfill uses the same content-equivalence rule.
Partial multi-commit delivery still proves nothing; unavailable attribute or diff
observations never prove equivalence.

Delivery proof is ancestry of the exact final source in the inspected target,
with Git replacement objects disabled. The metadata commit itself is never
tested for containment. Multi-commit work, another generation's trailer, a
partial cherry-pick and an empty copied marker therefore cannot prove delivery.

An explicit replacement record lives under
`refs/heads/aq-provenance/replacements/<record-content-hash>`. It retains the
replacement source as its sole parent, with the same tree, and records:

```json
{"version":1,"kind":"replacement","source_oid":"<complete-replacement-oid>","base_oid":"<replacement-base-oid>","replaces":[{"identity":{"project_id":"p","repository_id":"r","task_id":"t","generation":"original-completion-id"},"source_oid":"<original-final-source-oid>"}],"authority":"repair_contract","reason":"Exact development repair contract: repair-task"}
```

Every original binding must exist and match. The replacement must descend from
its declared base and change its tree. Its *source*, rather than its metadata
marker, must be an ancestor of the inspected target. A development repair close
uses the daemon-authored `development_repair_sources` contract, naming every
original current completion/source. Its replacement base is the merge base of
the repair source and the default branch: main advancing after the repair merged
it does not make the repair incomplete. A repair whose tree equals that base
writes no replacement and closes only if every original source is its ancestor.

A legacy original without a completion ref is retained during that close
(`legacy_repair_source`) when its final completion names the contract source, in
full or as its unique abbreviation, or when it reported no source at all and
both of these hold: the delivery that filed the repair
(`development_repair_evidence.delivery_id`) names that task and source and
postdates the completion (the generation fence), and the source is an ancestor
of the repair (the pre-provenance check). A different reported source, a newer
generation or a source the repair lacks still requires migration. An empty
legacy commit list without that filing-delivery fence (for example, only a
delivery-row source binding) is not bound automatically either; an operator can
attest it explicitly (see [Legacy migration primitive](#legacy-migration-primitive)).

An invalid repair contract, a missing passing immutable source completion, or
an unlabelled source the bridge cannot bind refuses close as an operator blocker
(`precondition:provenance_migration`). Completion recording uses the same
delivery-refusal path as pipeline verification: it retains the live task, claim,
session and workspace, sets `needs_attention:delivery_provenance_migration`, and
emits `task.needs_attention`. The refusal names the operator remedy
`aq integration migrate-provenance <project-id> --apply`, scoped with
`--task-id <repair>` for an unlabelled source; inventory can report ambiguous
evidence that still needs operator resolution.

`authority:operator` additionally requires an authorized operator/equivalence
operation and a reason. Git cannot establish semantic equivalence of arbitrary
rewrites: the authority explicitly attests that the entire original work is
replaced, including every task/generation/source and a nonempty replacement
base/source pair. Publisher operations must preserve live-writer fences before
invoking this primitive. External squash/cherry-pick operations must supply that
evidence through an authorized operation; copying trailers is insufficient.
Ancestry-preserving external merges need no replacement record.

## Legacy migration primitive

`aq integration migrate-provenance <project-id>` inventories legacy completion
generations and exact repair bindings. Add `--apply` to publish verified Git
records. The operator/live-supervisor check and typed dispatch run through
CommandHandler. The command clones into a temporary isolated read repository;
dry-run changes no durable local/remote refs, indexes, configuration or DB rows.

Pages use `--limit` (1–1000, default 500), `--offset` and `next_offset`. The
retired journal — the immutable `development.legacy_provenance` events revision
`a00000000038` kept, one per `development_deliveries` row that named a source,
without its state — is read in keyset chunks, and a page retains only the rows
that name its own tasks, so a long history never refuses a page; those rows are
bounded (5000), with a smaller `--limit` or `--task-id` as the remedy.
A page binds its generations in batches (`BIND_BATCH`, 50): an apply stages each
batch's completion records locally, then reads, pushes (every ref leased to
absence, not atomic) and reads back all of the batch's refs in one round trip
each, so each generation settles alone and a failed write is reported in
`ambiguous` with its generation. A page starts no batch after `PAGE_TIME_BUDGET`
(45 s) and then reports `budget_exhausted` with `next_offset` at its first
unexamined generation (its missing-generation report is cut to the same span);
the CLI waits 300 s, so a page never outlives its client. `counts` gives the
page's totals.
`--task-id <id>` migrates only what that (held) task's close needs: the current
completion of each `development_repair_sources` member, bound from the contract
under the same fence the close applies, or the task's own passing generations
when it has no contract; `next_offset` is then `null`. A COMPLETED task with a
branch (or the retirement marker) but no passing generation is reported as a
missing generation, exactly as the paged inventory reports it, never as
`zero_fallback`. `--task-id <id> --source <oid>` is the explicit operator
evidence for a legacy close that recorded no source Git can verify (no reported
commit, no `completion_sources`, only a delivery manifest): it binds the exact
OID to that COMPLETED task's current passing generation, marked
`authority: operator` in the inventory. It refuses a task with a repair
contract, never overrides a source the generation's own evidence names, never
rebinds a retained generation, and an object the repository lacks stays
ambiguous. Apply never modifies or
deletes retained history. It expands only unique Git OID prefixes, binds explicit legacy
`completion_sources` to their exact completion ID, and verifies all objects.
A generation already retained in Git is reported `present`. Receipt state is
never containment authority. Missing sources, conflicting bindings, multiple
matching generations and incomplete repair contracts are reported in `ambiguous`
with task IDs, generations and reasons. A COMPLETED task with a branch, or with
the retirement's `development_legacy_artifact` marker, but no completion
generation at all is reported the same way. `operations` lists the outstanding
legacy actions the retirement kept as `legacy-operation:<id>` events; terminal
receipts were not copied anywhere.

Repair migration requires the full explicit source contract, unique original
and repair generations, a nonempty base/source pair, and current target ancestry
of the complete repair source. Missing generations outside a page are reported
instead of guessed. Repeating apply produces the same immutable objects/refs.
Missing-row legacy identities use the dedicated `legacy_completion_id` on live
and archived tasks, which changes only across the COMPLETED status boundary.
Ordinary task edits do not revoke an operator decision. Revision `a00000000062`
preserves timestamp-derived locators and restores audited live attestations
only after the last recorded reopen, without asserting delivery: the retained
Git record must still exist and bind the exact subject and source.
Legacy operator equivalence without an exact source generation or a nonempty
replacement base/source pair remains unmigratable and needs explicit new
operator evidence. Missing branch refs are never interpreted as empty work.

The compatibility reader is gone. A generation without a retained record now
evaluates `unknown` (`missing_git_provenance`) wherever delivery matters, instead
of borrowing a branch head, a reported commit or a journal manifest, so the
inventory's `fallback_generations` are exactly the generations that stay unknown
until migrated or resolved. `zero_fallback` across every page is the operator's
acceptance check that nothing is left behind. The retirement revision cannot
consult git, so it reports rather than decides: it keeps source provenance as
events, marks live branchless tasks a manifest named with an unrecorded source
(`development_legacy_artifact`, fenced to their generation, so they read unknown
rather than organizational), and records a `development.legacy_retirement`
summary per project naming those tasks, archived tasks of the same shape and
malformed rows.

## Local train quiesce

`integration_quiesce` is a separate local-operator-only control for an unfrozen
reconciler root in admission. It requires the explicit project integration
generation, root subject id/version and every reservation id/fence to release.
It refuses frozen sources, live writers, claims, workspaces, active operations,
unresolved remote mutations, another live subject, or a changed sweep request.
Task reservations require an explicitly PAUSED or finished task in that project;
collector reservations require a completed or cancelled operation in the project.
Attached or foreign ownership is refused. Preview writes nothing. Apply disables
that project's train schedule, clears its idle request/catchup, closes the root
with a journal action and releases the explicitly selected detached reservations
with fresh fences and an operator audit event. Tasks, refs and retained work stay
intact. It changes no integration mode and does not authorize completion; the
normal generation-fenced configuration control still governs the later cutover.

Historical claim records remain provenance when their exact named session is
stopped in both observed and desired state. Unknown or revivable holders still
block. An ended operation's pushed resolution remains audit history only when
its terminal batch and exact applied ref mutation acknowledge the resolved SHA.

Rollout may retain unfinished legacy cleanup fences without treating them as
active train writers. This exception requires a detached collector on its exact
`refs/heads/aq/integration/*` branch, a promoted legacy batch with no target ref,
a completed or cancelled owning batch operation, and no reserved ref mutation
or unsettled root intent. It applies only to rollout checks. The owner, fence,
cleanup backlog and publication evidence remain intact; cleanup still gates
owner release. This classification establishes ended authority and does not
assert delivery, completed cleanup, or permission to delete any ref.

## Expired mutation read-back recovery

The local operator may use `integration_reconcile_expired_mutation` to preview or
settle one expired candidate/ref mutation after its owning operation completed or
was cancelled. It requires an explicit project, mutation id, exact nonce, recorded
branch and lease fences, and a reason. The designated repository, operation
episode, detached branch authority and absence of live project writers, claims
and integration leases are re-proved. Two actual remote target reads must agree,
with unchanged durable bindings under the project/repository locks. The desired
OID records `applied`; a confirmed different or absent target records `superseded`.
An unavailable remote or changed binding refuses without writes. `prewrite_at`
is not proof of an unstarted candidate mutation. This control writes only the
mutation observation and operator audit; it never publishes, deletes Git work,
closes a task or restarts the old operation. Root-main mutations retain their
original specialized recovery control.
