# Transcript decoding and daemon responsiveness

Task `calm-ember-68`, base `6d9ca2d7d`, 2026-10-03. The task reports a
2026-10-03 21:44–21:54Z live fleet outage. This investigation reproduces a
blocking transcript path on the shared host; it does not retrospectively
identify the stack responsible for that historical outage.

## Reproduction and profile

From the checkout, run:

```bash
python scripts/acceptance/transcript-loop-profile.py
```

The script writes disposable synthetic JSONL files: 100,000 records per
transcript, 74.1 MB of Codex tool output or 59.5 MB of Claude assistant
thinking/usage records. It profiles one cold read and then reads eight session
backlogs sequentially while requesting `/health` every 10 ms. Each harness
is measured separately, with identical bytes, entry counts and final offsets
before and after. Latency includes the delay in scheduling each request;
an ASGI-only timer would miss that delay. The HTTP app is a minimal health
router, without a database provider, to isolate transcript processing.

| Harness | Health p95 before | Health p95 after | Maximum before | Maximum after | Probes before / after |
| --- | ---: | ---: | ---: | ---: | ---: |
| Codex | 1665.641 ms | 30.973 ms | 1751.756 ms | 487.921 ms | 71 / 856 |
| Claude | 1642.185 ms | 23.440 ms | 1872.309 ms | 192.689 ms | 54 / 787 |

Raw local evidence was recorded in `/tmp/calm-ember-68-before.txt` and
`/tmp/calm-ember-68-after.txt`; the script is the durable reproducer. The
before-change profile records 100,000 `json.loads` calls per read. Their
cumulative profiled time was 1.250 s for Codex and 1.385 s for Claude;
normalization (`_entry_from_line`) accounted for 1.226 s and 1.737 s respectively.
These cumulative figures overlap and must not be added. Profiling overhead
makes these unsuitable as production duration estimates.

Previously only stat/read and selected Codex prefix helpers were offloaded;
`read_new` decoded and normalized the entire backlog synchronously on the
event loop. Both readers now offload the full operation, including Codex
model lookup and duplicate/completion recovery. Thread barriers in the unit
tests verify that `/health` completes while normalization remains blocked in
another thread. Existing reader/checkpoint tests verify byte-offset and usage
semantics. Fleet processing duration increased from 13.99 to 18.87 s for
Codex and 13.87 to 16.53 s for Claude in these runs; the scheduling overhead
buys loop responsiveness rather than reducing total decoding work.

## Operational evidence and health

The metrics loop-lag probe now owns a bounded, independent stack sampler.
After an overdue heartbeat it logs code locations without request contents or
locals; first warning, 30-second continuing warnings and a recovery summary
show the most frequently sampled stack. This remains usable without ptrace.
It cannot run while a C extension holds the GIL indefinitely, and sampled
locations can reflect blocking I/O or host pressure as well as CPU work.

Production health and readiness serve a background snapshot. A slow collector
cannot multiply database work through repeated probes. Collection times out
after two seconds and refreshes every five seconds; missing, failed or stale
snapshots fail closed. `/health` returns `busy` for initialization, timeout or
staleness, with snapshot age; subsystem failures remain `degraded`. The
embedded MCP lifespan preserves API startup/shutdown so the collector starts,
stops and restarts with its server. The new read-only `perf.health_latency`
doctor check warns above a two-second p95 of completed requests, complementing
the loop-lag check for queueing before ASGI dispatch.

## Validation and limits

The focused reader/health/performance/doctor run passed 123 tests. The real API
and embedded MCP lifecycle run passed 12 tests. The final related-area run
passed 425 tests, including WebSocket restart and selection-catalogue checks.
Ruff, `git diff --check`, and catalogue regeneration checks passed. Commands
use `aq test` and the separate disposable PostgreSQL service on port 5534;
no operator database was migrated and no daemon was restarted. Exact commands
are recorded in the task's completion evidence.

The measured workload meets the one-second health target. The exact live
fleet load was not replayed, and these numbers do not prove that all causes
of the ten-minute outage are fixed. Operator rollout should retain performance
probes, inspect watchdog stacks during a fleet soak, and check health latency
and loop lag together. A frozen shared loop still cannot answer HTTP, and
the stack sampler is diagnostic evidence rather than a second HTTP server.
