---
name: variation
description: Ten fresh seeds plus bounded presets under an approved object's suite.
vars:
  object_id: {required: true}
  candidate_sha256: {required: true}
  render_profile_sha256: {required: true}
  artifact_uri: {required: true}
  view_set: {required: true}
  seeds_a: {required: true}
  seeds_b: {required: true}
  presets: {required: true}
---
# Frozen generator variation suite

Create one ordinary container directly under the object epic and cook with
`--parent SUITE_ID`. AQ checks the exact current approved checkpoint, attaches
its review gate, records experiment publication exclusions and refuses a second
suite for this object. Each seed variable is a JSON array of five distinct
integers; presets is a finite JSON object with at most 16 entries and 16 KiB. Every
leaf materializes the approved candidate bundle artifact rather than rebuilding
it, and captures under the attempt's pinned render profile and editor build:
submit each capture with `--attempt-id` carrying that attempt id, and read the
retained bundles and earlier round branches of this object as in scope. The suite consumes the existing
final-suite reserve, never a new object allowance. Install before the terminal
stop. Reconciliation waits for all its children, including exhausted failures.
Use an admitted finite Matter job/GPU lease and atomic submit-and-wait when
AQ-4 is operational; otherwise retain an audited bounded native invocation.
Human review uses a closed evidence task and review gates, never a worker wait.

```aq-graph
version: 1
parent:
  title: "Variation suite for {object_id}"
defaults:
  task_type: research
nodes:
  - key: seeds-a
    title: "Object {object_id}: fresh seeds A"
    description: |
      Materialize {artifact_uri}; verify candidate SHA-256 {candidate_sha256}.
      Evaluate seeds {seeds_a} with view set {view_set}, render profile
      {render_profile_sha256}. Use at least three directions per seed.
      Verify reload, finite geometry, family identity and diversity. Record
      every failure, immutable capture URI/hash and actual/unknown costs.
      Do not edit source, scorer, references or budgets; do not publish assets.
    acceptance: ["All five seeds reported, with failed captures left unscored."]
    labels: [object-experiment, object-variation]
  - key: seeds-b
    title: "Object {object_id}: fresh seeds B"
    description: |
      Materialize {artifact_uri}; verify candidate SHA-256 {candidate_sha256}.
      Evaluate seeds {seeds_b} with view set {view_set}, render profile
      {render_profile_sha256}. Use at least three directions per seed.
      Verify reload, finite geometry, family identity and diversity. Record
      every failure, immutable capture URI/hash and actual/unknown costs.
      Do not edit source, scorer, references or budgets; do not publish assets.
    acceptance: ["All five seeds reported, with failed captures left unscored."]
    labels: [object-experiment, object-variation]
  - key: presets
    title: "Object {object_id}: boundary presets and clean reload"
    description: |
      Materialize {artifact_uri}; verify candidate SHA-256 {candidate_sha256}.
      Test bounded presets {presets} over the complete view set {view_set},
      render profile {render_profile_sha256}, with fresh-process reload and
      second-light evidence. Preserve locked-test failures; any subsequent edit
      requires new evidence and approval. Keep immutable receipts, resource
      limits and unknown costs explicit. Use only the reserved final budget.
    acceptance: ["Every declared boundary and reload result is retained."]
    labels: [object-experiment, object-variation]
```
