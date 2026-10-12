# Mergeable generated metadata

Task: `smart-current-36`.

GitHub uses ordinary text merges and does not run AQ's custom generated-file
merge driver. Unrelated source edits must not conflict merely because their
generated outputs share a changing aggregate hash or count.

Selection catalogue schema/generator version 2 omits the committed global
digest. Each module record carries an integrity hash of its path, metadata,
area descriptions, discovery roots and versions. Membership remains checked
in both directions between modules and areas. Adding another module does not
rewrite existing module hashes. The loader computes the full catalogue digest
in memory for selection records, cache identity and promotion freshness. It
continues to validate version 1 historical Git blobs against their original
global digest, preserving base ownership and replay identities.

Full regeneration comparisons, named-module freshness checks and CI checks
remain authoritative. Per-record hashes detect hand edits before use, including
in historical blobs without a source tree. Schema versions prevent silently
interpreting a missing digest as an older format.

## Audit of every generated merge path

| Artifact | Aggregate metadata | Change |
|---|---|---|
| `tests/selection_catalogue.json` | Global catalogue digest | Compute in memory; commit module hashes |
| `docs/reference/cli-command-inventory.json` | Global and category counts | Compute with `inventory_counts`; commit records only |
| `docs/reference/playbook-commands/README.md` | Registered command count | Omit from index; generator still reports counts |
| `docs/reference/configuration-schema.json` | None; model schema | No change |
| `docs/reference/promotion-flow-schema.json` | None; model schema | No change |
| `src/playbook_v2_schema.json` | None; model schema | No change |
| `src/tools/command_catalogue.json` | None; definitions and categories | No change |
| `openapi.json` | Stable API version; no generation timestamp/count/digest | No change |
| `packages/aq-client/**` | Stable boilerplate; definitions from OpenAPI | No change |
| `scripts/aq-client-boilerplate.sha256` | One hash per boilerplate file | No change |

The CLI inventory schema becomes version 2. Its record validation and
acceptance-status assertions remain intact; totals are derived on demand.

Regression coverage creates two branches from one base, adds different test
modules in separate sorted positions, runs the actual catalogue generator on
each branch and performs plain `git merge` with no custom driver. Every
generated artifact is present; the merged catalogue must equal fresh generation
from the combined sources. Ordinary conflicts from adjacent insertions or edits
to the same record remain possible and still use the existing regeneration
path. CI workflows, required checks and the PR gate are unchanged.
