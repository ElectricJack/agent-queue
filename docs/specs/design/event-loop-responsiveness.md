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
