# Object-loop refactor comparison evidence

`baseline.artifact.json` is an unchanged copy of the supervisor-supplied reviewed
artifact from `.aq/inputs/object-loop/artifact.json` for task `fair-cascade-93`.
Its canonical SHA-256 is
`e1630040e3ffaebb52ba917826bfdf1755e05d20410e2690d5b500bc31fec27a`.
It is historical test evidence, not an importable or active policy bundle.

The supplied source's raw file hash is
`3c447d950409d6f03ef2f9de5d86a3ebdbf721b8d760ea845eae507fdcc758c5`.
AQ's normalized `source_digest` is
`0a1b96a900549bf840821eda167e0228c4c08711a8586261bf9cd015c093aa85`,
matching the supplied manifest and artifact. Compiling from the task-local
source path changes source references and `compiled_at`, so it cannot reproduce
the artifact's exact hash. The active Matter policy was not inspected.

`past-budget-trace.json` preserves the complete calibrated-budget stop trace
from the supplied `traces.json`. That archive names an earlier artifact,
`e9a2e756d3698ad04210529ed9f6a0571015bbdf603a85003f71109301ebb0a0`;
the refactor does not relabel it as evidence from the new artifact. Its captures
are synthetic, and it stores command/outcome and traversal summaries rather
than original command arguments/results.

`tests/test_object_loop_playbook.py` reconstructs that budget scenario, compares
its command/outcome sequence with the archive, and dry-replays every newly
recorded fixture round through the new graph and the historical graph. Both
must issue exactly the same commands in order with the same arguments. Replay
supplies typed recorded results and never calls a live command adapter. Only
occurrences of the policy's own hash are remapped for the historical graph,
so its exact-policy eligibility checks see their original identity. No other
candidate, review, reference or artifact digest is rewritten.
