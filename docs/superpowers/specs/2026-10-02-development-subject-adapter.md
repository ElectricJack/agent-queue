# Development decision table and incremental adapter

Task `agile-harbor-62.3`, implementing `rev-agile-ridge` revision 2,
SHA256 `5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`,
§5.6 and phase 3. This ships the policy/adapter seam; it does not activate a
project, change the operator database, remove an engine, or replace recovery.

## Frozen project policy

`development_policy.render_development_policy(project_id, DevelopmentPolicy)`
renders the installed settings into a disabled project-owned source. Every
setting is retained: validation mode, command list, separate queue/run budgets,
cadence, cap, generated rebuild command and its timeout. The template lives in
`src/integration/development_policy_template.md`; it is review input, not an
installed system playbook. The caller uses the existing V2 proposal/review/import
workflow. No unreviewed source is seeded into the vault.

`PinnedDevelopmentPolicy(definition, source)` verifies the exact source digest,
project scope, artifact identity and literal decision table. Runtime loads it
by the subject's immutable artifact SHA; current project settings cannot change
an in-flight subject. The source pins local validation commands/budgets, while
the compiled table pins the decisions. Per-project edits still go through review.

The table has root-batch and source rules. Completed pushed sources need no
review approval or PR. They enter in dependency order; the cap counts admitted
members. A held, failing, unpushed, unknown or parked member withholds its
dependents while independent members continue. Frozen source heads/bases are
never replaced with live branch tips. A new completion/head is eligible for a
new subject; it does not rewrite an existing manifest.

## Validation and the exact-green invariant

Focused conclusive red repairs the aggregate. Advisory red keeps its real red
outcome and does not file a code repair; `none` submits no validation. Both
reach an explicit no-default publication/evidence gate when they have no green.
They cannot manufacture success or weaken `git_publish`'s exact-SHA trusted
green requirement. This is a conservative difference from the installed old
publisher, which can publish advisory failures or skip validation. The project
supervisor explicitly confirmed this resolution on 2026-10-02 (message
`msg-9c69b71ba4ce4efc997efd4a0764a781`). It must remain visible in rollout review.
Existing trusted evidence on the current exact head can satisfy a no-validation
policy; a gate answer alone cannot grant publication authority.

Local checks use `LocalCIProducer` and the existing detached job runner. Request
and observation occupy separate visits; every request starts at most the next
sequential finite preset. Pending/missing checks cannot turn into green.
Distinct infrastructure observations select a new explicit attempt identity;
they neither file a code repair nor consume a repair generation. The shared
plan now accepts the installed zero-second queue budget, as the job service
already does. Local green does not create App attestation.

## Adapter and operator handoff

Construct `DevelopmentIntegrationAdapter` through the existing command owner:

- `observe` is the shared read-only observer with existing project/gate/writer
  and remote facts.
- `frontier_for` uses the existing completion provenance and delivery snapshot
  reader. Its `satisfied` set includes only proven containment, no artifact,
  explicit settlement or obsolete prerequisites. Exact parked revisions from
  existing operations and new source subjects belong in `parked`. Members carry
  recorded source bases and current per-member holds. Repair `carries` must be
  established by the existing repair-source contract **and actual ancestry**;
  a task-name link is insufficient. Preserve existing retarget settlements and
  `keen-stone-14` preserved-repair inventory/recovery in that reader.
- `policy_for` loads the retained reviewed definition and source by artifact SHA.
- `repository_for` supplies the existing retained clone with the pinned rebuild
  command/timeout; it must not use a worker checkout or substitute live sources.
- `shared_ports` binds shared Git, writer filing/leases/stop proof, gates,
  receipts/attempts and cleanup through the existing command handler. Supply
  `PublisherJobs(handler)` as `job_client`. Publication's trusted-green resolver
  checks the same exact head, generation and pinned local validation plan.

`adapter.reconciler()` returns the existing `IntegrationReconciler`, in shadow
by default. Install it at the existing single remote-pass boundary; do not run
a second loop. Membership, decisions, Git actions and parks use the existing
subject journal. A conflict creates one replay-safe source repair subject for
that exact revision/target, and leaves the root building. A verified replacement
repair can carry that source and release its descendants. Conclusive validation
failure retains the aggregate repair scope. A completed, proved replacement
waits for root delivery without spending another repair generation. Resumable
repair tasks are not expired for queue/provider waits; three generations exhaust to a named human
gate. Stop proof and unpublished-work preservation remain shared primitives.

`DevelopmentIntegration._sweep` checks the durable new subject **and matching
branch owner** while holding the same repository exclusion used by shared Git
publication. `publish` rechecks a late takeover before writing its legacy
journal. Shadow subjects, subjects without a writer, other projects and other
targets leave legacy delivery active. An expired or handoff-pending writer
still excludes legacy publication until its release is proved. Engine rollback
must use the existing fenced stop/preserve/release path under that exclusion;
do not flip ownership around a live or uncertain write.

Task `agile-harbor-62.4` owns real project scenarios, durable-state migration,
review/import and activation/rollback after the parent cutover gates. Bind the
snapshot/recovery ports above before activation. This task keeps the old engine
available and never activates a production subject.

## Verification

Focused checks: `aq test tests/test_development_subjects.py`.
Affected area: `aq test tests/test_development_*.py
tests/test_integration_ci_producers.py tests/test_integration_reconciler.py
tests/test_playbook_v2_integration_policy.py tests/test_selection_catalogue.py`.
Use the documented disposable PostgreSQL test service; no operator migration
is required. Regenerate the selection catalogue after adding the test module.
The branch retains published prerequisite ancestry for the shared observer,
reconciler and compiler, which were absent from its initial source base.

## Reviewed source loading correction (2026-10-04)

Compiled PlaybookDefinition objects do not contain the source Markdown. Import,
review approval and save-and-compile retain verified source next to the immutable
artifact as a separate Markdown file. Runtime verifies its source digest against the
pinned artifact before reading Development settings. Older imports may load installed
project source only when it passes the same digest, scope and policy checks; a changed
vault source cannot replace the reviewed settings. Retained source lets an in-flight
subject continue after later vault edits.
