# Deferred bounded recovery behind an integration owner

Task: `stark-quest-41`. Extends the bounded worker recovery specification of
2026-10-10 without changing recovery eligibility, budgets or ownership policy.

The first guarded pool failure requests READY through pool teardown. An attached
or pending integration owner can refuse that release. Production teardown then
records the confirmed stopped writer while retaining its claim and workspace.
Previously recovery reported a retry anyway; the orphan sweep subsequently
replaced the cause with `session_not_live`. That generic incident cannot recover
a `context_thrash` execution end through the existing operational exit allowlist.

After teardown, record the actual disposition in a transaction fenced by the
task epoch, session instance, last claim epoch and exact failure-streak record.
A released, unassigned READY task is a scheduled bounded retry. A confirmed
stopped writer still bound to its failed task is a deferred continuation:
BLOCKED with `session_<cause>_handoff_deferred`, preserving its claim, workspace,
owner, continuation and streak. Record the failed claim, branch and workspace
as historical pointers. Canonical owner ids, fences and source proofs remain in
the existing ownership and release-audit records; do not duplicate their authority.
Queue supervisor attention and report deferred recovery instead of retrying.
The second same-cause failure keeps its existing `session_<cause>` attention.
An unconfirmed stop cannot establish deferred stopped-writer evidence or
scheduled recovery. A changed claim, instance, routing authority or explicit
task status wins; an old recovery must neither overwrite it nor report a retry.
Duplicate observations must not repeat a disposition comment or notification.

This change intentionally does not automatically release an owner, clear a
supervisor hold or admit context thrash to generic supervisor recovery. The
recorded incidents require fresh runtime/ownership/source evidence and an
explicit supported recovery decision. Where existing controls cannot preserve
the streak while retrying a released context-thrash writer, report that bounded
gap; do not substitute task metadata edits or a restart that clears the streak.

Validation uses production pool teardown with transactional task/session
attempts and attached/pending/released owners, duplicate and late observations,
replacement claims and unconfirmed stops. Existing cross-session second-failure
tests, recovery/ownership area checks and disposable swarm smoke remain required.
