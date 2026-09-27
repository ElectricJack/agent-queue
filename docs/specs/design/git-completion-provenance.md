# Exact completion provenance in Git

Implements shared-contract items 4–6 of the approved
`projects/agent-queue/plans/2026-09-27-development-publisher-delivery-truth.md`.
This is a development-mode primitive. Publisher/consumer integration and the
removal of the legacy reader belong to the plan's later operations/retire tasks.

## Identity and retention

The immutable generation is the existing `task_completion_records.id`. A
completion binds project, repository, task, generation and the exact full final
source OID. `claim_epoch` supplements that identity; it does not replace it.
Ordinary `aq git commit` / `aq git commit-changes` messages carry `AQ-Task`.
`GitManager.acommit_all` also reads the worktree claim file. For direct shell
commits, use `git commit --trailer 'AQ-Task: <task-id>'`. Existing hooks run as
configured. A trailer identifies authorship, never complete delivery.

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
history is never amended. Code-free outcomes retain existing validation: a
proven empty, explicitly code-free Git source has `artifact:false`; branchless,
non-Git and vault-only outcomes keep their existing completion behavior.

## Complete replacements

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
original current completion/source. A legacy original with a full matching
completion OID can be retained during that close; ambiguous ones require migration.

An invalid repair contract, a missing passing immutable source completion, or
an unlabelled source without that matching final OID refuses close as an
operator blocker (`precondition:provenance_migration`). Completion recording
uses the same delivery-refusal path as pipeline verification: it retains the
live task, claim, session and workspace, sets
`needs_attention:delivery_provenance_migration`, and emits `task.needs_attention`.
The refusal names the operator remedy
`aq integration migrate-provenance <project-id> --apply`; inventory can report
ambiguous evidence that still needs operator resolution. Empty legacy commit
lists, including those with delivery-row source bindings, require that migration
instead of expanding the automatic bridge's authority.

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

Pages use `--limit` (1–1000, default 500), `--offset` and `next_offset`. The legacy
delivery inventory is bounded at 1000 rows. Apply never modifies or deletes old
rows. It expands only unique Git OID prefixes, binds explicit legacy
`completion_sources` to their exact completion ID, and verifies all objects.
Receipt state is never containment authority. Missing sources, conflicting
bindings, multiple matching generations and incomplete repair contracts are
reported in `ambiguous` with task IDs, generations and reasons.

Repair migration requires the full explicit source contract, unique original
and repair generations, a nonempty base/source pair, and current target ancestry
of the complete repair source. Missing generations outside a page are reported
instead of guessed. Repeating apply produces the same immutable objects/refs.
Legacy operator equivalence without an exact source generation or a nonempty
replacement base/source pair remains unmigratable and needs explicit new
operator evidence. Missing branch refs are never interpreted as empty work.
After all pages are reconciled, operations/retire can verify that their remaining
unlabelled inventory is empty before removing the compatibility reader.
