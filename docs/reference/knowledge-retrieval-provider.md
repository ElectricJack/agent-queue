# Optional knowledge retrieval provider boundary

`src/knowledge/providers.py` is the core K12 contract slice. It does not register
or load plugins, assemble context, write index receipts, cite evidence or deliver
prompts. Final K12 owns service registration and the external aq-memory adapter;
K08/K09 own context selection, aggregate budgets and delivery. This slice does
not satisfy the G5 measured retrieval pilot gate.

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
or changes a revision hash. Final K12 must bind index receipt/manifests to the
exact revision when implementing chunk indexing; chunk excerpts cannot be
introduced through this boundary without a core-owned manifest validator.

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
aq test tests/test_knowledge_retrieval.py tests/test_knowledge_provider_absence.py
```

Adapter registration, derived index receipts, the aq-memory implementation and
the lexical/no-memory/semantic quality comparison remain final K12 deliverables.
Any feature activation, paid generation or G5 pilot needs its existing separate
approval and evidence.
