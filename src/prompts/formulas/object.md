---
name: object
description: Gated object epic with immutable inputs and an AQ-2 finalization hold.
vars:
  object_id: {required: true}
  proposal_sha256: {required: true}
  start: {required: true}
---
# Object experiment

Cook at the project root only, after separately activating the exact reviewed
object-loop artifact. `start` is a JSON ObjectLoopStartArgs packet without
project_id or epic_task_id (derived by AQ). Include the artifact's bare
policy_sha256, immutable input hashes, approved `other` brief revision/hash,
one to three variants and aggregate reservations including every retry and the
final suite. `proposal_sha256` binds approved rev-amber-zenith revision 2.
No paid work is admitted by cooking: the bootstrap is transactionally gated
until the playbook commits the finalization hold. Never close it around a
missing loop, run a generator here, or activate a policy from this task.
The approved brief explains the object, maximum rounds and budget in plain
English. Internal bootstrap, probe, capture and score evidence goes to the
supervisor through task notes; any review is supervisor-only. Only the
finalizer submits one human result with before/after images and an ordinary
explanation of improvement and what would help next time.

```aq-graph
version: 1
parent:
  title: "Object {object_id}"
  description: "Artifact-only object experiment; proposal rev-amber-zenith revision 2 ({proposal_sha256})."
defaults:
  task_type: research
nodes:
  - key: bootstrap
    title: "Verify object {object_id} bootstrap"
    description: |
      AQ releases this task only after the durable loop and held finalizer exist.
      Verify the supplied immutable manifest, artifact retention and budget:
      {start}
      Record bootstrap evidence, then close this leaf. Do not generate or
      publish assets. The loop policy owns sibling creation and scoring.
      Candidate and scorer workers hand off immutable URI/hash evidence,
      never another worker's directory. Worker task retries must fit the
      original reservation, including unknown costs.
      This is internal evidence with no implicit human-review deliverable.
      Send technical blockers to the project supervisor; Jack needs a single
      plain-English sentence only if he must act.
    acceptance:
      - "The loop finalization hold exists before this bootstrap can run."
      - "Reference, rig, scorer, policy and candidate hashes are fixed."
    labels: [object-experiment, object-bootstrap]
```
