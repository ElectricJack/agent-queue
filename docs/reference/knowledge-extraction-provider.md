# Optional knowledge proposal generation

K13 captures durable completion summaries and exact current knowledge revisions
into scoped retained artifacts. `src/knowledge/capture.py` owns capture;
`extraction_store.py` owns job leases, receipts and independent budgets;
`extraction.py` generates pending, unverified proposals. Generation never
accepts a proposal, sets verification, grants authority or writes guidance.
Consolidation pins the exact base revision and preserves its links and evidence.
The existing proposal decision path remains the only way to apply the change.

Deployment is inert. Core knowledge, writes, the memory master and the selected
feature must all be explicitly enabled for an allowlisted project, with
`knowledge.legacy_memory_mode: disabled`. Both `knowledge.extraction` and
`knowledge.consolidation` have these independent settings:

```yaml
enabled: false
provider_id: ""
allowed_providers: []
daily_microusd: 0
daily_tokens: 0
policy_version: "1"
```

Daily budgets use UTC days and integer microUSD/tokens. Task execution, retrieval,
extraction and consolidation counters are separate. An empty provider selection,
missing loaded adapter or zero allowance retains inputs for later processing.
Changing the source policy or outbound provider fences old jobs before transfer.
Sensitive input checks reject credential patterns before calling the provider.
Source hashes, tombstones, exact scopes and consolidation source permissions are
checked again before admission and proposal publication.

The daemon has a separate start/stop hook and wake-only bus subscriptions.
Each dispatch reconciles at most two projects, rotates through the allowlist,
reads eight persisted events plus two missing completions per project, and
captures at most two eligible consolidation revisions per project. It advances
the event cursor only in the transaction that retains the exact input receipts.
It ignores event payload text; completion summaries remain hypotheses. A missing
completion creates an explicit `source_unavailable` quarantine receipt.
Completion identity, attempt and ranges are canonical, so late or duplicate
events and reconciliation scans converge even after event retention. Completion
inputs use event ID zero; their durable completion ID is the evidence identity.

Workers lease at most two jobs per dispatch, with one in flight per project
across both features. Calls contain at most eight identically authorized inputs,
8000 input tokens and eight outputs. Core uses UTF-8 byte length as a conservative
input-token bound; a local estimate must cover it. The 15-second request timeout
fits inside the 30-second lease. Generation does not run in the scheduling cycle
or block task completion, claims, integration or capacity accounting.

## Loaded provider contract

A separately approved plugin registers one local `knowledge_extraction` service
implementing `KnowledgeExtractionService` from `src/plugins/services.py`:

- `provider_id`: `^[a-z][a-z0-9_-]{0,63}$`, matching the selected allowlisted ID.
- `provider_version`: nonempty printable text, at most 128 UTF-8 bytes.
- `available`: local availability; `deprecated` must not be true.
- `estimate(request)`: synchronous, local upper bound, exactly
  `{"microusd": int, "tokens": int}`.
- `generate(request)`: async, returns only `outputs`, `actual_microusd` and
  `actual_tokens`. Each output has `title`, `body`, `category` and optional
  `summary`/`tags`. Verification, authority, source, link and scope fields are
  refused. Core supplies retained sources and unverified classification.

`ExtractionRequest` includes a stable operation ID, feature, exact project scope,
bounded inputs and limits. Each input preserves its source identity, ordinal,
attempt/event identity, evidence type and line range. The registry only reads an
already loaded service; it never imports a plugin, initializes a legacy watcher
or probes a remote provider. Partitioning includes actor, permissions, source
policy and outbound provider as well as scope, so a batch cannot inherit the
first item's role.

`MemoryExtractionPort` in `src/knowledge/generation_port.py` wraps the external
aq-memory package's stateless proposal callback and local estimator. The callback
receives `request.wire()` and returns the response above. Do not wrap a legacy
extractor that owns buffers, guidance writes or promotions. The port has no
memory store, event subscription, KV or acceptance method. This repository does
not contain or install the separate aq-memory package: its backend integration
and real-model quality remain untested. Deterministic fakes verify the core port.

## Accounting, replay and diagnostics

Admission reserves money and tokens and commits the stable provider operation ID
before an external request. Returned output is retained before proposal writes.
Saved output replays without provider lookup or another charge; deterministic
output keys, all proposals and job success commit atomically. Actual usage settles
once, including when proposal content is rejected. Unretainable content with
valid usage reports still records the charge. Provider overages remain visible
and open a persistent circuit; five consecutive provider failures also open it.
A restart or UTC-day change does not clear an open circuit.

A timeout, cancellation or expired paid operation without saved output has an
unknown outcome. It enters quarantine, keeps the reservation and never retries
the paid operation automatically. There is no exactly-once paid-call claim or
operation-status recovery support in this version. Resolving unknown charges or
resetting a circuit requires a later explicit operator recovery workflow; no
worker command guesses the provider's result.

`aq knowledge generation-status` is a local-operator-only, content-free view of
job states/reasons, the latest 100 daily budget rows, persistent circuits and
unknown reservation count. It never looks up a provider. The internal/local
`knowledge_generation_tick` command is exposed as `aq knowledge generation-tick`
for an explicit bounded dispatch. Both have closed empty argument schemas;
session/supervisor principals cannot supply source data or run these commands.

Migrations 63 and 64 add job/budget persistence and the failure counter. The
imported K12 index receipt migration is chained at 65 to avoid the existing
validator-restore revision 61. Downgrades refuse retained data; the operator
owns production migrations and read-only rollback.

Focused verification uses disposable PostgreSQL and fake providers:

```bash
aq test tests/test_knowledge_capture.py tests/test_knowledge_extraction.py \
        tests/test_knowledge_extraction_store.py
aq test tests/test_knowledge_migrations.py tests/test_knowledge_index_receipts.py \
        tests/test_knowledge_extraction_store.py tests/test_knowledge_extraction.py -m migration
```

These checks establish replay, accounting and protection behavior. G6 still
requires explicit outbound-provider/cost approval, authorized real evidence and
measured quality before an operator activates a one-project extraction pilot.
