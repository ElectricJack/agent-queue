# Object-loop bundle: review and activation handoff

AQ-3 ships a mechanically validated, inactive policy for `matter-engine-cpp`,
plus the `object` and `variation` formulas. It does not establish operational
readiness for autonomous generation. The approved proposal is Matter review
`rev-amber-zenith`, revision 2; artifact approval and activation are separate.

The recorded artifact is
`sha256:9d5eac29069121be9a98dc0cb2f36858bf1336c40a7323a813df06b5a5e3e977`.
Its source, canonical artifact, contract/grant manifest, compiler diagnostics
and live/dry traces are in `src/prompts/reviewed_playbooks/object-loop/`.
The byte-identical test recording is in `tests/fixtures/playbooks/v2/object-loop/`.
`traces.json` records real AQ commands against disposable PostgreSQL, followed
by write-free preview replay with identical command arguments and results.
Capture measurements are synthetic fixtures, not renderer or visual-quality
evidence. The tests cover improvement, ties, invalid captures, scorer exhaustion,
provider pause/deadline, review rejection/withdrawal/revision, budget/round/plateau
stops, multiple objects and restart recovery.

## From a finished render to a startable loop

The loop cannot be started from a `matter_render` job until two identities
exist: durable artifact URIs for the evidence, and a `render_profile_sha256`.
Both are produced by one command each, from a completed job id.

```bash
# 1. Retain the capture, and read the render profile off the immutable result.
aq job retain 44a60940-aa75-4284-be56-9e28d2748056 --json
#    -> render_profile_sha256   quote as ObjectLoopStartArgs.render_profile_sha256
#    -> artifacts[]             becomes ScoreReceipt.artifacts
#    -> captures[]              each image becomes Capture.image
#    -> candidate_artifact      already carries sha256 == candidate_sha256
#    -> rig_sha256              quote as ObjectLoopStartArgs.rig_sha256

# 2. Prove a URI still names the bytes it claims, any time later.
aq artifact verify artifact://sha256/aefb3b06bc177013a45015de69066201eaafbda20c3d0ae7516ceb1a88882e9f
```

Retention is idempotent — the digest is the identity, so a second run yields the
same URIs and rewrites nothing. Both commands are scoped to the caller's own
job, so a worker session that submitted the render can run step 1 for its own
receipt; anything outside that scope is refused.

What step 1 does not do, deliberately: it never measures an image. `Capture.ready`
and `decoded`, and every per-view metric, are the external scorer's to produce.
AQ gives it the pointers and the digests.

Two fields are only right if their producers are, too. `render_profile_sha256`
covers the editor build, the capture adapter, the GPU lease, the view set and
each view's resolution, the admitted rig frame budget and the VT readiness
counters — so a start packet that quotes it is claiming those bytes came from
that preset. And `candidate_artifact.sha256` equals the candidate's declared
`candidate_sha256` because the canonical manifest is what AQ retained; if the
bundle's declared identity stops being the canonical document, retention refuses
it rather than minting an artifact that merely sits beside the claim.

### Operator-only steps, and who runs them

`aq formula cook`, `object_loop_start` and `object_loop_inputs` are refused to
worker sessions. The refusal names the step that supersedes it: the **project
supervisor** runs `aq object_loop inputs` and cooks the `object` formula, because
formula cooking and loop mutation stay out of worker scope. Everything above —
retention, verification, and assembling the start packet — is worker-safe.

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

`start.max_rounds` (1–8, default 8) is the round ceiling, so the pilot's
three-round cap is `max_rounds: 3` and each wave still reserves its own
variants — `calls: 3` only ever bought one variant per wave. Like the repair
and plateau caps it is fixed input: a repeated start that restates it as the
default is refused as different fixed inputs. When the third round's score asks
to continue, the reservation is refused with `round cap reached`; the loop
records that as a stop through the policy's score fallback, keeping the
verified incumbent.

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
`tests/test_object_loop.py` retains the five prerequisite regression probes,
and covers the durable artifact store (minting, idempotence, symlink and
tamper refusal, URI resolution), the render profile's stability under per-run
jitter and its sensitivity to every preset input, and the acceptance path end to
end: one retention run yielding both a valid `ObjectLoopStartArgs` and a
`ScoreReceipt` that passes `validate_score_receipt`.
It also pins the round cap: `max_rounds` is bounded start input, three rounds of
two variants each is admitted and refused on the fourth, the refused
continuation charges nothing, and a loop row written before the field existed
keeps the eight-round ceiling.
`tests/test_jobs_matter.py` asserts the profile is recorded on a real completed
render's immutable result, that the admitted frame budget reaches the contract,
and that a result with no retained capture records none.

Run these through `aq test` with a disposable `POSTGRES_TEST_DSN`. The affected
area checks cover V2 execution, formulas, graph creation, vault seeding, reviewed
bundle validation/import, reviews, generated API contracts and test selection.
The isolated `scripts/e2e-env.sh --reset` / `scripts/e2e-smoke.sh` kit verifies the
swarm without an LLM or the operator database.
