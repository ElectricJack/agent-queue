# Optional knowledge retrieval provider boundary

`src/knowledge/providers.py` is the core ranking contract, and this page is the
whole optional-retrieval surface: the contract, local registration in
`src/plugins/services.py` and `src/knowledge/registration.py`, derived index
receipts and the core-owned chunk manifest in
`src/knowledge/index_receipts.py`, the legacy aq-memory adapter port in
`src/knowledge/memory_port.py`, and the offline arm comparison in
`src/knowledge/benchmark.py`. K08/K09 own context selection, aggregate budgets
and delivery; K13 owns extraction. Nothing here satisfies the G5 measured
retrieval pilot gate, and nothing here authorizes an activation.

Construct `KnowledgeRetrieval` with the existing `KnowledgeService`, the live
`MemoryConfig`, and an optional synchronous `provider_lookup`. The lookup must
only inspect an already registered service locally. It must never import a
plugin, start Milvus, initialize embeddings or perform network I/O. Keeping the
legacy memory plugin installed does not authorize its initialization. The
existing knowledge and memory configuration defaults remain off.

`search(principal=..., project_id=..., query=..., limit=..., semantic=False)`
defaults to the ordinary lexical path. An explicit semantic request requires
core knowledge enablement for the authorized scope, `knowledge.semantic.enabled`
and `memory.enabled`. Missing authorization fails before provider lookup.
The principal must be derived by the command owner, never from request fields.
Only a semantic adapter that has separately passed its outbound/cost policy may
be registered. No provider or actual-model evaluation is authorized by this
contract.

The provider implements the async `RetrievalProvider.search(RetrievalRequest)`
method. Its request has the bounded query, core-derived `scope_key`, an
`include_shared_global` hint and `limit` (1–100). Shared-global eligibility still
requires a current grant for each returned record. Use a fixed managed index;
there is no caller-controlled collection name, filesystem path, role or model
scope. Return a list or tuple of at most `limit` references in relevance order:

```python
RetrievalReference(
    record_id=record_uuid,
    revision_id=exact_revision_uuid,
    chunk_id="index-chunk-7",
    score=0.82,
    provider_version="adapter-v1",
)
```

UUIDs are mandatory exact identities. Scores must be finite numbers; chunk IDs
and provider versions are nonempty printable strings of at most 128 UTF-8 bytes.
Extra fields, including snippets, text, scope, verification and authority, are
rejected. The core validates even preconstructed reference objects. A chunk ID
is opaque ranking metadata only: it never selects served text, becomes a source
or changes a revision hash. Index receipts bind to the exact revision through the
core-owned manifest validator below, and chunk excerpts cannot be introduced
through this boundary, because a manifest may not carry text.

`hydrate_references(references, principal=..., project_id=...)` accepts at most
100 references. Each hit goes through exact core reads with current scope,
session, capability, source and redaction checks. Serving semantic metadata
requires both `knowledge_search` and `knowledge_show`. Only current, active,
undisputed revisions are suggestions. Staleness dates remain labeled by core.
Invalid, unknown, foreign-revision, private, revoked, redacted and superseded
hits are omitted; duplicate chunks produce one summary. Provider ordering and
bounded ranking metadata accompany core identity/hash, safe title/summary,
category, tags, verification, freshness and current authority annotation.
Provider text is never exposed. No rejected identities, counts or exception
messages appear in fallback diagnostics.

`read(reference, principal=..., project_id=...)` delegates to the exact core
revision read without provider lookup. It preserves authorized historical,
retired or disputed evidence and its labels; it never substitutes a newer
revision after reindexing. Permission revocation and tombstones still apply,
and inaccessible source/link descriptors remain filtered by core. Ordinary
`KnowledgeService.show` remains the explicit read API without ranking metadata.
Consumers must reauthorize again at their later delivery or citation boundary;
a retrieved summary is not an authorization receipt.

Provider calls have at most two concurrent slots per boundary and a timeout
that includes waiting for a slot (default 0.5 seconds, maximum 5). No database
connection is held while awaiting ranking. Absence, outage, timeout, oversized
or malformed responses, and an empty eligible result cause a fresh authorized
lexical read. Disabling a switch during ranking also falls back. Cancellation
propagates. Core storage/authorization errors remain core errors; an optional
retrieval diagnostic never writes task state, changes an outcome or releases a
claim. Filtering and cursor pagination remain on `KnowledgeService.search`.

Verification uses isolated PostgreSQL records and fake providers only:

```bash
aq test tests/test_knowledge_retrieval.py tests/test_knowledge_provider_absence.py \
         tests/test_knowledge_index_receipts.py tests/test_knowledge_memory_adapter.py \
         tests/test_knowledge_retrieval_benchmark.py
```

The index receipt arm is a migration arm and stays deselected unless asked for:
`aq test tests/test_knowledge_index_receipts.py -m migration`.

## Local registration

A provider is a service a loaded plugin registered as `"knowledge_retrieval"`
(`ctx.register_service`), typed by `KnowledgeRetrievalService` and
`KnowledgeIndexService` in `src/plugins/services.py`. `RetrievalProviderRegistry`
reads that one registered object and nothing else: no import, no entry-point
resolution, no initialization, no health probe. It accepts the provider only when
`provider_id` matches `^[a-z][a-z0-9_-]{0,63}$`, `provider_version` is a
non-empty printable string of at most 128 UTF-8 bytes, and `search` is async;
otherwise the provider is absent and the lexical path answers. `available` is
read as local state, and a probe that raises is treated as absent.
`registered()` returns a versioned read-only `handshake()` for an operator to
inspect; nothing sets `knowledge.legacy_memory_mode` or any switch.

