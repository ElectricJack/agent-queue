# Wait checkpoints and continuation prime

Implementation of approved hibernation/waits section 11.1 step 1 and the prime
projection in section 11.3. The v2 flag stays off. This slice does not register a
listener, start a deadline driver, stop a process, park a task, launch a session,
or acknowledge held messages.

## Capture and fallback interfaces

`wait_checkpoint` has a concrete default callback in `src/waits/checkpoint.py`
through `WaitV2CommandsMixin`. It still requires the existing v2 flag and
authenticated holder authorization. Runtime composition can supply an explicit
callback in `orchestrator.wait_v2_handlers`.

The hibernation owner creates the active continuation and registration-time
`held_cursor_seq` before returning `checkpoint_then_end_turn`. Capture requires
that existing intent; it does not manufacture one from an agent's request.
`checkpoint_continuation(conn, scope=..., wait_id=..., summary=..., facts=...,
continuation_id=None, generation=None)` uses the caller's transaction and locks
task, wait, then continuation. It repeats the live session instance and claim
checks and refuses superseded, stopped or non-hibernating intents. Summary input
is nonblank UTF-8 of at most 8 KiB. The original text and SHA-256 are stored
together. An identical retry preserves the first facts; replacement prose is
refused. The CLI bounds its binary file read and preserves CRLF in the digest.

Facts come only from the server collector: task, epoch, session, checkout branch,
HEAD, dirty paths, last comment, open subtasks and existing handoff facts. Git
inspection uses the async GitManager API. Agent prose remains an assertion;
verification claims are not promoted to managed verification evidence.

The deadline/hibernation owner calls `degrade_checkpoint(conn, task_id=...,
wait_id=..., continuation_id=..., generation=..., now=...,
transition_budget_seconds=..., facts=...)` inside its authenticated service
command transaction. All inputs are server derived. The deadline and task claim
are rechecked with the same lock order. The transition anchor is the later of
registration and intent creation, covering subsequent eviction. Before the
budget expires, missing prose is refused. At expiry, missing prose commits
`summary_degraded=true`, null summary/hash, and server facts only. A submitted
summary is retained; replay does not change facts, and late prose cannot replace
a committed degraded checkpoint. This follows detailed section 11.1 and design
7.1; missing agent prose alone does not constitute failure to prove a stop.

## Outcome and prime interfaces

`wait_outcome_envelope(wait, continuation=None, held_messages=(), decision=None)`
constructs the approved envelope for both in-place and hibernated delivery. It
refuses pending waits and removes private adapter bindings from the condition.
Dispatcher/resume owners retain responsibility for committing and delivering
the message and providing the recorded supervisor decision.

`continuation_prime_context(conn, task_id=..., project_id=..., session_id=...)`
reads a continuation bound to the fresh session and its owning project/task.
`PrimeRenderer` renders outcome, compaction summary, current ticket, comments,
then held routine mail. Summary and historical server facts are labelled
separately. Legacy local prime overrides cannot drop this resume contract.
Ordinary primes keep their existing section shape and budgets.

Continuation comments are paged newest first, retained under a separate 64 KiB
UTF-8 budget, and rendered oldest first. Every comment is included if it fits;
overflow discards the oldest text first and includes `aq task comments` as the
full-history pointer. The ordinary context preview does not truncate these
comments or the summary. Held mail is scoped by sequence cursor and recipient
and excludes wake/control messages. Reading prime never stamps held mail
delivered; `wait_ack` owns those stamps and cursor advancement.

No database DDL, claim/pool scheduling or public API model change is made here.
The tests cover the SQL checkpoint/deadline race on disposable PostgreSQL,
command activation and claim fences, exact byte/hash capture, comment budgets,
fresh-session scope, held mail, legacy overrides and harness envelope wrapping.
