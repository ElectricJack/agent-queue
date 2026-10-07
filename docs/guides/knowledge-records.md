# Knowledge records

Keep durable findings as versioned knowledge, and cite them from executable tasks.
Agents use the [aq-knowledge skill](../../src/skills/aq-knowledge/SKILL.md);
[aq-cli](../../src/skills/aq-cli/SKILL.md) provides command discovery.

## Why it exists

A task tracks work that can be claimed, completed and delivered. Knowledge retains
what that work established: an incident, procedure, decision or reference. Knowledge
never enters task readiness, capacity or progress. Core stores and searches it with
every optional memory provider absent; see [src/knowledge/service.py](../../src/knowledge/service.py)
and [src/records/search.py](../../src/records/search.py).

## Vocabulary

A **record** identifies either a task or knowledge. A **revision** retains an exact
knowledge snapshot, including its sources and outgoing links. A **proposal** is an
unaccepted correction. Verification and authority require separate authorized
actions; a recent or frequently retrieved finding gains neither. A **managed source**
is a legacy item whose write ownership has moved to core through explicit import.
Its compatibility fence survives flag rollback and deletion of the original file.

## A realistic example

Prerequisites: a running daemon and grants for the intended project. Choose that
project explicitly for every operation; `agent-queue` below is an example. A
global supervisor can target a project without changing its session scope.
Global records require `--global-scope`, separate enablement and grants; omitting
a project does not select global scope. Discover current capabilities and search
for an existing record before creating a duplicate:

```bash
aq record capabilities --project-id agent-queue --json
aq record search --project-id agent-queue --kind knowledge --query 'External tool' --json
```

Read `data.capabilities.enabled`, `writes_enabled`, the feature flags and
`granted_operations`: configuration and principal grants are independent.
Capabilities describe the current installation, not the shipped defaults or a
historical observation. Discovery activates nothing. If storage or writes are
disabled or refused, report the limitation; a save request does not authorize
feature enablement or a silent fallback to a legacy note.

With core writes enabled for the scope and the required grants, create a
reference. This is a template; substitute the actual researched body, URL,
observation time and a stable idempotency key for the request:

```bash
aq --json knowledge create --project-id agent-queue \
  --title 'External tool reference' \
  --body 'What the tool does, why it may help this project, limitations, and source links.' \
  --category reference --tags '["external-tools"]' \
  --sources '[{"source_id":"upstream","kind":"url","url":"https://github.com/owner/repo","observed_at":"2026-10-07T00:00:00Z","retained":false}]' \
  --idempotency-key 'reference-owner-repo-2026-10-07'
```

URL sources require `source_id`, `kind: url`, `url`, a timezone-bearing
`observed_at` (use UTC), and an explicit `retained` boolean. `retained: false`
records a consulted URL without claiming an archived artifact; `true` requires
the retained `artifact_id`. Other source kinds have different required fields.
The CLI's generic `ARRAY` label does not describe this deeper source contract;
see [Source](../../src/knowledge/models.py).

The create receipt supplies `record_id`, `knowledge_alias` and `revision_id`.
Reuse the key only to replay the same request. Read back the title, body, sources
and revision before reporting the save:

```bash
aq --json knowledge show --project-id agent-queue --identity 'record:<returned record_id>'
```

Use `record:<UUID>` or `knowledge:<kn-alias>`; a bare `kn-...` alias or
`knowledge:<UUID>` is invalid. Cite the identity and revision, not an export
path. Use `knowledge show --revision-id` for an exact historical read, and
`knowledge update --if-revision --idempotency-key` for a guarded edit. A stale
base returns a conflict; reread and reconcile before updating. Knowledge starts
unverified. A worker can propose a protected correction, while verification,
authority, proposal acceptance and erasure require stronger grants. See
[src/records/auth.py](../../src/records/auth.py) and
[src/commands/knowledge_commands.py](../../src/commands/knowledge_commands.py).

## Inputs and outputs

Knowledge accepts a title, original body, category, tags, optional summary,
sources and namespaced metadata. Bodies preserve Unicode and newline bytes.
Mutations use an expected revision and idempotency key; responses include stable
identity, revision and content hash. History retains the old snapshots. Retirement
requires a reason, and record-level restore creates a new revision from retained
history, clearing verification; it does not restore a database backup. Redacted
history returns a typed unavailable result. It never substitutes the current body.
See [src/knowledge/models.py](../../src/knowledge/models.py).

`aq record search --kind task|knowledge|all` performs local lexical discovery.
Informational links can cite an exact knowledge revision. They do not become task
dependencies or change the execution graph. The mixed graph is implemented by
[src/records/graph.py](../../src/records/graph.py).

## State ownership

PostgreSQL owns installation identity, records, immutable revision envelopes,
payloads, links, receipts, protections and ownership mappings. Vault exports are
derived Markdown; an edited export is marked diverged and is not overwritten.
Retained import artifacts live under the confined `record-artifacts/` namespace.
Back up the database and the vault together. See
[src/records/export.py](../../src/records/export.py) and
[src/knowledge/imports/apply.py](../../src/knowledge/imports/apply.py).

