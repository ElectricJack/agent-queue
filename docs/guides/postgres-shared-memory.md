# PostgreSQL shared memory

Use the repository's `docker-compose.yml` for Docker PostgreSQL installations.
It gives both services `shm_size: 1g`. This is the tmpfs capacity, not a change
to `shared_buffers` or a reservation of 1 GiB of resident memory. The native
apt/Homebrew path in `aq install` has no Docker tmpfs to configure; an existing
Docker server is reused as configured by its operator.

`aq doctor --check db.shared_memory` warns if the serving container's allocation
is below 256 MiB or its `/dev/shm` usage exceeds 80%. It matches a local TCP
database endpoint to a unique running container publishing port 5432 and reads
`df` inside that container. Remote and socket endpoints, missing Docker access,
ambiguous matches, and failed probes are reported as unavailable observations,
not healthy results. This check never recreates containers or changes PostgreSQL.

## Applying the capacity change

An operator outside a worker slot owns this maintenance window. Record the
running container's mounts and take a verified backup first. For the October 25
incident, the verified cluster was `agent-queue2_pgdata` mounted at
`/var/lib/postgresql`, using `/var/lib/postgresql/18/docker` as PGDATA. Keep the
Compose project `agent-queue2`, service `postgres`, and that volume identity.
If the installation instead has an anonymous parent volume, follow the
[PostgreSQL 18 volume migration guide](postgres18-volume-migration.md) first.

Stop writers with `aq stop --keep-sessions`, then from the canonical checkout:

```bash
docker compose config --quiet
docker compose up -d --no-deps postgres
docker inspect aq-postgres --format '{{.HostConfig.ShmSize}}'
docker exec aq-postgres df -Pk /dev/shm
docker inspect aq-postgres --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}'
docker exec aq-postgres psql -U agent_queue -d agent_queue -tAc 'SHOW data_directory'
```

Verify `1073741824` bytes, the intended named volume, and the existing application
data. Do not use `docker compose down -v`. Once capacity and database contents
are verified, remove the temporary incident mitigation:

```bash
docker exec aq-postgres psql -U agent_queue -d agent_queue -v ON_ERROR_STOP=1 \
  -c 'ALTER SYSTEM RESET max_parallel_workers_per_gather'
docker exec aq-postgres psql -U agent_queue -d agent_queue -v ON_ERROR_STOP=1 \
  -c 'SELECT pg_reload_conf()'
docker exec aq-postgres psql -U agent_queue -d agent_queue \
  -c 'SHOW max_parallel_workers_per_gather'
aq start --no-dashboard
aq doctor --check db.shared_memory
```

The PostgreSQL default is 2; check the effective value in a new connection.
Verify daemon readiness, a live worker terminal, and the absence of repeated
`wait.listener_restarted`/`DiskFullError` messages. Recheck shared memory usage
under normal parallel query load before declaring the incident resolved.

## October 25 query-lifetime audit

Supervisor evidence captured at 21:48Z showed 64 MiB configured, 98–100% used,
and 1/2/4/8/16 MiB segments surviving the approximately 21:42Z daemon restart.
Every client backend was only 2–5 minutes old; there were no long transactions,
and observed idle-in-transaction intervals were below one second. Old segments
were still being touched after the restart. This rules out an old daemon cursor
or transaction as the explanation for those surviving segments.

The wait source scan was the failed allocator (196736 bytes for a parallel
plan); that does not establish it as the owner of the large retained segments.
The static audit found:

* `src/waits/listener.py`: `_connect` owns a dedicated asyncpg LISTEN connection;
  `run` closes it in `finally`. Source drains use scoped engine transactions
  with bounded event/lease timeouts; sleeping for notifications happens outside
  those transactions.
* `src/database/queries/wait_source_queries.py`: `wait_source_watermark` scopes
  its READ COMMITTED transaction and advisory lock, including its bounded retry.
  Results are materialized before that scope exits.
* `src/database/queries/observation_snapshot_queries.py`: the only explicit
  database stream, `bounded_rows`, uses an async context manager, including
  oversize/error exits. `consistent_read` owns the surrounding transaction.
* `src/database/engine.py`: pooled connections use SQLAlchemy context cleanup
  and default rollback-on-return. The default 1800-second recycle applies at
  the next checkout, not as a timer that kills an active query.

No query-lifetime defect was demonstrated, so query semantics were preserved.
PostgreSQL intentionally retains [session DSM](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/access/common/session.c)
and stores variable-sized [cumulative statistics in dynamic shared memory](https://www.postgresql.org/message-id/E1ncJtG-000gDK-1G%40gemulon.postgresql.org).
The doubling sizes and survival across client disconnects are consistent with
a long-lived DSA area, such as shared statistics, but do not identify its owner
conclusively. If growth continues after resizing, capture segment-to-process
mappings and `pg_stat_activity` together before recycling anything; investigate
the owning PostgreSQL subsystem from that evidence. Never unlink live DSM files.
