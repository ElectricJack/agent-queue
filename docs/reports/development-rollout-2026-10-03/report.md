# Development scenarios and per-project rollout: evidence and operator handoff

Task `agile-harbor-62.4`; approved design `rev-agile-ridge` revision 2, SHA256
`5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`, §6 phase 3.
Contract:
[development scenarios and rollout](../../superpowers/specs/2026-10-03-development-scenarios-and-rollout.md).

This is **disposable test evidence**. No production project was transferred, no
reconciler flag was changed, no operator database was migrated and no daemon was
restarted by this worker. The identities below come from a real bare Git origin
and a real disposable PostgreSQL database, not from a fabricated receipt fixture.

## What passed

- **Parked member.** Four completed sources; two collide on one file and a third
  depends on the colliding one. Sealing admitted members in dependency order (the
  independent member ahead of the dependent), the merge parked exactly the
  colliding revision in the shared journal and filed the existing repair for it,
  the root kept building and published the independent work, and the parked
  source and its dependent stayed undelivered with their branches intact.
- **Validation failure.** Two sources whose merged aggregate failed the pinned
  focused check on a real detached snapshot. The conclusive red is preserved as
  evidence for the exact head, the red candidate never reached the default
  branch, the existing repair was filed with the aggregate scope, its worker
  merged the exact frozen revisions and published a fix keeping both sources
  ancestors, and only the rebuilt green aggregate was published.
- **Transfer serialization and unresolved publications.** A publisher holding
  the shared repository engine lock blocked the audited transfer for as long as
  it ran, and a transfer holding the exclusive lock blocked a publisher; the
  rollback direction serialized the same way. An unresolved publish intent
  refused cutover and rollback; a confirmation of an earlier intent did not hide
  a later prepare; confirming the newest intent released both directions.
- **Durable state, migration and rollback.** One batch the old engine had landed
  and one it had parked were read, never rewritten: the landed revision counted as
  satisfied only because the target still contains it, the parked revision was
  withheld, and only the still-owed work was merged and delivered. The audited
  rollback returned the project to `legacy` with both operation rows, the subject
  and every undelivered source branch intact.

All four ran zero operator, recovery, redrive, rebind or settle commands, and
every journalled action followed its committed decision for the pinned artifact.

## Exact disposable lineage

[scenario.json](scenario.json) is captured from the passing rollback test's JUnit
`rollout_evidence` property.

| Evidence | Identity |
|---|---|
| Reviewed development artifact | `sha256:d9bc7cdccd997b6275f79612c3b41f56d24cf61d6e1ad56d2c83c638e2682be8` |
| Subject | `development-subject-aq-dev-repo-sha256-d9bc7c…2682be8` |
| Subject version before rollback | `13` |
| Subject version after rollback | `14` |
| Engine after rollback | `legacy` |
| Published default-branch SHA | `50df6ce166afe7e384f9e363a8173dde579c2507` |
| Fixture base SHA | `ccd804930c9f95e88db526fa64fee94f7ed43360` |
| Retained source branches | `aq/golf`, `aq/hotel` |
| Collected source branch | `aq/india` (backed up and logged before deletion) |
| Transfer operator | `human:local-operator` (fixture identity) |

The `production_authorization` field is `null` on purpose: this specimen supplies
no production authorization.

## Operator handoff

Per project, after the parent cutover gates:

1. Render the project source from its installed `DevelopmentPolicy`, then review
   and import it through the existing V2 workflow so the project policy pins its
   own artifact.
2. Set `integration.reconciler_shadow: true` and restart. The seeded subject stays
   `legacy`; shadow visits journal mirrors and mutate nothing.
3. Compare the shadow journal against the legacy decisions over the recorded
   window (`aq integration shadow-report`), then transfer with the audited
   per-project engine transfer, recording exact subject versions, artifact
   digests and evidence.
4. Rollback is the same transfer with `engine: legacy` and the then-current exact
   versions. Ownership is durable in the subject rows, so feature-off alone does
   not return authority.
5. Durable state migrates read-only. Legacy operations, receipts, gates, source
   branches and the `keen-stone-14` preserved-repair inventory stay in place while
   the old engine remains available.

## Verification

Focused (disposable PostgreSQL on `localhost:5534`):

```bash
aq test tests/test_development_scenarios.py -q -p no:xdist
```

5 passed, including the concurrency regression. Both fixes are
mutation-verified: reverting the exclusive fence makes the serialization test
fail (`the transfer completed while a publisher held the fence`), and reverting
the exact latest-intent correlation to a subject-level answer makes the
unresolved-publication test fail (`DID NOT RAISE`).

Affected area:

```bash
aq test tests/test_development_*.py tests/test_integration_reconciler.py \
  tests/test_integration_root_scenarios.py tests/test_integration_ci_producers.py \
  tests/test_integration_gitops.py tests/test_playbook_v2_integration_policy.py \
  tests/test_selection_catalogue.py -q
```

Plus `ruff check` on the changed paths, `git diff --check`,
`python scripts/generate-selection-catalogue.py` and
`scripts/regenerate-generated.sh --check`. [checks.json](checks.json) records
these. No whole-suite run, no operator migration, no production flag change and
no task adoption was performed by this worker.

## Known limits

- The scenarios use the fixture project's own identity and one disposable
  database; the configured development projects named in the design
  (`matter-engine-cpp`, `agent-queue-web`) were **not** activated or migrated.
  Adopting them is an operator action with its own recorded evidence.
- Local validation is exercised through the real job submission path, real
  detached snapshots and a real finite preset, but the detached runner process
  itself is driven synchronously by the fixture rather than launched and adopted.
- A second failing rebuild inside one subject files the next repair generation
  over the members the frontier still owes; the third reaches the table's named
  human gate. Only the first generation's adoption is covered by a scenario.