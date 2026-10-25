---
id: object-loop
kind: pipeline
scope: project:matter-engine-cpp
enabled: false
triggers:
  - formula.cooked
  - task.completed
  - task.failed
  - review.decided
  - timer.5m
---
# Bounded object experiments

Run approved object experiments within their fixed budget and round limits.
Events wake the policy; persisted state decides what happens. Sweep on each
listed event and every five minutes, including after a restart.

## Admission and limits

Require the rendering adapter, durable artifacts, an approved brief, fixed
comparison inputs, a finite job/GPU lease and reserved costs before activation.
Use calibrated references unless the supervisor explicitly authorizes a
self-reference attempt. Use the retained baseline capture as that reference
and label results indicative, for plumbing only. Keep candidate and
capture identities distinct. Pin one editor build, rig, scorer and render
profile for every capture in an attempt.

Read at most 32 pending or active objects per sweep, before filtering; exceeding
that bound fails visibly. Process eligible objects in their recorded order.
Start only with current proposal approval and the running policy's exact hash.
Leave unapproved starts held. Process existing attempts only under that hash;
changing policy requires an explicit operator migration. Commit the finalizer
hold before releasing the bootstrap.

Preserve all approved start inputs and limits. The pilot allows two object
epics and one GPU lease, with at most three rounds and $100 per attempt. That
budget includes retries, scoring and final checks; it never resets per round.
Other approved attempts keep their own limits. Keep repair and plateau caps,
reserve each variant's allowed retries, charge unknown costs at full reserve,
and stop after 24 hours.

## Each sweep

For each eligible attempt, take the first applicable action:

| Condition | Action |
|---|---|
| Already stopped | Reconcile outstanding work and finalizer release. |
| Deadline reached | Stop and reconcile. |
| Scorer exhausted | Record a defect stop without receipts, retain the best verified candidate, and reconcile; quality is unavailable. |
| Checkpoint pending | Read the current checkpoint and follow its decision below. |
| Score available | Validate and record the score, then reconcile. |
| Otherwise | Reconcile; keep unsettled work held. |

A rejected, withdrawn or changes-requested checkpoint stops the attempt while
retaining evidence. Continue only with approval of the exact current review
revision, content hash and candidate, and a finite next-variant plan. Use the
fresh checkpoint version. If continuation is refused by limits, stop at that
same version. Otherwise keep the checkpoint held for supervisor variation
checks or revised evidence. Events and wait answers never grant approval.

Accept only the current completed scorer's latest typed score packet. If a
continuation is refused, try that packet once as a stop, removing its next
variants and review fields. Revalidate its evidence; invalid evidence still
fails. All other rejections fail the sweep. The typed commands own receipt
validation, ranking, tie retention, reservations and settlement. The compiler
chooses bindings, field mappings and graph structure from those contracts.

Retry only runtime errors: at most two command attempts, one second apart.
Other errors end the sweep visibly. Each sweep and item must terminate;
subsequent events recover from persisted state. Add no model calls, scripts,
provider substitutions or extra retry tasks.

## Workers, checks and reporting

Cook the object formula once at the project root with immutable inputs and the
approved brief, gating its bootstrap during graph creation. Workers start from
the retained best candidate, with prior
round branches, commits, retained bundles and frozen comparison inputs in their
handoff. They may read that history and edit generators, materials and presets
in their own workspace. Publish evaluation bundles to the artifact store.
Independent scorers check every required view and every wave member under the
same fixed profile, then record the typed score packet using the existing
scorer handoff protocol before closing.

Before the final stop, install one variation suite directly under the epic,
with checks beneath it: ten seeds plus boundary/reload coverage. Require exact
current candidate approval, reuse the final-suite reserve, reject duplicate
cooking, and keep all tasks artifact-only. Stay within three hierarchy levels
and add no ancestor dependencies. Finalization waits for candidate, scorer and
variation settlement and verifies artifacts, review state, failures and the
stop reason. Preserve failed child statuses; reporting defects never forces a
passing epic.

The user receives two documents per attempt: a plain-language brief naming the
object, maximum rounds and budget before cooking, then one final result with
paired before/after images for each view, the improvement in ordinary words
and advice for the next run. Reuse the final result document on resubmission.
Use the object's ordinary name and omit internal identifiers and scoring jargon.

Keep round scores, probes, captures, variation and continuation reviews internal
and supervisor-decided, outside user review requests, notifications and digests.
Experiment tasks and probe children have no implicit human-review deliverable.
Route the final result to the user. Send detailed blockers to the supervisor;
contact the user only when action is required, in one plain-language sentence
naming that action.

Review approval and activation are separate decisions. Activation requires an
operator decision naming the exact compiled hash. This policy cannot activate
itself, approve reviews, choose providers or publish generators. Product
integration requires a separate code task importing an accepted generator
through the project's integration owner.
