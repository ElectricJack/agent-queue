# Completed parent PR admission and reverification

Completed train parents must have an approved exact aggregate review whose
verification id matches the current, completed aggregate verification. The
existing authorized-root producer supplies this evidence under the frozen root
admission policy; it must also run after successful parent completion. GitHub
polling remains the restart-safe retry and human review observation path.
Archived children still identify a root as a verified parent at train admission;
archiving must not turn it into a leaf or discard its exact verification binding.

When an open PR still names the canonical repository, branch and default base,
but its head has advanced, the daemon may start a fresh verification episode.
This is permitted only for an active train project whose automatic authorized
root policy already admits that parent, with no hold, gate, rejection, delivery,
active batch or live branch writer. A Git read must prove the new published head
contains the previously completed, trusted aggregate and every carried receipt.
Remote Git reads run before the mutation transaction, which rechecks the source,
receipt set, owner fence and admission controls before committing the new episode.
An unrelated or rewritten head requires operator attention.

The transaction preserves the previous completed operation, verification,
receipt and review evidence. It starts a new episode and generation, accepts
the old receipts using the previous completed verification, clears current
verification and transfers a detached branch reservation at a fresh fence.
The reopen guard permits delivery receipts whose targets remain within the
reopened subtree, including grandchild receipts into nested parents. Receipts
that leave the subtree, including delivery to the default branch, still refuse
the reopen. This exemption applies only to a completed episode rollover;
ordinary reopens and other hierarchy mutations retain their delivery guards.
The parent returns to PAUSED and the ordinary integration-ready handoff files
a fresh verifier. Trusted CI and the existing playbook must verify and complete
the new aggregate before its new parent review can be produced. Repeated head
advances preserve receipts already accepted into earlier verified episodes.
No local test result, stale review or old exact-head operator grant authorizes
the new aggregate. Reconciler-owned parents remain under their owning engine.

Doctor and stall sweep report completed parent PRs with no active batch or
delivery. They distinguish missing aggregate verification, missing or rejected
parent review, unauthorized admission and awaiting train admission. These are
read-only findings, including PRs whose remote state cannot be read.

Validation includes a real Git and disposable PostgreSQL two-child scenario
with both flat and nested hierarchies:
collect, verify, complete, produce parent evidence and admit; advance the PR
head, preserve receipts, reverify, produce new evidence and admit again. Guards
must refuse rewritten heads, active owners, holds, stale observations and
already delivered parents. The integration owner supplies production delivery
proof for PRs #932 and #933; workers do not merge unheld tasks.
