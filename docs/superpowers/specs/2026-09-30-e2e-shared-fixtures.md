# Stateful daemon E2E fixture reuse and timing

The acceptance suite keeps four parallel shards. Each shard owns one disposable
PostgreSQL database, port, home, vault and daemon per pytest worker. Each scenario
is a separate pytest item; `xdist_group` with `--dist loadgroup` keeps a shard on
one worker. Selecting one item creates only its shard's fixture.

S1, S2 and S3 form an explicit ordered chain. A selected S2 or S3 prepares missing
predecessors in its own fixture. Successful predecessors are reused in a complete
run. Failure invalidates that chain and cleans its state before another item.
All other scenarios begin with empty task frontiers and no live sessions. Cleanup
uses public commands, restores swarm, fake-provider modes and the failover policy,
and deletes scenario tasks including terminal rows. Cleanup failure fails the
item and prevents reuse of a contaminated daemon. Final teardown runs even when
environment preparation or a scenario fails.

The fake-tier daemon launcher injects shorter scheduler/config watcher cadence;
production and tmux defaults remain unchanged. Waits are bounded predicates, and
negative observation windows retain at least two scheduler cycles. Explicit CLI
assertions keep separate real CLI processes. Timings distinguish environment
build/start/stop, CLI calls, condition waits and provider recovery. They remain
visible in stdout and JUnit properties. Compare the same machine, worker count,
markers and scenario coverage against the source baseline before rebalancing.

S16's recovery-to-all-down transition stops the fake-provider recovery sessions,
including draining sessions and launches that appear after the first snapshot,
until a fresh launch reports the simulated login failure. Each observed session
is stopped once. The bounded wait still requires `prova` to become
`unauthenticated`; cleanup does not override provider availability or extend the
convergence budget.

Regression checks cover scenario collection and selection, prerequisites, failure
cleanup, fixture reuse, daemon isolation, global restoration and coverage parity.
Fixture/probe safety suites remain separate from the real-daemon acceptance arm.

CLI imports are a measured bottleneck. Pytest opts into a per-world Unix-socket
launcher which registers the real CLI once and forks a new CLI child with a fresh
environment and Click context for each ordinary command. No handlers are invoked
directly. Claim and graph races, waits, plugin startup, help and version retain
fresh interpreter startup. Output/exit equivalence and environment isolation are
regression-tested. The launcher process group is terminated before world cleanup.
The standalone kit retains fresh interpreter coverage for all scenarios.

## Production responsiveness extension

The queued operator instruction extends this task to the backend. Production
scheduler cycles wake on committed task/routing/gate/provider/session changes,
coalesce event bursts, and retain the periodic five-second backstop. A minimum
one-second start-to-start interval bounds repeated event-driven cycles. Events
request a normal deterministic cycle; they never choose work or bypass readiness,
capacity, provider backoff or identity fences. Subscriptions and waiter tasks are
removed on shutdown/cancellation. Events emitted during a cycle remain pending.

Scheduling configuration exposes `cycle_interval_seconds` (5),
`min_cycle_interval_seconds` (1) and `config_poll_interval_seconds` (30), all finite
positive values, with minimum cadence no greater than the periodic interval. They
are hot-reloadable. Idle installations retain the original scheduler and config
poll cadence. The fake fixture selects shorter values through the same production
configuration. Benchmark task-to-worker and provider override recovery-to-worker
latency in the owned daemon, and record idle completed cycles, process CPU and
PostgreSQL transaction counts with identical cadence and workload before/after.
