# Immutable observation snapshots

The approved implementation-playbooks building-blocks specification (2026-10-12,
rev-smart-flare r1, C2) adds opt-in paging to `doctor_run`, `list_projects`, flat
`list_tasks`, and `get_stuck_tasks`. Existing calls keep their live behavior,
including the default flat list's cap on completed tasks and the `read` outcome
of `get_stuck_tasks`.

## Calling the commands

Start with the usual selection/filter arguments plus `pagination: "snapshot"`
and optionally `page_size` (1–500, default 500). The initial result contains an
empty row collection, a compact `identity_manifest`, source `coverage`, and the
complete finite `page_tokens` array. Each token descriptor has `kind` and
`row_count`; doctor uses separate `checks` and `findings` kinds. Request each
page with the **same original arguments**, `snapshot_id`, and its `page_token`.
There is no next-page query and no producer callback on continuation.

For example, a flat task scan starts with:

```json
{"project_id": "p", "status": "BLOCKED", "pagination": "snapshot", "page_size": 100}
```

Subsequent calls add the returned snapshot ID and one page token. Repeating a
page returns the same rows, timestamps, coverage, and expiration after live
tasks or diagnostic data change. A page-size or filter change is a different
request and is rejected. Tree and compact display modes reject snapshot paging.
All filters are applied in SQL before bounded task materialization. Dependencies,
when requested, include only blocking edge kinds and are read with their upstream
statuses in the same transaction. A project scan refuses dependencies outside
that project with `snapshot.source_scope_mismatch`.

`complete` describes source coverage and representability, **not whether the
caller traversed every page**. A consumer must combine this with its own traversal
receipt before resolving a prior incident. Read-only database scans use PostgreSQL
REPEATABLE READ, including related stall-watch holds, sessions, and agents. Live
route explanations and doctor probes are observations over the reported
`interval_started_at` / `interval_finished_at`; `observed_at` remains the producer's
reference time.

## Limits and failures

Normal pages have at most 500 rows and 192 KiB of serialized JSON. The initial
manifest has at most 10,000 identities; every command envelope stays below
262,144 bytes, including typed default fields. Bounded source scans reject excess
rows or retained source bytes. They never silently truncate or mark a failed scan
as a complete empty observation.

A single oversized row is kept whole as an encrypted inspection artifact. Its
reference and snapshot owner appear in `artifacts`; normal page reads reject its
token. Authorized internal inspection uses `PrivateValueService.read(reference,
owner=PrivateOwner(**owner), principal=current_principal)`. Doctor's existing
`CheckResult.data` is preserved byte-for-value, not split into invented checks;
independent findings can still be paged. This result has `complete: false` and
`snapshot.row_oversize`. Oversized manifests/envelopes and quota admission failures
return explicit incomplete diagnostics without committing a partial snapshot.

Snapshots expire 30 minutes after materialization. Page reads never extend TTL.
Tokens are random opaque locators bound to snapshot, producer and version,
normalized request, effective scope, and current authority fingerprint. Current
capabilities, session identity, playbook run, key availability, expiry, and quotas
are checked again when reading. A token grants no authority, including for
LOCAL/SERVICE private artifact reads. Missing snapshots/pages, reduced grants,
changed scope or run, expired snapshots, and missing encryption keys fail closed.

## Storage and reuse

`SnapshotService` in `src/observations/snapshots.py` accepts bounded named row
collections, public compact identities, coverage, completeness, reference time,
and public metadata. D1/state callers can use the same service with their own
producer/version and identities containing state keys and revisions. Metadata
and identities must contain only public routing/receipt fields; page bodies are
protected by A3's configured private-value encryption service.

Revision `a00000000096` adds JSONB manifest/page tables and a `snapshot` private-value
owner kind. Manifest, all page rows, and encrypted private values commit in one
transaction. A PostgreSQL advisory transaction lock serializes admission against
logical serialized-byte quotas: 64 MiB per playbook run, 256 MiB per effective
scope, and 2 GiB per host database. Run/scope/host totals count all stored snapshots, including expired payload
awaiting collection, so admission cannot outrun cleanup; reads recheck the totals. There is no eviction of unexpired evidence. Daemon
housekeeping calls the operator/service-only `observation_snapshot_cleanup`
command at most once per minute, deleting at most 100 expired snapshots plus
their page rows and encrypted values in one transaction.

## Deliberate compatibility changes

Only these three existing typed execution fingerprints change. Paging arguments
and receipt fields are additive; `list_tasks` also declares `rejected` for an
incomplete snapshot. Doctor's C1 schema already reserved the opt-in inputs.
The new cleanup command adds one catalogue entry.

| Contract | Previous fingerprint | C2 fingerprint |
| --- | --- | --- |
| `get_stuck_tasks` | `sha256:a173738202a943b63548d3a808e03378341825a551fb5bb1774d1582ece7fa00` | `sha256:4c8954faa5a68c26288d33fefd84876649e5ec4df050614f8a60f6bfb544b46e` |
| `list_projects` | `sha256:c9a22e0fc26c61d2c33e0989fbf8f4f6beba7b429f225ca70a6e7762ed163d0a` | `sha256:50db7ab8cbe97b54fac0fbfdef01eacb9b90d367526469016e81899e51971c07` |
| `list_tasks` | `sha256:b3acb362f394bf55d871ce44f6f7b93f4d9a9152612319ffe8432ad66dac9888` | `sha256:6f4227faba6e74f07f46e7ee2caafebdb9455f41028fe15cc9d6e50c507eef26` |

The original `legacy-command-descriptors.json` evidence is unchanged. The separate
`observation-contract-changes.json` records exact before/after execution and
transport evidence, and descriptor tests apply it only to these three names.
The only reviewed bundle referencing them is `stuck-task-watch`; its source,
steps, and `read` transition are unchanged. Its `get_stuck_tasks` contract pin,
artifact digest, and manifest were rebuilt and copied to the shipped bundle.
The two `review-pipeline` test artifacts also pin the new `list_tasks` contract
and route `rejected` to `sweep-failed`; their exact before/after hashes are in
the same compatibility record. Their dashboard graph fixture is regenerated.
Generated command docs/catalogue, CLI inventory, OpenAPI, Python client and
TypeScript client are regenerated from sources.

Focused behavior is covered by `tests/test_observation_snapshots.py`; migration
round trips are in `tests/test_migration_playbook_building_blocks.py` and require
the migration marker and disposable PostgreSQL. Run the focused and area checks
through `aq test`; operator database migrations remain operator-only.
