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
# exits 2: unsupported until K08/K09 integration is implemented
```

Exit 0 means all requested offline contract checks passed; exit 1 means an invalid
manifest, observation, or invariant; exit 2 means unsupported integration.
Reports have no clock, random IDs, host-dependent versions, or elapsed timings.
They report synthetic selected/citation counts and conservative UTF-8 input-token
bounds only. ContextBundle integration is explicitly unsupported, model quality
is unmeasured, and these checks do not certify installed harnesses or local models,
retrieval precision/recall, live task correctness, cost, latency, or release gates.

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
`scripts/evaluate-knowledge.py`. K09 (`vivid-quest-44.2`) can load the module with
`runpy.run_path` or `importlib` and supply a trusted offline adapter. Adapter
inputs exclude `expected` and `input_hashes`; adapters must never read the oracle
to select records. The snapshot adapter reads the explicit `observation` input;
an integrated adapter should ignore that snapshot and use its isolated K08 setup.

An integrated adapter must normalize actual K08 observations into the schema:

- `selected`: ordered exact record/revision/kind, excerpt, SHA-256, evidence,
  authority and freshness labels. Do not invent selections or verified status.
- `rendered` and `rendered_sha256`: the actual common knowledge Markdown,
  including its wrapper and trust labels, identical across harness labels.
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
No credentials, network calls, plugin activation, production data or migrations
are needed. Final K09 acceptance still requires real K08 adapter integration and
the planned harness-delivery tests; this independent runner does not satisfy it.
