# Offline knowledge evaluation contract (v1)

Run the synthetic golden set:

```bash
python scripts/generate-knowledge-fixtures.py --check
python scripts/evaluate-knowledge.py --output /tmp/knowledge-report.json
aq test tests/test_knowledge_evaluation.py
```

The default adapter replays recorded synthetic observations and checks them against
an independent expected oracle. It exercises the evaluation contract without K08.
It implements no context selection, authorization, budget gate, ContextBundle,
persistent citation or delivery state. All incident fixtures, including the Azure
incident reproduction, are synthetic; no original incident artifacts are certified.
The negative directory contains sealed observations that must fail:

```bash
python scripts/evaluate-knowledge.py tests/fixtures/knowledge/negative
# exits 1
python scripts/evaluate-knowledge.py --adapter context-bundle
# exits 2: no disposable --db-url, so there is nothing to integrate against
```

## The integrated set (`integrated/`)

`integrated/` is replayed against the **real K08 service**, in a disposable
database the runner recreates and drops:

```bash
python scripts/evaluate-knowledge.py --adapter context-bundle \
    --db-url postgresql+asyncpg://user:pw@localhost:5532/aq_knowledge_scratch
# exit 0 on pass; add --keep-db to inspect the rows it wrote
python scripts/evaluate-knowledge.py --adapter local-model \
    --db-url postgresql+asyncpg://user:pw@localhost:5532/aq_knowledge_scratch
aq test tests/test_knowledge_harness_delivery.py
```

`--db-url` must name a scratch database. Worker sentinels, the maintenance
database and the operator's configured database are refused before any
connection. These checks are the only thing here that touches a database, and
`tests/test_knowledge_harness_delivery.py` runs the same adapters through the
leased test databases instead.

Exit 0 means all requested offline contract checks passed; exit 1 means an invalid
manifest, observation, or invariant; exit 2 means the integration could not run
(no disposable database, or a database this runner must not touch).
Reports have no clock, random IDs, host-dependent versions, or elapsed timings.
They report synthetic selected/citation counts and conservative UTF-8 input-token
bounds only. Model quality is unmeasured, and these checks do not certify
installed harnesses or local models, retrieval precision/recall, live task
correctness, cost, latency, or release gates.

## Manifest and observation boundary

`manifest.schema.json` uses JSON Schema 2020-12 and format version 1.
Each manifest includes independent `records`, `expected`, `observation`, frozen
`delivery_rules`, `budget`, and requested harness labels. Every top-level field
except `input_hashes` is sealed by SHA-256 of canonical JSON (sorted keys, compact
separators, ASCII escaping). New or missing section hashes fail. To deliberately
edit fixtures, change the authoring script and regenerate; tests mutate temporary
copies and explicitly reseal only when testing the observation oracle.

The evaluator exports `FixtureAdapter.observe(inputs, *, harness, role) -> dict`
and `evaluate_manifest(manifest, adapter=None)` from
`scripts/evaluate-knowledge.py`. Load the module with `runpy.run_path` and supply
a trusted offline adapter. `tests/knowledge_fixture_adapter.py` ships two:
`ContextBundleFixtureAdapter` drives prepare, thin harness delivery,
acknowledgment and citation readback against the real service, and
`LocalModelFixtureAdapter` does the same for a CLI with neither hook nor prompt
channel. Both are seeded from the manifest's own `records` and read no oracle.
Adapter
inputs exclude `expected` and `input_hashes`; adapters must never read the oracle
to select records. The snapshot adapter reads the explicit `observation` input;
an integrated adapter should ignore that snapshot and use its isolated K08 setup.

An integrated adapter must normalize actual K08 observations into the schema:

- `selected`: ordered exact record/revision/kind, excerpt, SHA-256, evidence,
  authority and freshness labels, plus `verification` and `lifecycle` when the
  record seals them. Do not invent selections or verified status.
  `content_sha256` is the hash of the *sealed* revision: an integrated fixture
  declares `revision_sha256` and the oracle compares the adapter's reported hash
  against it, while a snapshot fixture declares nothing and the oracle hashes
  the excerpt it rendered. `evidence` is a retained artifact source id, which the
  rendered payload therefore carries verbatim.
- `rendered` and `rendered_sha256`: the actual common knowledge Markdown,
  including its wrapper and trust labels, identical across harness labels. It
  must contain every selected excerpt and name what it carries — the composite
  identity (snapshot renderers) or the sealed revision hash (K08 renderers).
- `omissions`: internal identity/reason pairs. They are consumed by the oracle
  and excluded from reports. Unauthorized title/body/snippet/count/error markers
  must never occur elsewhere in output, including metadata or diagnostic strings.
- `owner`: the requested execution owner kind/ID and claim epoch.
- `deliveries`: observed bundle/transport/key/state/new receipts for each input
attempt. The runner compares them and detects duplicate new receipts; it does
  not create, acknowledge, retry, or deduplicate a delivery.
  The input `delivery_rules.events` provides transport attempts and acknowledgment
  outcomes to replay; expected deduplication decisions remain in the hidden oracle.
- `citations`: ordered exact record/revision/content-hash pins, injected kind
  and owner, one per selected item when delivery is observed. Prepared, failed,
  and unknown delivery observations create no injected citations.
- `usage`: `upper_bound_bytes:utf8`, with tokens equal to UTF-8 bytes of the
  entire rendered knowledge plus `reserved_tokens`, and bytes plus
  `reserved_bytes`. Reserves represent required prompt content/output allowance
  already measured by the owning adapter. No characters/4 or pretend tokenizer.
- `diagnostics`: `context.required_over_budget` when required reserves alone
  exceed a cap. In that case knowledge, rendering and citations must be empty;
  the report says `required_over_budget`, never claims a met total hard cap.
- `metadata`: additional observed output checked for forbidden markers and
  harness parity. Keep provider/harness wrappers outside the common payload.

Owner and bundle IDs are synthetic fixture identities, never production attempt
IDs. Worker and supervisor fixtures with the same authorized payload demonstrate
role parity without treating role/harness labels as access grants. The runner
requires schema-valid output, exact selected bytes and labels, citations, omissions,
aggregate byte/token caps and common output across Claude/Codex/OpenCode/local
labels. Smaller-window, recycled-slot and redacted-history scenarios check
observations; production reauthorization and lifecycle transitions remain K08/K09.

Failing socket/DNS/URL/process spies surround every adapter invocation; swallowed
refusals still fail, and exceptions/arguments/private markers are never echoed.
This is a guard for trusted test code, not a sandbox for hostile adapter code.
No credentials, network calls, plugin activation or production data are needed;
the integrated adapters add a disposable schema and nothing else. These checks
still measure no retrieval precision/recall, no live task correctness, no cost
or latency, and no installed harness or local-model quality: they bound the
selection, budget, delivery and citation contract and nothing beyond it.
