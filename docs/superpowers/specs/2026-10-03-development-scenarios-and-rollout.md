# Development acceptance scenarios and per-project rollout

Task `agile-harbor-62.4`, implementing `rev-agile-ridge` revision 2, SHA256
`5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`, §6 phase 3
acceptance. It builds on the
[decision table and adapter](2026-10-02-development-subject-adapter.md), the
[subject foundation](2026-10-02-integration-subjects-foundation.md), the
[policy tables](2026-10-02-integration-policy-tables.md),
[shared Git primitives](2026-10-02-shared-integration-git-primitives.md) and
[shared CI producers](2026-10-02-shared-ci-producers.md).

This task ships the seam and the evidence. It activates nothing: both
reconciler flags stay off, no production project is transferred, and the old
`DevelopmentIntegration` keeps every durable row and source branch.

## The seam

`src/integration/development_runtime.py` supplies what the adapter needs and
installs it at the existing single remote pass, beside the root and parent
subject loops on `IntegrationService`:

- `DevelopmentFrontierReader` builds the frontier from rows the old engine
  already wrote. A member is a checkpointed source task in the project whose
  recorded head is what its recorded branch actually holds — read through
  `RetainedGitReads` over the publisher's own retained clone, because a
  repository the integration engine created carries no base checkout and a
  checkout-bound port would answer `unknown` for every branch; its dependencies
  are the `task_dependencies` release edges (`blocks`), never provenance edges;
  `satisfied` is delivery receipts, retired `abandoned` proofs, and landed
  operations whose exact revision the target still contains; `parked` is the
  exact revisions the durable `development.operation` journal parked. A repair
  `carries` a parked revision only when the existing `development_repair_sources`
  contract names it **and** the repair's head really contains it — a task-name
  link is not proof.
- `DevelopmentTrustedGreen` is publication's exact-head green resolver over the
  pinned local plan: one exact head, one generation, one pinned plan version,
  conclusive green. A pending, missing or earlier-head receipt is never green,
  so no validation mode can weaken the shared publisher.
- `DevelopmentPrimitiveAdapters` binds the primitives Development-specifically
  over existing mechanisms: `writer_file` through the existing
  `DevelopmentIntegration.ensure_repair` filing (replay-safe through the
  existing repair identity), `gate` through the existing `gate_create` command,
  and `cleanup` through the existing backed-up, logged, lease-checked
  `delete_branches`. Git rows 3–7 come from the shared `GitOperations`.
- `DevelopmentWriterProjection` mirrors the filed repair's existing lifecycle
  onto the subject's durable writer and budget columns, because the adapter
  observes those columns rather than the task table. Every input is the existing
  substrate: the subject's own journal names the repair the `writer_file`
  primitive filed, and the task, session and workspace rows say what it has done.
  A terminal task with a live session or a locked checkout keeps its lease. A
  `merged` build recorded after the filing is the subject adopting that repair,
  which releases the lease but keeps the generation ordinal — that ordinal is
  what makes the third failing rebuild reach the table's named human gate.
- `DevelopmentSubjectRuntime` seeds from current project settings (a project
  needs a reviewed, imported `development` artifact pinning its scope), projects
  writers in one bounded page, wakes resolved gates and runs both reconciler
  loops. A seeded subject is `legacy` until the operator's transfer, so a
  shadow visit journals a mirror and mutates nothing.
- Publication and cleanup run under the *existing* repository publisher
  exclusion, which now names the exact subject it acts for: one publisher per
  repository holds across the cutover, and the ownership check admits the
  mutation only while that subject is a live root at the pinned engine version.
- `retain_candidate` keeps the subject's built candidate reachable in the
  retained store. The shared merge primitive pins a candidate locally and does
  not push before publication, while the existing `job_submit_integration`
  snapshot clones that store and checks the candidate out by SHA. A local
  retention branch makes that exact object visible to the snapshot; nothing is
  pushed, `HEAD` is never moved (the old engine reads it), and no fence or
  journal is written.
- `transfer_development_engine` is the audited per-project activation and
  rollback. Its only supported route is
  `integration_development_engine_transfer` (`aq integration
  development-engine-transfer`), preview by default and applied with
  `--apply` at the exact previewed versions; the Python helper is never an
  operator surface. It runs inside the *exclusive* repository engine fence — the
  same lock identity `publisher_exclusion` holds shared for a whole publication —
  so it waits for a publisher already in flight instead of racing it; row
  versions alone cannot fence a publisher that is outside any transfer's
  transaction. It requires exact subject versions, a reason and cutover
  evidence, and refuses an unconfirmed publish write in **both** directions: the
  reconciler must not adopt one and the old publisher must not resume one. That
  check correlates by intent key *and* journal order, so an intent confirmed
  earlier never hides a later prepare. It never deletes a subject, journal entry,
  operation row, branch or receipt; rollback is the same serialized transfer
  back to `legacy`, after which the old engine resumes the same durable state.
