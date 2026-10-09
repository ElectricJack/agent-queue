# Development integration

> Historical runbook: the old train/development publisher, shadow cutover and
> rollback controls were removed on 2026-10-04. Commands for those paths below
> are retained as historical context. Use the [current Subject integration guide](hierarchical-integration-trains.md)
> for configuration, delivery and recovery. `app-setup`, `app-verify` and
> `trust-manifest` remain available for App diagnostics.

Turn on AQ's batched delivery for a project, watch a batch reach your default
branch, and read the journal it leaves behind.

This is the delivery path AQ is configured to use on this repository. Workers
push ordinary task branches and close with the checks they actually ran; the
daemon collects finished branches, merges them, validates the result once, and
publishes it. There is no pull request, no hosted-CI receipt chain, no
per-parent verifier and no squash in this path.

A `blocks` dependency on a completed code task requires fresh git proof of its
current completion revision on the configured default branch.
Preserving a batch candidate does not release the successor. Each clean source
merges directly into the batch, and a conflicting member holds only its own
dependents; independent siblings can still publish. The shared evaluator uses
exact source ancestry or explicit git replacement evidence bound to the
immutable completion generation. A later completion at a different revision
needs its own proof. Branchless organizational tasks have no artifact of their
own; recorded code remains an artifact after branch cleanup.

The publisher can assemble an already-completed dependency chain in one batch,
in dependency order. It does not spend a separate batch interval on each link.
Human gates and unfinished prerequisites still withhold publication.

If you want to know *why* it behaves the way it does, read
[the integration concept page](../concepts/integration.md) first — this guide
assumes its vocabulary (batch, manifest, journal, parked).

## Before you start

* The daemon is running and you are on the machine that runs it. Every command
  that changes integration policy requires **local operator authority**; a
  worker session's token gets `unauthorized` for `develop`, `sweep`, `adopt`
  and `cancel-preserving`. `aq integration status` is readable by a worker for
  its own project.