`require(provider_id)` is the receipt-side lookup and refuses with stable codes:
`knowledge.provider_unregistered`, `knowledge.provider_invalid_id`,
`knowledge.provider_unavailable`, `knowledge.provider_deprecated`. Selecting a
provider, paying its cost and retiring it stay explicit operator decisions, so
the reserved name holds one instance: core never races or ranks two providers.

## Derived index receipts and the chunk manifest

`record_index_state(provider_id, record_id, revision_id, sequence,
chunk_manifest_sha256, indexed_at, redacted_at)` (migration
`a00000000065`) is the rebuildable checkpoint of one provider's index of an exact
revision. `DerivedIndexReceipts` exposes:

- `hydrate_index_payload(record_id=..., revision_id=..., principal=...,
  project_id=...)` — the server-authorized `record_id`, `revision_id`,
  `sequence`, `content_sha256`, `hash_version`, `scope_key`, `title` and `body`
  of one revision. It reads through the ordinary authorized path, so no
  caller-chosen collection, path or record is reachable from it.
- `acknowledge(provider_id=..., manifest=..., principal=..., project_id=...)` —
  stores the core-computed digest. A provider may not supply a digest, a chunk
  text or any other field: the manifest is exactly `record_id`, `revision_id`,
  `content_sha256`, `hash_version` and `chunks`, and each chunk is exactly
  `chunk_id`, `ordinal`, `char_start`, `char_end`. Chunks must be a contiguous
  partition of the exact revision body — no gaps, overlaps, duplicates, padded
  labels or overlong labels — because anything else cannot be verified without
  excerpts. A manifest for another record, revision or byte string is refused
  with `record.invalid_input`, an acknowledgment at or below the stored
  sequence is refused with `knowledge.index_stale`, and the upsert itself
  refuses to move the checkpoint backwards.
- `acknowledge_erasure(provider_id=..., record_id=..., revision_id=...)` —
  `knowledge.index_not_redacted` unless the exact revision's payload is already
  gone, then it stamps `redacted_at` (`erased`, or `never_indexed` when there was
  no derived chunk). Redaction is one-way: a redacted revision cannot be
  acknowledged again, because the authorized read it needs has been withdrawn.
- `lag(provider_id=..., limit=...)` and `pending_erasures(provider_id=...)` —
  bounded reads for an operator or a reindexer. Lag never gates a read: an
  unindexed revision is served by the lexical path today.

Receipts require core enablement for the authorized scope,
`knowledge.semantic.enabled`, the memory master flag and a registered,
non-deprecated provider; otherwise the write is refused with
`knowledge.disabled` and nothing is stored. `doctor`'s `records.index_lag` and
`records.redaction_cleanup` report per-provider `lag` and `erasure_pending`
counts, read from the receipts themselves, so doctor never initializes an
optional provider.

## The legacy aq-memory adapter port

`src/knowledge/memory_port.py` is the core side of the legacy semantic adapter:
`MemorySemanticPort` (or `legacy_adapter(...)`) wraps the plugin's own memory
service, which stays a separate repository owning embeddings, chunking and its
fixed Milvus collection. The port forwards the core-derived `scope_key` as an
opaque label for the plugin to map, keeps only rows naming an exact
`record_id`/`revision_id`/`chunk_id` with a finite score, re-validates them into
`RetrievalReference`, collapses duplicate chunks and caps the batch at
`request.limit`. Every legacy `summary`, `original`, `content` and tag field is
dropped by construction, and a row tagged for another scope is dropped rather
than trusted. A result that is not a list of rows is a core error
(`record.invalid_input`), which the boundary reports as `provider_unavailable`
and answers from a fresh lexical read. The port has no write attribute at all,
so `kv_set`, `fact_set`, `save_document`, promotion and consolidation are
unreachable from it; `handshake()` records that as `read_only: true`,
`authoritative_writes: false`, `extraction_fenced: true`.

Register it from the plugin as the `"knowledge_retrieval"` service, with the
identity bounds `RetrievalProviderRegistry` accepts. The plugin-side
implementation is not delivered here: this repository contains no aq-memory
checkout and the plugin is not installed, so embeddings, chunking and the
Milvus collection remain unproven work in that repository.

## The offline arm comparison (G5 pre-gate, not G5)

`src/knowledge/benchmark.py` compares no-memory, lexical and semantic over one
authorized corpus, the plan's declared scenarios and the declared metrics
(precision at 8, required-evidence recall, exact-citation rate,
retired/disputed/private selection rate, empty-bundle accuracy, duplicate rate,
added input tokens as a UTF-8 upper bound and latency), plus a
`lexical_fallbacks` count that keeps semantic degradation visible. The semantic
arm runs over a deterministic offline provider whose chunks come from the real
hydration and receipt path, so its reach is exactly what core acknowledged.

```bash
AQ_RETRIEVAL_BENCHMARK_OUT=/tmp/retrieval-arms.json \
  aq test tests/test_knowledge_retrieval_benchmark.py -k g5_is_not_satisfied
```

The report always carries `g5_satisfied: false` and `g5_missing_evidence`: a
real installed provider, at least three runs per arm with recorded variance, a
blinded reviewer-approved measured benefit, an explicit operator decision for
provider, scope and cost, and protected-scenario evidence from an authorized
retained artifact rather than only synthetic fixtures. Enabling a provider or
scope still needs its own approval.
