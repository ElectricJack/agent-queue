# Development integration

Turn on AQ's batched delivery for a project, watch a batch reach your default
branch, and read the journal it leaves behind.

This is the delivery path AQ is configured to use on this repository. Workers
push ordinary task branches and close with the checks they actually ran; the
daemon collects finished branches, merges them, validates the result once, and
publishes it. There is no pull request, no hosted-CI receipt chain, no
per-parent verifier and no squash in this path.

A `blocks` dependency on a completed code task stays blocked until the delivery
journal confirms its completion revision on the configured default branch.
Preserving a candidate or publishing a parent aggregate does not release the
successor. Publication updates dependency state automatically; a later completion
at a different revision needs its own delivery. Branchless tasks have no code
artifact to publish.

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
  --command '/path/to/venv/bin/python -m pytest -q' \
  --interval-seconds 300 \
  --reason 'Batched development delivery for demo'
```

The command returns `{"outcome": "configured", …}` with the stored policy, and
the same decision is written into the delivery journal, where it stays. This is
the real configuration row this repository's own project carries, read back
with `aq integration status agent-queue`:

```text
{
  "state": "adopted",
  "target_ref": "refs/heads/main",
  "manifest": [],
  "evidence": {
    "kind": "configuration",
    "operator_id": "local:-",
    "policy": {"validation": "focused",
               "commands": ["~/.agent-queue/operator-checks/isolated-tests.py tests/test_development_integration.py"],
               "timeout_seconds": 300, "interval_seconds": 300, "max_batch_size": 50}
  },
  "reason": "Steady-state development: five-minute batches and focused integration checks; …"
}
```

(A configuration row carries an empty manifest and is journaled in state
`adopted`: it records a decision, not a publication.)

What that did, all in one transaction
([`DevelopmentIntegration.configure`](../../src/integration/development.py)):
set the project's mode to `development`, stored the policy, cleared any drain,
bumped the project's integration generation, suppressed the older
review-and-merge automation for this project, and wrote a `configuration` row
into the delivery journal recording who asked and why.

Options:

| Option | Meaning |
|---|---|
| `--validation focused` | Default. Every `--command` must exit 0 or the batch parks. At least one command is required. |
| `--validation advisory` | Runs the commands, records failures, publishes anyway. |
| `--validation none` | Runs nothing; the journal records `not_run`. |
| `--command` | Repeatable. Runs under `bash -c` in AQ's retained clone, with the daemon's environment. |
| `--interval-seconds` | Periodic recovery sweep interval. Default 300. Task completion also requests a sweep on the next integration cycle (normally within 5 seconds, once an active batch finishes). |
| `--reason` | Required, and kept in the journal. |

> **Note.** Validation commands do not run in a worker's worktree. They run in
> AQ's own clone under `<data_dir>/development-integration/…`, with whatever
> is installed for the daemon's user. Point at an absolute interpreter or a
> small wrapper script rather than assuming a virtualenv is active. Each
> command is bounded by a 300-second timeout (`timeout_seconds`, up to 3600);
> a timeout is recorded as exit code 124.

Policy changes take effect on the next batch. There is no drain to wait for.

## Watch a batch happen

`aq integration status <project>` reports development publication receipts and
pending writes. It does not require GitHub App bindings or strict train rollout
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
  "state": "delivered",
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
| `parked` | A batch was assembled but validation failed. | Read the evidence; fix, or retry — see below. |
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

Journal row states are `prepared`, `publishing`, `delivered`, `parked`,
`adopted` and `cancelled` (`development_deliveries` in
[`src/database/tables.py`](../../src/database/tables.py)). Rows are never
deleted: the journal is the audit trail for what reached your default branch.

## What a worker sees

A worker in a development-mode project is primed with a shorter contract than
in the strict modes ([`src/prime/sections.py`](../../src/prime/sections.py)):

> Commit and push your task branch, run focused local checks, and close with
> actual evidence. Ordinary commits and merges are accepted; no squash, PR,
> hosted CI, or parent verifier is required. The daemon collects completed
> source branches and publishes validated batches to main. Do not push main
> yourself.

Two consequences worth knowing as an operator:

* **A closed task is not a delivered task.** Delivery happens on the next
  sweep, and can park.
* **Stacked work needs declared dependencies.** The sweep orders members by
  their dependency edges, and skips a dependent whose prerequisite parked. Work
  stacked only by branch topology, with no edge, is not ordered for you.

## When something conflicts

A member that will not merge is parked with its conflict output as evidence,
and the rest of the batch continues. A real parked row — two documentation
tickets editing the same generated manifest:

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

After the batch is assembled AQ looks at each parked row again:

* if a later member already brought the same content to `main`, the row is
  marked `adopted` and nothing else happens;
* otherwise AQ files one ordinary repair task, `development-repair-<digest>`,
  on its own branch, describing the parked sources and the base to resolve
  against. It is a normal queue task with three retries; waiting for a worker
  does not expire it. At most three generations of repair are chained before
  the content is left parked for you.

Parked content is not re-tried while its sources are unchanged — a worker
pushing new commits makes it eligible again by itself. To retry unchanged
parked content under the current policy:

```bash
aq integration sweep demo --retry
```

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
* Each task's branch must be an ancestor of that SHA. For a squashed or
  hand-rewritten equivalent, add `--accept-equivalent` after reviewing the
  content; the manifest then records `"acceptance": "operator_equivalent"`
  instead of `"ancestry"`.
* No selected task may still have a worker or a live session, and no child of a
  selected task may still be open.
* Tasks are closed leaf-first, with a completion record whose verification
  reads *"Operator adoption; not CI attested"*. The evidence is recorded as
  `operator_accepted` — never as a CI result.

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

Delivery journal rows are deliberately retained: they are history, not runtime
state. See [switching integration modes](integration-migration.md) for what
does and does not carry over.

## Related pages

* [Integration](../concepts/integration.md) — the mechanism and its guarantees.
* [Integration troubleshooting](integration-troubleshooting.md) — when a
  delivery is stuck.
* [Switching integration modes](integration-migration.md) — moving to or from
  the strict modes.
* [Migrations](migrations.md) — who may run Alembic against which database.

## Source and tests

[`src/integration/development.py`](../../src/integration/development.py),
commands in
[`src/commands/integration_commands.py`](../../src/commands/integration_commands.py),
CLI in [`src/cli/integration.py`](../../src/cli/integration.py).

```bash
aq test tests/test_development_integration.py
```
