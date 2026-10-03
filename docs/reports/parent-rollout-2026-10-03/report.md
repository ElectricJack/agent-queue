# Parent rollout evidence and operator handoff

Task `amber-meadow-15.5`; approved design `rev-agile-ridge` revision 2,
SHA256 `5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`.
Implementation source base: `5c962782ca17a69fe1bfe04269848f052988c65d`.
Contract: [parent rollout evidence](../../superpowers/specs/2026-10-03-parent-rollout-evidence.md).

The disposable scenario passes: an eight-child epic with a failed child and a
real `shared.txt` merge conflict reaches one named human gate without a redrive
or rebind after adoption. Reconstructing the runtime retains the gate. A human
hold remains binding through restart and explicit transfer to legacy. The
original receipt and conflict intent are unchanged, and the default branch is
unchanged. This is test evidence; no production parent has been transferred by
this worker.

## Exact disposable lineage

[scenario.json](scenario.json) is captured from the passing test's JUnit
`rollout_evidence` property. These identities come from real Git publication and
PostgreSQL rows, rather than a fabricated receipt fixture:

| Evidence | Identity |
|---|---|
| Reviewed parent artifact | `sha256:5c6c36ceb9be0cea2addf4edd07fbd13bd64d03b6b384f090429b923fa0b13a9` |
| Subject | `parent-subject-ba6b70f9ac3a59349b3e067d04de131d` |
| Original episode | `0b1934fd-f9b7-44d7-92aa-cafc6dd24805` |
| Gate | `gate-8fc3704059de4b84` |
| Receipted aggregate | `2937271713c914e14245e46cac2a04d9e722e7e7` |
| Adoption input subject version | `2` |
| Rollback input subject version | `13` |

The specimen includes the child's reviewed source head, the before/after target
heads, operation/episode binding, committed and conflicting intent identities,
shadow decision, transfer operator/reason/evidence, human gate answer projection
and final subject. Its `fixture-*` approval references are disposable inputs;
they supply no production authorization.

## Verification

The final focused run passed **26 tests**. It covers:

- Eight children, one failure, one conflict, one gate; existing stage 13 reaches
  a gate without creating another repair stage.
- Fresh runtime and promotion service after interruption before/after remote
  push; the original intent yields one immutable receipt by exact read-back.
- Restart after writer filing but before operation linkage; replay links the
  same ordinal before leasing it.
- Existing `keen-stone-14` failed-aggregate recovery, then new child fixes in
  the same episode, preserving receipts and advancing the writer ordinal.
- Exact trusted green completion; a wrong head or failed check cannot complete.
- Stale-version and unresolved-write refusal for adoption/rollback, and fresh
  legacy collection after an explicit rollback.
- Legacy collection, readiness, CI and repair exclusion for adopted subjects.
  Feature-off retains durable authority.
- Open gates, expired dashboard projections and explicit human holds remain
  binding after rollback; a verified retry permits legacy compatibility.

Focused command (with the documented disposable test DSN on localhost:5534):

```bash
aq test tests/test_integration_parent_reconciler.py -q \
  -o junit_family=xunit1 --junitxml=/tmp/amber-meadow-15.5-focused.xml
ruff check src/integration/parent_engine.py tests/test_integration_parent_reconciler.py
git diff --check
```

The initial test attempt with an unset `POSTGRES_TEST_DSN` exited 4 before
collection; it is not counted as verification. The final focused log is
`/tmp/amber-meadow-15.5-focused.log`.

The affected area run passed **190 tests**, including disposable migration
upgrade/replay and the shared root publisher/ownership checks:

```bash
aq test tests/test_integration_parent_subjects.py \
  tests/test_integration_parent_completion.py \
  tests/test_integration_cancelled_collection.py \
  tests/test_parent_ci_publication.py tests/test_integration_records_and_gates.py \
  tests/test_integration_root_adapters.py tests/test_migration_parent_subjects.py \
  -m 'not perf and not slow and not tmux and not integration' -q \
  --junitxml=/tmp/amber-meadow-15.5-area.xml
```