Legacy project notes, memory files and edited exports are not canonical graph
records and do not prove ingestion. Search for the canonical record before
assuming a file was imported. For a requested reference, create the missing
record with sources and verify its receipt/readback. Keep existing files;
bulk import remains an explicit operator workflow. Optional semantic retrieval
and extraction are separate from the core store and lexical search.

[src/config.py](../../src/config.py) ships core, writes, UI, import, export,
context, semantic retrieval, extraction and consolidation disabled. The project
allowlist is empty; global scope requires explicit enablement. Core storage
works with `memory.enabled: false`. Context and provider paths require the
memory master as well as their own feature flags and approved scope. An
operator crosses the applicable release gate before enabling each one. Plan
approval, completed tasks and a passing test never activate these controls.

## Refreshing agent guidance

`src.vault.ensure_default_aq_skills` scans `src/skills/*/SKILL.md` and installs
missing skills into Claude, Codex, npm Gemini and Snap Gemini discovery paths
on daemon startup. Claude is seeded unconditionally; the other targets require
their harness homes to exist. Existing copies are write-if-absent and preserve
customizations. After deploying the source change, an operator can inspect and
deliberately repair drift without restarting the daemon:

```bash
aq doctor --check skills.installed_drift
aq doctor --check skills.installed_drift --fix
```

The repair backs each differing copy up as `SKILL.md.bak` and copies shipped
text. Review drift first and reconcile desired custom edits from the backups.
Missing copies are seeded on normal startup; the drift check reports differences
in existing copies, not missing installations. Workers do not repair shared
harness directories or manage the daemon.

Profiles are also seeded write-if-absent. `aq doctor --check profiles.system_drift`
reports installed worker-template and supervisor differences. Preserve customized
profiles by merging the relevant role/rules text into the vault copy; for an
intentional full replacement use `aq agent profile-reseed --profile-id <id>`,
which backs up the file and syncs the replacement. `--grants-only` merges
capabilities but does **not** refresh role/rules. The supervisor's automatic
capability sync is additive and likewise does not refresh prose. Do not edit
derived worker rungs or expand grants to repair discovery. Prime tool guidance
comes from the current source template rather than a copied profile.

## Common failures and recovery

| Symptom | Read or action | Meaning |
|---|---|---|
| `knowledge.disabled` | `aq record capabilities --project-id <project> --json` | Ask the operator to check the approved scope and flags; workers cannot activate them |
| `record.forbidden` | Inspect `granted_operations` and requested project | A grant does not confer another project's scope |
| `record.revision_conflict` | Read the exact current revision and propose a reconciled edit | Replaying with a new key does not resolve a stale base |
| `memory.migrated_read_only` | Use canonical knowledge update with an expected revision and key | The legacy source has one permanent core writer |
| `source_diverged` on a managed note | Read the canonical record and retain the differing source for operator review | The filesystem edit is not an automatic knowledge revision |
| `record.revision_redacted` | Retain the typed tombstone; consult the erasure ledger | Import replay or an old backup cannot authorize resurrection |
| An edited or missing export | Operator runs `aq doctor --check records.exports` | Repair the projection without discarding retained history |

The [deprecation and restore runbook](knowledge-deprecation.md) explains the
operator evidence behind these decisions. No live import, paid provider call,
adapter removal or source deletion was performed for this increment.

## Related pages

- [Resumable knowledge import](knowledge-import.md) — sealed selection, backups,
  replay receipts and ownership cutover.
- [Knowledge deprecation and restore](knowledge-deprecation.md) — compatibility
  telemetry, synthetic restore proof and the explicit G7 decision.
- [Migration and restore runbook](knowledge-migration-restore.md) — the complete
  operator backup, recovery and staged rollout procedure.
- [Knowledge context](../specs/knowledge-context.md) — common bundles, observed
  delivery, citations and shared budgets behind G4.
- [Extraction provider contract](../reference/knowledge-extraction-provider.md) —
  durable unverified proposals, independent budgets and G6 approval.
- [Database migrations](migrations.md) — disposable test databases and the
  operator-only production upgrade boundary.
- [Reviews](reviews.md) — approval evidence remains owned by the review system.

## Source and tests

[src/knowledge/deprecation.py](../../src/knowledge/deprecation.py) retains
content-free compatibility attempts and checks deprecation evidence.
[src/doctor/record_checks.py](../../src/doctor/record_checks.py) exposes the
informational `records.deprecation` report. The report never approves G7.

With the separate disposable PostgreSQL test service available and
`POSTGRES_TEST_DSN` configured, run:

```bash
aq test tests/test_knowledge_deprecation.py tests/test_knowledge_compatibility.py \
  tests/test_record_doctor.py
```