- `development_runtime_for` constructs all of it from existing orchestrator
  owners and returns `None` unless a reconciler flag is on.

## What the scenarios prove

`tests/test_development_scenarios.py` runs over a bare fixture origin (whose
default-branch ref log records every value it ever held) and a disposable
PostgreSQL database. The reconciler runs on the daemon's `IntegrationService`
remote pass beside a disabled legacy publisher. Only the forge and the detached
runner boundary are substituted: the retained clone, the shared Git primitives,
the existing repair filing, the gate command, the real `jobs` rows, the real
detached snapshots and a real finite `ruff` preset executed in them are the
shipped ones. The only operator action is the audited per-project transfer.

1. **Parked member.** Four completed sources where two collide on one file and a
   third depends on the colliding one. Sealing admits members in dependency order
   (the independent member is admitted ahead of the dependent); the merge parks
   exactly the colliding revision in the shared journal and files the existing
   repair for it; the root keeps building and publishes the independent work; the
   parked source and its dependent are never delivered and keep their branches.
2. **Validation failure.** Two completed sources whose merged aggregate fails
   the pinned focused check on a real detached snapshot. The conclusive red is
   preserved as evidence for the exact head and the red candidate never reaches
   the default branch; the existing repair is filed with the aggregate scope; its
   worker merges the exact frozen revisions, publishes a fix that keeps both
   sources ancestors, and only the rebuilt green aggregate is published.
3. **Transfer serialization and unresolved publications.** A publisher holding
   the shared engine lock in flight blocks the audited transfer, and a transfer
   holding the exclusive lock blocks a publisher, in both directions. An
   unresolved intent refuses cutover and rollback; a confirmation settles only the
   intent it names, so a later prepare stays visible; confirming the newest intent
   releases both directions.
4. **Durable state, migration and rollback.** One batch the old engine already
   landed and one it parked are read, never rewritten: the landed revision is
   satisfied (only because the target still contains it), the parked revision is
   withheld, and only the still-owed work is merged and delivered. An audited
   rollback returns the project to the old engine with both operation rows, the
   subject and every undelivered source branch intact.

All four assert the invariants: zero operator, recovery, redrive, rebind or
settle commands; every journalled action follows its committed decision for the
pinned artifact; publication happens only from an exact candidate the target
still holds; a failure is evidence, never a bypass.

## Activation, migration and rollback procedure

Per project, after the parent cutover gates:

1. Review and import the rendered project source through the existing V2
   workflow, so the project policy pins its own artifact.
2. `integration.reconciler_shadow: true` and restart. The seeded subject stays
   `legacy`; the shadow loop journals mirrors of current truth and mutates
   nothing.
3. Compare the shadow journal with the legacy decisions over the recorded window
   (`aq integration shadow-report`), then transfer with
   `aq integration development-engine-transfer PROJECT_ID --engine reconciler`,
   recording exact subject versions from the preview, artifact digests and
   evidence. The default is a preview; `--apply` is the only mutating spelling.
   The apply needs `integration.reconciler_active: true` and its restart first:
   a reconciler that may only mirror must never be handed the project, so the
   transfer is refused while the active loop is off.
4. Rollback is the same command with `--engine legacy` and the then-current exact
   versions, followed by a restart. Ownership is durable in the subject rows, so
   feature-off alone does not return authority; the transfer does.
5. Durable state is migrated read-only. Legacy operations, receipts, gates,
   branches and the `keen-stone-14` preserved-repair inventory stay in place;
   nothing is deleted while the old engine is available.

## Verification

Focused: `aq test tests/test_development_scenarios.py`.
Affected area: `aq test tests/test_development_*.py tests/test_integration_reconciler.py
tests/test_integration_root_scenarios.py tests/test_integration_ci_producers.py
tests/test_integration_gitops.py tests/test_playbook_v2_integration_policy.py
tests/test_selection_catalogue.py`.
Use the documented disposable PostgreSQL service; no operator migration is
required and no production flag is changed. Regenerate the selection catalogue
after adding the module.