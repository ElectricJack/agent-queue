# Object-loop bundle: review and activation handoff

AQ-3 ships a mechanically validated, inactive policy for `matter-engine-cpp`,
plus the `object` and `variation` formulas. It does not establish operational
readiness for autonomous generation. The approved proposal is Matter review
`rev-amber-zenith`, revision 2; artifact approval and activation are separate.

The recorded artifact is
`sha256:08fc137f2499a67f7b577f1a438c38c6e8ca1ec424dc6933a17f79cd3bcb0acd`.
Its source, canonical artifact, contract/grant manifest, compiler diagnostics
and live/dry traces are in `src/prompts/reviewed_playbooks/object-loop/`.
The byte-identical test recording is in `tests/fixtures/playbooks/v2/object-loop/`.
`traces.json` records real AQ commands against disposable PostgreSQL, followed
by write-free preview replay with identical command arguments and results.
Capture measurements are synthetic fixtures, not renderer or visual-quality
evidence. The tests cover improvement, ties, invalid captures, scorer exhaustion,
provider pause/deadline, review rejection/withdrawal/revision, budget/round/plateau
stops, multiple objects and restart recovery.

## Review without activation

After delivery, the operator must run a daemon containing the new
`object_loop_inputs` contract. Normal vault initialization seeds the bundle at
`reviewed-playbooks/object-loop/`, and seeds formulas write-if-absent at
`formulas/object.md` and `formulas/variation.md`. Existing formula edits survive;
compare them with `src/prompts/formulas/` before the pilot.

For the playbook-review workflow, install `source.md` as
`projects/matter-engine-cpp/playbooks/object-loop.md` in the vault. Extract
`rules` and `steps` from `artifact.json` into a local semantic-body JSON file.
Submit that body with the review document using `aq review submit`,
`--playbook-id object-loop`, `--playbook-body <body.json>` and
`--no-activate-on-approval`. Use kind `other` so approval does not trigger spec
ingest. Submission pins the compiled bytes; approval stores those exact bytes
and records `activated: false`. Compilation timestamps mean a fresh submission
has its own hash: inspect the review's `playbook.artifact_sha256`, source,
semantic diff and diagnostics, and use that hash consistently thereafter.

For approval of the already recorded bundle bytes, the operator can import
them with `aq playbook v2-import --path reviewed-playbooks/object-loop`.
Import validates and stores exactly the recorded hash; it never activates it.
Import itself is not a human approval decision. Retain the separate approval
record naming the hash. No live review decision or import is performed by this
implementation task.

## Activation prerequisites

Before recording an activation decision, verify the Matter artifact adapter
can retain and materialize immutable URI/hash bundles after task cleanup;
calibrated reference/view/light/rig/scorer manifests and the hash-bound brief
are approved; workers can execute finite capture jobs under an audited resource
lease; and cost coverage, retries and the final suite fit the object budget.
Keep the pilot at at most two object epics and one GPU lease. The read bridge
has an explicit 32-object bound; it fails visibly above that bound.

Only after those checks and an explicit operator decision, activate with
`aq playbook activate --playbook-id object-loop --artifact-sha256 <approved-hash>`.
This is a separate recorded operation. Neither source frontmatter nor proposal
approval authorizes it. Put the same hash, without the `sha256:` prefix, in each
object's `policy_sha256`; the running policy ignores objects bound to other bytes.

## Formula and worker inputs

Cook `object` at the project root with `object_id`, the approved proposal's
`proposal_sha256`, and `start`: a complete JSON `ObjectLoopStartArgs` packet
without `project_id` or `epic_task_id`. AQ derives those identities. The graph
transaction holds the bootstrap behind an event gate until the loop's finalizer
hold commits. A timer recovers missing formula/completion/review events.

Candidate tasks use immutable artifacts and cannot publish their source.
The independent scorer receives the current loop version and wave manifest.
Before closing its held task, it records a note beginning exactly
`object-score:1` plus a newline, followed by one JSON `ObjectScoreRecordArgs`
object. `aq task set <held-id> --note <packet>` is the supported handoff.
Invalid captures retain required null metrics. Include a finite `next_variants`
packet for continuation; checkpoint continuation still requires the exact
current review revision, content hash and candidate approval.

Cook `variation` into one existing suite container directly under the object
epic, after exact candidate approval and before recording the terminal stop.
Supply immutable candidate/render hashes, a durable artifact URI, locked view
set, two JSON arrays of five distinct integer seeds, and finite JSON presets
(1–16 entries, at most 16 KiB). Exactly one suite may consume the object's final
reserve. Three leaves cover both seed groups and preset/reload evidence. This
preserves the three-level hierarchy: epic, suite, checks. Finalization waits for
their settlement, including exhausted failures, without erasing those failures
or turning review rejection into approval.

Product integration remains a separate ordinary code task importing an accepted
generator. Experiment task completion grants no publication authority.

## Verification

`tests/test_object_loop_playbook.py` runs the bundle through real command
contracts and then replays every command in V2 dry-run mode. Set
`AQ_OBJECT_LOOP_TRACE_DIR` to retain each successful trace. It also tests formula
transaction rollback, scope/capability refusal, exact review revision, reserve
reuse prevention, finalizer holds, and approval storage without activation.
`tests/test_object_loop.py` retains the five prerequisite regression probes.

Run these through `aq test` with a disposable `POSTGRES_TEST_DSN`. The affected
area checks cover V2 execution, formulas, graph creation, vault seeding, reviewed
bundle validation/import, reviews, generated API contracts and test selection.
The isolated `scripts/e2e-env.sh --reset` / `scripts/e2e-smoke.sh` kit verifies the
swarm without an LLM or the operator database.