This intentionally enables the relevant migration tests against disposable
PostgreSQL, while retaining the other slow-marker exclusions. Both runs emit
the existing `pkg_resources` deprecation warnings. Their JUnit totals and log
digests are retained in [checks.json](checks.json). No whole-suite run was
performed.

## Production prerequisites and adoption

**Production gates are pending.** The supervisor clarified that this task ships
disposable tests, the rollback gate fix and this runbook; its worker close does
not await or claim production transfer or cutover.

The P1 reporting code is deployed on `main` at `33bfd2c89`, as confirmed by the
supervisor. The live initial report at
`/home/jkern/.agent-queue/operator-checks/shadow-report-20261003-initial.md`
has report digest
`sha256:b83fe5d919dc5758785c632f89b6c1fde017f0fb1f8c907507175548b87d256e`.
It correctly blocks cutover: zero recorded shadow observations, zero roots
compared, zero reconciler-owned subjects and a recorded span of zero seconds.
Its requested window is 5,279 seconds of the required 604,800 seconds. Both
`integration.reconciler_active` and `integration.reconciler_shadow` remain OFF;
no shadow week has been observed or waived. This initial report supplies no
adoption approval.

The supervisor's task inbox records the intended observation window as
2026-10-03 07:20 UTC through 2026-10-10 07:20 UTC, referring to
`docs/reports/2026-10-03-integration-stall-recovery.md` on the deployed checkout.
Elapsed time alone does not establish recorded coverage. The inbox also names
missing candidate-publication and ownership-release policy routes identified
by P1; their reviewed resolution and live evidence remain production
prerequisites. This parent scenario does not claim to supply those routes.

The supervisor owns production operations. The following receipts remain
required before adopting production parents:

1. Exact P1 root cutover receipt and reviewed shadow comparison, including the
   required shadow-week evidence from revision 2. Dependency completion alone
   is not an engine-transfer receipt.
2. Operator-applied additive migration `a00000000058` (and subsequent required
   revisions), with read-only `aq db current` evidence. The disposable migration
   replay test does not prove the operator database has been upgraded.
3. Import/review of the parent's exact episode-pinned artifact. A new project
   activation does not replace an existing episode's pin; an older episode
   without a compatible parent table remains legacy.
4. Reviewed scenario evidence and explicit human approval for each selected
   parent. Record repository, task, episode, artifact and complete subject map.

Once those prerequisites are satisfied, the supervisor previews each parent:

```bash
aq integration engine-transfer <repo> --parent-task-id <parent> \
  --engine reconciler --json
```

Apply that exact complete version map, repeating `--expected-subject` for every
entry. The active loop must already be installed and enabled:

```bash
aq integration engine-transfer <repo> --parent-task-id <parent> \
  --engine reconciler --apply --expected-subject <subject>:<version> \
  --reason 'approved parent adoption' --evidence <root-cutover-reference> \
  --evidence <shadow-comparison-reference> --evidence <scenario-reference> \
  --evidence <human-approval-reference> --json
```

Retain the preview, accepted response and audited journal entry, including the
input versions and resulting subject versions. Verify live visits progress or
retain the named gate. A changed map/version or unresolved remote write requires
a fresh observation; settle ambiguous publication under its original owner and
intent before transferring authority. Existing repository publisher exclusion
and exact trusted green before default-branch publication remain in force.

## Rollback

Preview the same parent with `--engine legacy`, then apply its fresh complete
version map and a reason. Unresolved writes still refuse the transfer. Inspect
the preserved episode, artifact, receipts, writer fence/ordinal and human gate.
An open gate or human hold continues to block legacy mutation; resolve a gate
through the verified human surface according to its pinned choices.

The active/shadow flags are shared with root integration. Leave the active loop
enabled when roots or other parents still belong to the reconciler. Disabling
it alone stops visits but does not grant legacy authority. A full feature-off
rollback first transfers every affected subject through its supported ownership
surface, then changes flags and uses the operator's session-preserving restart.

No redrive/rebind command is needed to progress the adopted scenarios. The
existing recovery commands and legacy data are retained for rollback and
unadopted episodes; deletion belongs to P4 after its live counts prove safe.
