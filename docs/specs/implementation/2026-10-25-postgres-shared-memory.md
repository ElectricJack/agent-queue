# PostgreSQL shared memory capacity

Task: `vivid-harbor-88`.

The canonical `docker-compose.yml` provisions PostgreSQL 18 with `shm_size: 1g`
for the daemon and disposable test service. Keep project `agent-queue2`, service
`postgres`, and the `pgdata:/var/lib/postgresql` mount unchanged so an operator
can adopt the existing `agent-queue2_pgdata` cluster. Preserve the running daemon
container's `unless-stopped` restart policy.

Register a report-only `db.shared_memory` doctor check. For a configured local
TCP PostgreSQL endpoint, discover the unique running Docker container publishing
that endpoint on container port 5432. Inspect its shared memory allocation and
run `df -Pk /dev/shm` inside that container, never on the daemon host. Warn when
the allocation is below 256 MiB or usage exceeds 80%. Report unavailable
observations explicitly, including partial observations; do not claim health
for remote databases, socket endpoints, ambiguous containers, or failed probes.
Commands must be asynchronous, bounded, cancelled cleanly, and read-only.

Installation documentation must direct Docker users to this committed Compose
definition. `aq install` provisions native apt/Homebrew services or reuses an
existing server; it does not create Docker containers and must not reconfigure
an existing server. Container recreation, daemon lifecycle, and resetting the
temporary `max_parallel_workers_per_gather=0` mitigation belong to the operator.

Document the query-lifetime audit and distinguish evidence from hypotheses.
Old DSM files alone do not establish a query leak: PostgreSQL intentionally
retains session DSM and shared cumulative statistics areas. Do not alter query
semantics without a demonstrated resource-lifetime defect.
