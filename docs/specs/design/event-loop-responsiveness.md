# Event loop responsiveness and stall evidence

Task `calm-ember-68`, 2026-10-03. The daemon's HTTP API shares the scheduler's
asyncio loop. Synchronous transcript decoding can delay every request even
when PostgreSQL is idle. An await-chain warning identifies a suspended task,
not the Python code consuming CPU; stall diagnosis needs samples of the loop
thread while the stall is happening.

## Transcript reads

Claude and Codex readers perform the complete read, JSON decoding, prefix
recovery, and entry normalization in a worker thread. The asynchronous reader
interface, byte offsets, incomplete trailing records, usage identities,
completion recovery and duplicate suppression retain their existing semantics.
Reader instances have no mutable parsing state shared between threads. The
watcher also offloads session-key discovery and checkpoint file stat calls.

## Stall sampler

The existing metrics loop-lag probe owns a companion watchdog thread while
performance probes are enabled. The loop sends a heartbeat each probe period.
If it is more than one second late, the watchdog samples only that thread's
Python stack using `sys._current_frames`, without ptrace or elevated access.
It logs the first stack immediately, at most one continuing warning per 30
seconds, and the most frequently observed stack on recovery. Stack depth and
distinct stacks per episode are bounded; samples contain filenames, function
names and line numbers, never locals or transcript text. Shutdown stops and
joins the watchdog. These are sampled locations, not proof of CPU ownership:
blocking I/O or host starvation may produce the same overdue heartbeat.

## Health isolation

Production `/health` and `/ready` read a snapshot maintained by one background
task, rather than invoking database/playbook checks for each request. Collection
has a two-second timeout, with a five-second refresh interval. A missing,
failed, or more than ten-second-old snapshot fails closed; `/health` reports
`busy` for an initializing, timed-out or stale collector, and `degraded` for
reported subsystem failures. Required-playbook repairs become visible on the
next collection. Responses include snapshot age and retain subsystem checks.
No request waits for collection, and repeated probes cannot multiply database
work. A frozen loop still cannot serve HTTP; the watchdog logs while Python
allows its thread to run, and cached health removes request-owned collection
as another source of stalls.

The main provider reads agents once per collection. `perf.health_latency` is a
read-only doctor check over the last three minutes of retained route latency
histograms. It requires ten completed `/health` requests, warns at p95 above
two seconds, and reports insufficient or disabled telemetry as informational.
This duration starts at ASGI dispatch; socket queueing before dispatch is
covered by the existing loop-lag check and the watchdog, not this histogram.

## Verification

`scripts/acceptance/transcript-loop-profile.py` creates synthetic 100,000-record
transcripts for each harness, profiles a cold read, and processes eight session
backlogs while probing `/health`. Probe latency includes the time the loop was
unable to schedule the request. Use identical parameters before and after.
This is a repeatable workload without a daemon, database, or private transcript
content. It does not establish which code caused the historical fleet outage;
operator deployment and a fleet soak remain necessary to verify that incident.
Unit checks use thread barriers to prove health proceeds while parsing or
collection is blocked, instead of machine-dependent wall-clock assertions.

## Isolated Git repository cleanup

Incident samples on 2026-10-25 repeatedly located the loop in synchronous
`TemporaryDirectory` cleanup during authenticated origin fetches. Isolated Git
directories (fetch, clone, push and remote reads) must be recursively removed in
a worker thread. The owning operation waits for removal before returning or
propagating cancellation, including cancellation received during cleanup. Git
subprocesses and credential brokers retain their existing termination and
zeroization order. Regression checks use thread barriers to prove the loop runs
while removal is blocked and the operation retains ownership until removal ends.

## Repeated handler work

Wait cost accounting keeps at most 128 first-turn observations per lifecycle,
including observations with no completed usage yet. Entries are scoped to the
session identity, harness, work directory and turn start. A file's device,
inode, size, nanosecond modification time and change time must still match
before reuse. Changed, replaced or missing files invalidate the observation;
a file changing during a read is never cached. Token reduction runs in a worker
thread as well as transcript parsing. Completed accounting does no further
measurement writes. This cache contains measurements, not wait lifecycle state.

Delivered-PR sweeps reject ineligible PRs and attached writers before opening or
locking the retained Git store. Eligible proofs still fetch and verify fresh
remote heads, requesting both exact refs in one remote read. Apply retains its
fresh proof and final PR-head check.

Train stall and delivery watchdog inventories reserve their one-minute cadence
before awaiting work, with at most one inventory in flight per handler. Slow
inventories cannot multiply under overlapping ticks. Completion and cancellation
release the guard; subsequent ticks still run the train.

`scripts/acceptance/daemon-handler-cost.py` measures repeated active PR previews
and unchanged pending transcripts using temporary Git stores and transcript
files. See [the handler cost report](../../reports/2026-10-25-daemon-handler-cost.md)
for before/after evidence and the separate live CPU acceptance obligation.