* The project has exactly one registered repository, or you have designated
  one (see [Designating the repository](#designating-the-repository)).
* The repository has a remote URL the daemon can push to.

## Turn it on

```bash
aq integration develop demo \
  --validation focused \
  --command 'aq test tests/test_demo.py' \
  --interval-seconds 300 \
  --reason 'Batched development delivery for demo'
```

The command returns `{"outcome": "configured", …}` with the stored policy, and
the same decision is written into the delivery journal, where it stays. A
supported policy for this repository could look like this in
`aq integration status agent-queue`:

```text
{
  "state": "finished",
  "target_ref": "refs/heads/main",
  "manifest": [],
  "evidence": {
    "kind": "configuration",
    "operator_id": "local:-",
    "policy": {"validation": "focused",
               "commands": ["aq test tests/test_development_integration.py"],
               "timeout_seconds": 300, "interval_seconds": 300, "max_batch_size": 50}
  },
  "reason": "Steady-state development: five-minute batches and focused integration checks; …"
}
```

(A configuration row carries an empty manifest and is journaled in state
`finished`: it records a configuration action, not delivery.)

What that did, all in one transaction
([`DevelopmentIntegration.configure`](../../src/integration/development.py)):
set the project's mode to `development`, stored the policy, cleared any drain,
bumped the project's integration generation, suppressed the older
review-and-merge automation for this project, and wrote a `configuration` row
into the delivery journal recording who asked and why.

Options:

| Option | Meaning |
|---|---|
| `--validation focused` | Default. A reported command failure parks the batch; an infrastructure outcome defers it. At least one command is required. |
| `--validation advisory` | Runs the commands, records failures, publishes anyway. |
| `--validation none` | Runs nothing; the journal records `not_run`. |
| `--command` | Repeatable finite command. AQ rejects unsupported syntax at configuration time; see the preset list below. Commands run as detached jobs against the candidate commit. |
| `--interval-seconds` | Periodic recovery sweep interval. Default 300. Task completion also requests a sweep on the next integration cycle (normally within 5 seconds, once an active batch finishes). |
| `--timeout-seconds` | Seconds each command may *run*. Default 300, maximum 3600. Time queued for a test slot is not counted. |
| `--slot-wait-seconds` | Seconds a command may queue for a test slot (via `aq test`) before the batch is deferred to the next tick — never parked, never repaired. Default 600, maximum 3600. |
| `--regenerate` | Command that rebuilds the repository's generated files — the paths its `.gitattributes` marks `merge=aq-generated` — for example `scripts/regenerate-generated.sh`. Run without a shell in AQ's clone after a merge in which both sides changed a generated file; see [Conflicts confined to generated files](#conflicts-confined-to-generated-files). Unset by default. |
| `--regenerate-timeout-seconds` | Seconds one regeneration may run before that merge counts as failed. Default 600, maximum 3600. |
| `--reason` | Required, and kept in the journal. |

Supported commands are `aq test …`, `pytest …`, `python[3] -m pytest …`,
`ruff check …`, `python[3] -m ruff check …`, `npm ci`, `npm test`,
`npm run build`, `pnpm install --frozen-lockfile`, `pnpm check`,
`pnpm run build`, and `scripts/e2e-smoke.sh`. The Node and smoke commands
take no extra arguments. Shell operators, arbitrary scripts, absolute
interpreter paths, and wrappers such as `isolated-tests.py` are unsupported.
The Python presets use the daemon's interpreter and its installed packages;
they cannot select another project's virtualenv. Node and pnpm executables
are resolved by the server. Configure an install command before a test or
build command; checks in one validation attempt share a detached snapshot,
so ignored `node_modules` survives between them. A retry uses a fresh snapshot.

> **Note.** Validation commands do not run in a worker's worktree or under a
> shell. They run as managed jobs in a detached snapshot of AQ's retained
> candidate clone. Each command may *run* for 300 seconds
> (`timeout_seconds`, up to 3600); time it spends queued for a test slot is bounded separately
> (`slot_wait_seconds`, default 600). A timeout is recorded as exit code 124
> and, like any validation that verified nothing, defers the batch rather
> than parking it — see
> [Validation could not finish](integration-troubleshooting.md#a-passed-task-blocked-at-delivery).

Existing policies containing commands outside this list must be reconfigured
by the local operator. AQ does not rewrite a stored policy during an upgrade;
an unsupported stored command defers validation until it is replaced.

Policy changes take effect on the next batch. There is no drain to wait for.

## Watch a batch happen

`aq integration status <project>` reports git delivery evidence and publisher
operations, including pending writes. It does not require GitHub App bindings or strict train rollout
configuration: development mode uses ordinary Git, including local origins.

Let the interval fire, or ask for a sweep now:

```bash
aq integration sweep demo
```

A delivered sweep returns `{"outcome": "delivered", "id": …, "head_sha": …,
"manifest": […]}` and leaves the matching journal row behind. Here is a real
one from this repository, where a batch carried a sibling documentation ticket
to `main`:

```text
{
  "id": "fb0f1204-41c2-45e8-8489-7c0c860b28db",
  "target_ref": "refs/heads/main",
  "state": "finished",
  "expected_sha": "a62833d5819ca10179a0ea8f9b793dc6e7bcd7c1",
  "prepared_sha": "a9a10b3183d13f0b0fdec44ab6bbe3b6c4fbdd37",
  "manifest": [{"task_id": "solid-grove.12", "source_sha": "0e9f949f…", "parent_task_id": "solid-grove"}, …],
  "evidence": {"kind": "local", "validation": "focused",
               "checks": [{"command": "…", "exit_code": 0, "output": "…"}],
               "conclusion": "passed"},
  "reason": "development batch"
}
```

The outcomes you will see:

| `outcome` | Meaning | What to do |
|---|---|---|
| `delivered` | The batch is on the default branch. | Nothing. |
| `idle` | Nothing was eligible this pass. | Nothing. `parked` lists members that conflicted. |
| `parked` | A batch was assembled and its tests failed. | Read the evidence; fix, or retry — see below. |
| `deferred` | Validation could not finish (timeout, no test slot, outage, nothing collected). Not parked; no repair. | Nothing, unless it repeats — then fix the validation environment. |
| `base_moved` | The default branch moved while publishing was being prepared. | Nothing. The next sweep rebuilds on the new base. |
| `adopted` | Recorded already-delivered work. | Nothing. |

## Read the journal

```bash
aq integration status demo
```

The interesting fields:

* `effective_mode` / `desired_mode` — should both say `development`.
* `generation` — the compare-and-swap token any mode change must quote.
* `policy` — the validation policy actually in force.
* `deliveries` — the journal, newest first, capped at 100 rows.
* `ownership` — branches with a live writer, each classified `reserved`,
  `stopped_awaiting_preservation` or `live_or_unconfirmed`.
* `pending_publications` — rows in `publishing`: a push whose outcome is not
  yet confirmed. A non-empty list is also reported as a `publication_pending`
  blocker and clears itself on the next sweep.
* `parked` — deliveries waiting for a retry, a repair or a human.

Publisher operation states are `prepared`, `publishing`, `finished`, `parked`
and `cancelled`. Their revisions are stored as `development.operation` events
in the existing event log. `finished` says an action ended; it never establishes
that a task is delivered. Validation checks and infrastructure streaks remain
actual execution evidence. After a restart or an uncertain push, the publisher
fetches and inspects git before its next action. The status field keeps its
name, `deliveries`, but it is this operation history, not a delivery record.

### Delivery is git's answer

Nothing in the database says a task is delivered. Every reader — readiness,
claims and pool demand, the publisher, container settlement, archive, branch
cleanup, status, explain and doctor — asks git one question through
[`src/integration/delivery_truth.py`](../../src/integration/delivery_truth.py):
is the exact final source of the task's *current* completion generation an
ancestor of the configured target? A close retains that source in git
(`refs/aq/provenance/completions/…`, see
[exact completion provenance](../specs/design/git-completion-provenance.md)), so
branch cleanup and archive never erase the answer.

A generation without that retained record is **unlabelled**. No branch head,
reported commit or historical manifest stands in for it: it is `unknown`
(`missing_git_provenance`), and unknown fails closed — its dependents wait, it is
never published, never archived and never settles a container. The publisher
names it with the `missing_provenance` skip reason, and a repeated identical
skip stalls with one supervisor message, like every other stall. A branchless
task with no recorded commits is organizational and has no artifact of its own.

The one database answer every reader honours is a **settlement**: a record that
a generation is *not owed* to one target (task metadata
`development_delivery_settlement`). Git is still asked first: a settlement only
turns work the target lacks from `pending` into `settled`, which satisfies
dependents and settlement like a delivery but never says the work was delivered;
contained work reads `contained` whatever was settled. It is fenced to its
repository, its target and, for ordinary work, the exact completion it settled,
so reopening and closing the task owes the new work again.
Settlements come only from a [retarget](#when-the-default-branch-changes) and
from [`settle-parked`](#settle-or-dismiss-a-parked-delivery).

### Retiring the legacy delivery table

Revision `a00000000038` removed the `development_deliveries` table, the
receipt journal older publishers wrote. Before dropping it, the revision keeps
what is not a delivery answer, and only that:

* each outstanding action (`prepared`, `publishing` or `parked`, a validation
  deferral, a pending branch cleanup) becomes the `development.operation`
  event `legacy-operation:<row-id>`, so recovery resumes it by inspecting git;
* each row that named a source becomes one immutable
  `development.legacy_provenance` event: identity, target, member sources,
  exact `completion_sources` bindings, repair replacement mapping and the test
  or conflict evidence it recorded — without its state or any delivery
  conclusion. Only `aq integration migrate-provenance`, a legacy repair's
  close and train-mode `adopt-legacy-deliveries` read it, and each of them
  re-proves every source in git;
* a live branchless task a manifest named with a source its current completion
  never recorded gets the `development_legacy_artifact` marker (fenced to that
  generation), so it reads as unknown rather than as an empty container;
* one `development.legacy_retirement` event per project lists the retained
  actions, the marked tasks, archived tasks of the same shape and malformed
  rows, and the daemon's upgrade log names them.

The revision is idempotent: rerunning it, or running it after the operator
command below already retained an action, retains each fact once. It never
writes a delivered state, and it does not recreate the receipts under another
name.

Retain every legacy generation in git, ideally before the upgrade and again
after it:

```bash
aq integration migrate-provenance demo
aq integration migrate-provenance demo --apply
```

The result names `fallback_generations` (generations that would evaluate
unknown for lack of retained provenance), `fallback_count`, `zero_fallback`, the
outstanding legacy actions retained as events in `operations`, and unresolved
identities in `ambiguous`, with the page's totals in `counts` (generations
examined, `present`, `written`, `would_write`, `missing_generation`, `ambiguous`,
`repairs`, `fallback`). Follow `next_offset` through all pages; a zero count
on one page does not prove the entire project migrated. Each page is bounded in
time: generations are bound in batches whose new refs are published in one
transfer, and a page starts no batch after 45 seconds. A page that stops early
reports `budget_exhausted: true` and a `next_offset` at its first unexamined
generation, so continue from there (a `--task-id` run is simply repeated).
Exact completion and
repair mappings are retained in git. Missing generations, ambiguous sources,
branchless tasks a retired manifest named without a generation, and incomplete
equivalence evidence are reported with task ids rather than guessed; an
operator resolves each with explicit provenance or adoption. Until then those
tasks stay unknown. A legacy close
that recorded no commit and that only a delivery manifest located can instead
be bound by attesting its exact final source:

```bash
aq integration migrate-provenance demo --task-id demo.7 --source <40-hex-oid>          # preview
aq integration migrate-provenance demo --task-id demo.7 --source <40-hex-oid> --apply
```

The attestation binds only that COMPLETED task's current generation, and is
refused when the generation's own evidence names another source or a repair
contract names the task's sources.

Some legacy tasks have no database completion row at all. They can be recovered
without reopening finished work: supply an audit reason with the exact source,
or explicitly declare artifact-free research with `--no-artifact`:

```bash
aq integration migrate-provenance demo --task-id demo.8 --source <40-hex-oid> --reason "legacy work already delivered" --apply
aq integration migrate-provenance demo --task-id research --no-artifact --reason "research only; no code" --apply
```

Omit `--apply` to preview either decision. The command retains immutable Git
evidence under the recorded current generation id, or a legacy generation
identity fenced to the task version. Missing-row attestations retain that
version identity too, so archiving preserves the decision. It checks for active
writers and rechecks the generation before writing; reopening changes the
generation. It records the operator, reason and result in the audit log and
does not fabricate a passing worker completion or CI evidence. A no-artifact
declaration cannot erase source evidence that the generation already recorded.

A COMPLETED task whose latest completion did not pass is attested the same way
on a `--reason`: a research task closed over a blocked close is finished work,
not work still owed. Its recorded commits are what it read rather than an
artifact of its own, so `--no-artifact` replaces them and `--source` may name a
different commit — but an attested source must already be contained in the
target, so the decision can only clear work the target holds. The failed close
is retained verbatim; the audit records `completion_outcome: fail`.

A bare pre-train parent episode uses this same completion provenance, and so
does an episode whose only parent operation was cancelled before it ever
verified or completed: neither demands a verification that never existed. Any
real parent operation or verification history still requires the exact verified
parent completion, and so does a parent an operator already adopted — a stale
adoption is a decision to redo, not a legacy episode.
`invalid_parent_completion` and `parent_provenance_mismatch` name broken
bindings and require parent verification recovery. They do not mean a branch
must be pushed again. Neither does `parent_adoption_provenance_mismatch`: an
adopted parent's retained source disagrees with the head its operator adoption
recorded, or that record holds no artifact. The adoption is redone through the
delivered-children recovery (`aq integration adopt … --settle-delivered-children
--dry-run`, see [integration troubleshooting](integration-troubleshooting.md)),
never through `migrate-provenance`. `missing_or_ambiguous_source` means git
could not resolve the retained record itself — unreadable, or naming a commit or
replacement the repository cannot resolve — so the stall names it rather than a
missing ref, and only a new completion of the exact commit supersedes it.

## What a worker sees

A worker in a development-mode project is primed with a shorter contract than
in the strict modes ([`src/prime/sections.py`](../../src/prime/sections.py)):

> Commit and push your task branch, run focused local checks, and close with
> actual evidence. Ordinary commits and merges are accepted; no squash, PR,
> hosted CI, or parent verifier is required. The daemon collects completed
> source branches and publishes validated batches to main. Do not push main
> yourself.

With a `regenerate` policy the section adds one more paragraph: never
hand-merge generated files; take either side, resolve the sources, run the
policy's command and commit what it writes.

Two consequences worth knowing as an operator:

* **A closed task is not a delivered task.** Delivery happens on the next
  sweep, and can park.
* **Stacked work needs declared dependencies.** The sweep orders members by
  their dependency edges, and skips a dependent whose prerequisite parked. Work
  stacked only by branch topology, with no edge, is not ordered for you.

## When something conflicts

Every clean member merges straight into the batch; there is no per-parent
assembly. A member that will not merge is parked with its conflict output as
evidence, and the rest of the batch continues: its siblings, its parent and
unrelated work are never held by it. Only a dependent that declared an edge on
it waits. A real parked row — two documentation tickets editing the same
generated manifest:

```text
{
  "state": "parked",
  "target_ref": "refs/heads/main",
  "expected_sha": "a62833d5819ca10179a0ea8f9b793dc6e7bcd7c1",
  "manifest": [{"task_id": "solid-grove.21", "source_sha": "05858218…", "parent_task_id": "solid-grove"}],
  "evidence": {
    "kind": "merge_conflict",
    "detail": "Auto-merging docs/README.md\nCONFLICT (content): Merge conflict in docs/plans/…/module-ownership.json\n…"
  },
  "reason": "source conflict; independent work may continue"
}
```

Newer rows also name `source_sha`, `target_ref`, `target_sha` and the batch
`aggregate_sha`, and say what the source conflicts with, each claim proven by a
separate merge: `conflict_with` is `target` when it conflicts with the target
alone (no sibling is blamed), `members` when a pair merged onto the target
conflicts on its own (`conflicting_members` names each partner, its source and
the paths), `batch` when only the combined batch conflicts, or `unproven`. The
repair description repeats it.

After the batch is assembled AQ looks at each parked row again:

* if a later member already brought the same content to `main`, the row is
  marked `adopted` and nothing else happens;
* otherwise AQ files one ordinary repair task, `development-repair-<digest>`,
  on its own branch, naming the source branches, conflicting files and target.
  In its branch, which starts from that target, the worker merges each listed
  source revision by its exact SHA. It never rebases, squashes or
  cherry-picks, so the source stays an ancestor and delivering the repair
  delivers the source. A conflicting generated file is regenerated, not
  hand-merged. A single-source repair is placed under its completed source when
  depth permits; at the hierarchy depth cap it is rooted with provenance and a
  delivery hold on the source. It is a normal queue task with three retries;
  waiting for a worker does not expire it.

Closing the repair does not release the source's dependents. Publication of
the passing repair to the configured default branch adopts the parked source
git replacement evidence, which releases them after fresh evaluation. That holds even for a repair that rewrote the
source commits anyway.

If `main` moves before the closed repair is published, the repair's own
publication can park on a new conflict. That row gets the next repair, rooted
and discovered-from the previous one, and the source is carried by the whole
chain. At most three generations are chained; past that nothing more is filed,
the batch records the `repair_generation_exhausted` diagnostic and the content
is left parked for you. `aq integration sweep demo --retry` or `--recover-child
<task>` on a parked source that conflicts again refreshes its existing row
rather than parking it twice. A recovery that leaves the child unpublished
names the repair carrying it, and the chain, or says that none does.

```bash
aq doctor --check integration.development_conflicts_unrepaired
```

lists every completed source parked on a merge conflict whose chain has no open
repair: none was filed, one ended FAILED or BLOCKED, or the generation budget
ran out. It names the batch, the conflicting files and the chain. Nothing will
release those dependents by itself. Reopen the ended repair; or merge the
source revision onto `main` on a branch that keeps it as an ancestor and record
it with `aq integration adopt`; or cancel the batch.

Parked content is not re-tried while its sources are unchanged — a worker
pushing new commits makes it eligible again by itself. To retry unchanged
parked content under the current policy:

```bash
aq integration sweep demo --retry
```

A park holds its source only on the target it parked on; after a
[retarget](#when-the-default-branch-changes) it holds nothing. When a parked row
is for work the target does not owe at all, settle it rather than repairing it:
[settle or dismiss a parked delivery](#settle-or-dismiss-a-parked-delivery).

### Conflicts confined to generated files

Most conflicts between parallel branches used to be in generated files: two
branches that each add a command both rewrite the CLI inventory, the command
pages index, `openapi.json` and the client models; two that each add a test
module both rewrite the selection catalogue. Every such conflict parked its
source and filed a repair whose commit read "regenerate catalogue/inventory".

Declare those files in the repository and give the policy the command that
rebuilds them:

```text
# .gitattributes
tests/selection_catalogue.json merge=aq-generated
openapi.json merge=aq-generated
packages/aq-client/** merge=aq-generated
```

```bash
aq integration develop demo … --regenerate scripts/regenerate-generated.sh --reason '…'
```

Then each member merges with the `aq-generated` driver defined as a text merge
that keeps the candidate's side of every overlapping hunk, so a generated file
never conflicts. When both sides changed one — or a modify/delete left one
unmerged — AQ runs the command in its clone, stages what it rewrote and folds
it into that member's merge commit; the batch evidence lists the files under
`regenerated`, by task. A conflict in any other file still parks the member,
and its `conflicting_files` then names only the files a person must resolve.
The repair task's description tells its worker to regenerate rather than
hand-merge.

The regeneration is checked, never trusted:

* it runs with this interpreter's `bin` first on `PATH`, a minimal
  environment and the worker database sentinels, bounded by
  `regenerate_timeout_seconds`;
* if it exits non-zero or times out, or writes a file its `.gitattributes`
  does not mark `merge=aq-generated`, the merge is undone, the clone is
  cleaned, and the member parks with `evidence.regeneration` holding the
  command, exit code and output tail.

Rows parked before you set `--regenerate` stay parked until their sources
change or you retry them with `aq integration sweep demo --retry`.

## When a candidate keeps being skipped

A completed task the sweep cannot publish yet is *skipped*, with the reason
recorded in its `development_publisher_skip` task metadata: an undelivered
dependency, a source ref that is gone before its work reached `main`, a
dependency cycle, a parked source. A skip is an observation, not a failure —
but one that repeats forever is a stall, so it has a bounded life
(`src/integration/development_stalls.py` (retired)):

* Each evaluation is fingerprinted: task, latest completion, reason, related
  task, the configured target (repository and ref) and the git evidence that
  matters — the candidate's observed source OID, plus the target OID for a
  reason decided by merging into it (a conflict). A target that moved without
  containing the work is not new evidence: unrelated deliveries do not reset
  the count.
* After `integration.publisher_stall_after` (default 5) consecutive identical
  unsuccessful evaluations the attempt ends as `stalled`. The publisher logs
  one error, sends `supervisor-<project>` one message naming each stalled task
  with its repository, target and source OIDs, completion, reason and a
  recovery command, and `aq doctor --check
  integration.development_publisher_stalled` reports ERROR. Earlier
  evaluations are progress observations (WARN from the third).
* A stalled attempt is not retried blindly and not reported again, across
  sweeps and daemon restarts alike. The sweep still checks it every time: as
  soon as git shows the work delivered, the candidate publishes and its record
  is removed. A new completion, a moved source, a changed target or reason, or
  an explicit `aq integration sweep demo --retry` / `--recover-child <task>`
  starts a new attempt, which can stall and notify again.
* A skip waiting on a live repair (its own parked source, or a parked
  dependency's) records `state: waiting` and does not count: the repair is the
  work in progress. `waiting_on` names the repair actually carrying the source,
  followed through repairs of repairs. When the chain ends without releasing the
  source (a repair fails, or the generation budget runs out), counting starts.
* Idle ticks evaluate nothing and daemon downtime is not counted.

The record holds observations and notification deduplication only. It is
never proof that work did or did not reach `main`; git answers that on every
sweep.

## Record work you delivered by hand

If you merged something yourself, tell AQ so its journal and the tasks agree:

```bash
aq integration adopt demo \
  --task demo.4 --task demo.5 \
  --target-ref refs/heads/main \
  --head-sha 4f0a2b6c… \
  --reason 'Merged by hand after review'
```

Rules ([`DevelopmentIntegration.adopt`](../../src/integration/development.py)):

* `--head-sha` must be **exactly** where the ref is right now, or the command
  refuses. `--target-ref` defaults to `refs/heads/main`.
* Each task's branch must be an ancestor of that SHA. This is observed in a
  private read snapshot, so an ancestry-only adoption succeeds while a sweep is
  running and never moves the publisher's checkout.
* For a squashed or hand-rewritten equivalent, add `--accept-equivalent` after
  reviewing the content; the manifest then records
  `"acceptance": "operator_equivalent"` instead of `"ancestry"`. That decision
  takes the publisher lock, so it is refused while a sweep runs; run it again.
* No selected task may still have a worker or a live session, and no child of a
  selected task may still be open. A task whose status, branch, claim or
  completions changed after its branch was observed is refused; adopt again.
* Tasks are closed leaf-first, with a completion record whose verification
  reads *"Operator adoption; not CI attested"*. The evidence is recorded as
  `operator_accepted` — never as a CI result.
* The adoption retains each task's exact completion generation in git (and,
  for `--accept-equivalent`, an explicit operator replacement record naming the
  generation, its source and the adopted base). Git then answers delivery like
  for any other close, so the adopted tasks count as delivered while their
  work stays on the target, and their `blocks` dependents are released —
  including content that landed rebased, so a chain of adopted repairs is never
  re-held as a dependency cycle. The `finished` operation event it appends is
  history; neither it nor a retired `adopted` row counts as delivery.

## Cancel repair scheduling you no longer want

```bash
aq integration cancel-preserving <operation-id> --reason 'Superseded by demo.9'
```

This cancels the scheduling, not the work. Repair and verifier tasks are paused, the
operation and its unfinished stages are marked cancelled, and a `cancelled`
row goes into the journal. Refs and attached workspaces are **retained** — only
detached reservations (no session, no workspace) are released. If a repair
writer's session is not provably stopped by its provider, the command refuses
with `DevelopmentBusy` rather than pulling a branch out from under a live
process.

Repeating cancellation also retires leftover tasks from older cancellations
that omitted the verifier. Once all delegates are terminal or paused, repeating
the command makes no changes. The verifier's task and audit references are kept;
cancellation does not claim that verification passed.

## Settle or dismiss a parked delivery

A parked row whose members the target does not owe (work already on another
branch, superseded, or delivered some other way) needs a decision, not a repair
and not a hand-moved target branch:

```bash
aq integration status demo                                   # parked: [<operation-id>]
aq integration settle-parked demo <operation-id> --reason 'Superseded by demo.9'
aq integration settle-parked demo <operation-id> --dismiss --reason 'Stale park'
```

* **Settle** (the default) checks in git that each member's current completion
  is still the one that parked, then records it, and every repair filed for the
  row, as not owed to the target. Only a row on the current target can be
  settled; the next sweep retires a row parked on an old one. The publisher never
  merges them there, their dependents are released on the next evaluation and no
  further repair is filed. The result lists `open_repairs`: repairs still being
  worked on, which you retire with `aq task close <id> --obsolete` once their
  sessions stop. A member that completed again after it parked owes its new work,
  so settling it is refused; dismiss the row instead.
* **Dismiss** only withdraws the row: its members return to the publisher, which
  merges them again on the next sweep and parks them again, as a new row, if they
  still conflict. Repairs are left alone.

Either way the row becomes `cancelled`, with the decision (`settled_by` or
`dismissed`: reason, operator, time) in its evidence. The command takes the
publisher lock, so it answers `blocked` while a sweep runs; run it again. It
needs a LOCAL operator or the project's live supervisor
([`DevelopmentIntegration.settle_parked`](../../src/integration/development.py)).

## When the default branch changes

Retargeting a development project — `aq integration enable --mode disabled`,
`aq project set … integration-repository` with a new `default_branch`, then
`aq integration develop` — makes the publisher deliver somewhere new. Three
things about the old target are not carried over
(`src/integration/development_settlement.py` (retired)):

* **Work the old target already has.** A generation completed before the
  retarget whose source the old target contains is settled as not owed to the new
  one (`delivered_to_previous_target`), instead of being merged — or parked and
  repaired — into it. Work completed before the retarget that the old target does
  *not* have is still owed and is delivered to the new target as usual. If the old
  branch is gone, nothing can be proven there and everything stays owed.
* **Repairs built for the old target.** A repair starts from the target its park
  named and carries that branch's history, so it is settled as not owed to any
  other target (`repair_for_previous_target`); its sources are judged on the new
  target on their own. A source that conflicts on the new target too gets a repair
  of its own there (the park records it as `evidence.repair_id`), never the old
  one. A park on the current target whose members all turn out not to be owed is
  cancelled, and its repairs are settled with it; if the same content parks there
  again later, its repair is owed again.
* **Parks on the old target.** A park is a conflict with one base of one target.
  Parks are scoped to the current target, so a source parked on the old one is
  merged into the new one on the next sweep; the stale row is cancelled with
  `evidence.retired` naming the new target.

Only `aq integration develop` moves the target: its configuration row records
`retarget: {from_ref, to_ref}` (and its result says so), and the first
configuration onto the new target fences which completions came before it. An
`aq integration adopt` onto another ref is not a retarget. The first sweep on the new target writes one
journal row of kind `settlement` listing what it settled, and sends
`supervisor-<project>` one message naming each task. If some of that work does
belong on the new target, merge its source there on a branch that keeps it as an
ancestor and record it with `aq integration adopt`, or reopen and close the task.

## Designating the repository

`aq integration develop` needs to know which repository it publishes to. With
exactly one registered repository it uses that one (creating the row from the
project's `repo_url` if the project has no repository row yet). With zero or
several, designate one first:

```bash
aq project set demo integration-repository-id demo-repo \
  --expected-integration-generation 3 \
  --reason 'Designate the publication repository'
```

The generation comes from `aq integration status demo`. Integration
configuration is compare-and-swap guarded: a stale generation is refused rather
than applied.

## Automatic recovery you will see in the journal

Development mode reconciles stopped branch owners on every sweep interval
([`preserve_stopped_owners`](../../src/integration/development.py)). When a
worker's session is confirmed stopped by its provider and nothing has replaced
it, AQ detaches that checkout at its existing `HEAD`, disables the workspace so
allocation cannot recycle a dirty slot into the next task, releases the branch
fence with a bumped token, returns a stranded `BUSY` agent to `IDLE`, and
writes a `preserved_workspace` row to the journal. Files and commits in the old
checkout are untouched. It does not resume manually paused tasks.

## Clean up the example

If you created a throwaway project for this guide, take it back to the shipped
default:

```bash
aq integration enable demo --mode disabled \
  --expected-generation 4 \
  --reason 'Finished the walkthrough'
```

Publisher operation events are deliberately retained: they are history, not
runtime state. See [switching integration modes](integration-migration.md) for
what does and does not carry over.

## Operator acceptance: reading delivery truth without changing anything

Everything below is read-only. None of these commands claims, settles,
repairs, rebinds or restarts anything; they only inspect what git answers.
The `aq` commands run against the live daemon on its real database — which
makes them read-only, unlike the `integration adopt` / `develop` / `resume`
controls elsewhere in this guide; the `git` commands run inside a clone of the
project's target repository, limited to read-only subcommands. **Never run
`alembic upgrade`, `aq start` or any migration against the operator database
from a worker slot** — a worker's `AQ_DB_SCOPE=worker` sentinel refuses local
upgrades, and that refusal is correct.

Keep the two facts separate in every answer:

* **Source checkpoint** — the exact commit the current completion generation
  finished at, on the task's source branch. A *closed* task and a *pushed*
  source branch both prove only this. Checkpoint existence is necessary,
  never sufficient.
* **Target containment** — the target ref (usually `refs/heads/main`)
  actually contains that exact source commit *for the current completion
  generation*. Only `merge-base --is-ancestor` on the target repository
  proves it. A stale close, a deleted source ref, or a journal row pointing
  the wrong way changes nothing here.

### Read-only command checklist

For a completed task `<task>` in project `<project>` with source branch
`<branch>`:

```bash
# 1. What the scheduler and settlement wait for, as git answers it now.
#    Read-only; lists targets, pending work and unknown work with reasons.
aq integration status <project>

# 2. Why a specific task is (or is not) running right now.
aq task explain --task-id <task>

# 3. Project-wide publisher health: conflicts still parked without repair,
#    and any stalled publisher (WARN below the stall bound, ERROR at it).
aq doctor --check integration.development_conflicts_unrepaired
aq doctor --check integration.development_publisher_stalled

# 4. Provenance inventory: how many completed generations still lack an
#    exact git source label. Default is a dry-run — no write.
aq integration migrate-provenance <project>
```

In the target repository, for the exact source checkpoint `<sha>`:

```bash
git log -1 <branch>                                    # source checkpoint, if the ref still exists
git merge-base --is-ancestor <sha> refs/heads/main && echo contained || echo not-contained
git rev-parse refs/heads/main                          # current target head, to compare against the status targets
```

Expected outcomes, by shape:

* **Contained (delivered).** `integration status <project>` lists the task
  under neither `pending` nor `unknown`; `merge-base --is-ancestor` on the
  target succeeds; `task explain` shows no `development_dependency_delivery`
  reason. A deleted source branch changes none of this — ancestry is the
  proof, not the ref.
* **Depth-two / depth-three completed source conflicts with advanced main
  (`agile-torrent.9`, `wise-ember.12` shapes).** The publisher parks the
  conflicting source, names the source(s), the target and the paths in its
  conflict evidence, and files exactly one bounded repair child — it does
  not invent a conflicting sibling pair when the conflict is against
  `main`. The clean sibling still publishes. Until the repair delivers,
  the source is not released: `task explain` on a dependent shows
  `development_dependency_delivery`. `doctor --check
  integration.development_conflicts_unrepaired` stays WARN/ERROR listing
  the parked source(s); after the repair delivers, the check goes OK and
  the stale skip diagnostic clears.
* **Adopted repair diagnostics.** An adopted repair cycle drops out of
  candidate evaluation entirely — no skip record, no further repair, no
  supervisor message. `doctor --check
  integration.development_publisher_stalled` reports it only if a *fresh*
  identical failure keeps recurring at the configured threshold (default
  five consecutive ticks; positive), at which point exactly one supervisor
  message exists and repeated ticks or a daemon restart do not duplicate it.
  New source, target or evidence — or an explicit recovery — starts a
  fresh attempt.
* **Missing refs and archived tasks.** A missing source ref, an archived
  task or a source on the wrong repository is *unknown*, never *empty* and
  never *delivered*: the dependent stays withheld
  (`missing_git_provenance`), the container stays open, and no repair or
  cleanup fires. `integration status` shows the task under `unknown` with
  its reason.
* **Completed epics (branchless containers).** An epic has no artifact of
  its own; it settles once *every* child that recorded work has its current
  generation contained — through the existing settlement transitions, once
  per container, with an audit event. A live session, an explicit non-stale
  blocker or train ownership keeps the guard in place even when git
  otherwise agrees. `claims.container_held` names whatever still holds it:
  `aq doctor --check claims.container_held`.
* **Reopened or multi-commit tasks.** An older generation's close — or a
  cherry-picked copy of its commit — cannot satisfy the current
  completion generation. `task explain` keeps the dependent withheld until
  the *current* generation's exact source proves containment.
* **Misleading legacy rows.** Journal rows are history. Even a row that
  says `delivered` for the wrong source cannot release a dependent:
  readiness, pool demand, claim and explanation all agree with the fresh
  git answer, and an external ancestry-preserving merge releases work on
  the next snapshot without any adoption.
* **Zero fallback inventory.** After the provenance migration applies to a
  fully migrated fixture, `aq integration migrate-provenance <project>`
  reports `fallback_count == 0` (`zero_fallback: true`). An unlabelled
  generation is still unknown and is named with its task id rather than
  guessed; that is the bridge working, not a failure.

### Recorded acceptance evidence (2026-09-27, commit `de5e2a7ee`)

The plan's scenario matrix is covered by the deterministic regressions
named in [Source and tests](#source-and-tests), run in a disposable
environment — none of the live tasks above were touched, and the running
daemon was not restarted or migrated:

* Focused: `aq test tests/test_development_integration.py tests/test_lifecycle_cleanup.py`
  — 259/259 passed (eleven-way stall; depth-two/depth-three conflict
  dispatch; adopted repair cycles; missing refs and archived tasks;
  reopened/partial provenance; fifth-tick escalation with one supervisor
  message; concurrent adopt/publish under the publisher lock;
  receipt-free admission; stale container settlement; the
  `development_deliveries` SQL ratchet).
* Combined area: `aq test tests/test_development_integration.py tests/test_development_validation.py
  tests/test_doctor_integration_checks.py tests/test_integration_controls.py
  tests/test_blocked_state.py tests/test_claim_commands.py tests/test_lifecycle_cleanup.py
  tests/test_hierarchy_settlement.py` — 607/607 passed, zero failures, no
  baseline-failure comparison needed.
* Disposable swarm smoke (side-by-side kit, `AQ_E2E_HOME`/`AQ_E2E_PORT`/
  `E2E_DB_NAME` overridden; the default kit's home was already owned by
  another checkout): `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh`
  — 19/19 scenarios passed, including S15 development delivery with an
  unlabelled generation withheld until an exact one exists.

No new test module or unowned source path was introduced, so no selection
catalogue or rule regeneration was required.

## Related pages

* [Integration](../concepts/integration.md) — the mechanism and its guarantees.
* [Integration troubleshooting](integration-troubleshooting.md) — when a
  delivery is stuck.
* [Switching integration modes](integration-migration.md) — moving to or from
  the strict modes.
* [Migrations](migrations.md) — who may run Alembic against which database.

## Source and tests

[`src/integration/development.py`](../../src/integration/development.py),
skip stalls in
`src/integration/development_stalls.py` (retired),
retargets and settlements in
`src/integration/development_settlement.py` (retired),
commands in
[`src/commands/integration_commands.py`](../../src/commands/integration_commands.py),
CLI in [`src/cli/integration.py`](../../src/cli/integration.py).

```bash
aq test tests/test_development_integration.py
```
