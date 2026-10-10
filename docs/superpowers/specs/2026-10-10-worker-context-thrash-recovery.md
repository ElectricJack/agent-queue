# Bounded recovery from worker context thrashing

Task: `agile-willow-17`. The pool exit path currently discards session-local
restart counts when it returns a task to READY. Repeated compaction failures
therefore launch identical workers indefinitely.

Persist the consecutive exit cause and count in task metadata, with the last
session id and instance token. Count an incident once, fence it to the current
claim, and retain it across new sessions and daemon restarts. A different cause
starts a new streak; an explicit task restart clears it.

Recognize Claude's explicit `Autocompact is thrashing` diagnostic from its
terminal or an exact-session transcript's bounded tail. Transcript evidence
must be a harness error, never a user prompt or a tool result quoting a failure.
An unchanged live session with this error is handled by the stall ladder too.
After two consecutive context-thrash incidents, exhausted stall ladders or pool
backstop timeouts, stop
automatic recovery: BLOCKED with needs_attention, release through the existing
guarded session teardown, and queue the standard supervisor recovery incident.
Do not quarantine healthy workers sharing that pool. Ordinary exit and provider
recovery behavior remains unchanged; their causes are still counted.

Before releasing a failed attempt, save a short recovery snapshot: exit cause,
previous claim identity, branch and checkout observations, recent task progress,
and evidence pointers. Present it explicitly as historical evidence in the next
prime, alongside any existing agent handoff. Never infer completed work or
invent a next action from a transcript.

Worker prime bounds descriptions (4 KiB) and legacy/spec context previews
(6 KiB) and provides
explicit paged task reads for omitted text. Role/rules, deliverables, pending
messages and completion requirements remain intact. Worker task_show defaults
to a 16 KiB preview; `--field FIELD --offset N --limit N` reads canonical text
or serialized structured fields in bounded pages without widening task scope.
Operator and elevated supervisor full views retain their current behavior.
Expose the persisted failure streak in task_show and the read-only stall.sweep
doctor report. Neither diagnostic automatically unblocks a task.

Validation covers two separate pool claims, duplicate observations, daemon
recreation, cause changes, explicit restart, stale identities, transcript
quotations and missing evidence, live stalls, supervisor notification,
continuation readback, oversized descriptions/spec context, and paged reads.
Run focused reconciler/prime/surface/doctor tests, related area tests, generated
catalogue checks, and the swarm smoke test for pool recovery.

Validation: 772 area checks passed, including generated clients, command docs,
CLI inventory, task controls, recovery, and accepted/refused repair restarts.
The final reconciler module passed 248 checks, including separate replacement
claims for compaction thrash, exhausted stalls and backstop timeouts. Ruff and
the complete generated-artifact drift check passed. Swarm Tier 1 passed all
20 scenarios against the final source, including the backstop recovery change.
