# Migrate a PostgreSQL 18 Compose volume

The official PostgreSQL 18 image uses `PGDATA=/var/lib/postgresql/18/docker`
and declares `VOLUME /var/lib/postgresql`. Older versions of this repository's
`docker-compose.yml` mounted `pgdata` at `/var/lib/postgresql/data`. With that
configuration, Docker also creates an **anonymous** volume at
`/var/lib/postgresql`, and the live cluster goes there. The named `pgdata`
volume may be empty. Recreating the container after changing the mount can
start a fresh, empty cluster instead of the existing one.

This procedure is for an operator **outside an AQ worker worktree**. It requires
a maintenance window and space for a separate backup. Do not run `docker
compose down -v`, remove either volume, or recreate the container before the
backup and stopped copy are complete. The repository's corrected Compose file
mounts `pgdata` at `/var/lib/postgresql`; keep the old container and anonymous
volume until the migrated cluster has been verified.

The Compose file sets `name: agent-queue2`, so the repository root and every
worktree slot manage the **same** `aq-postgres` container. `docker compose up`
recreates it whenever the running checkout's file mounts differently from the
container: before the migration, from a checkout with the corrected file;
afterwards, from one with the old file. Either recreation starts an empty
cluster. Before the window, make sure no checkout or automation will run
Compose against `postgres`, and afterwards bring every checkout up to the
corrected file. The container's `com.docker.compose.project.working_dir` label
names the checkout that last created it.

## Identify the source and destination

Run from the checkout containing `docker-compose.yml`. Capture the actual
volume names before stopping or recreating the container:

```bash
source_volume=$(docker inspect aq-postgres --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql"}}{{.Name}}{{end}}{{end}}')
target_volume=$(docker inspect aq-postgres --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}')
printf 'source=%s\ntarget=%s\n' "$source_volume" "$target_volume"
docker exec aq-postgres psql -U agent_queue -tAc 'show data_directory'
docker volume inspect "$source_volume" "$target_volume"
```

Proceed only if the source is an anonymous volume, the target is the intended
named `pgdata` volume, and `data_directory` is
`/var/lib/postgresql/18/docker`. If the mounts differ, investigate that
installation rather than applying these commands. Record the names and the
container ID in the maintenance log.

## Stop writes and back up the original volume

Stop every writer first. If the AQ daemon uses this PostgreSQL service, an
operator should run `aq stop --keep-sessions` outside a worker slot. Then stop
PostgreSQL cleanly:

```bash
docker compose stop postgres
docker inspect aq-postgres --format '{{.State.Status}}'
```

Confirm the status is `exited`. Check that the cluster state is `shut down`
before copying files:

```bash
docker run --rm --entrypoint pg_controldata \
  --mount "type=volume,src=$source_volume,dst=/source,readonly" \
  postgres:18-alpine /source/18/docker
```

Choose an **absolute path on storage with enough free space** for a full copy
of the cluster. Keep the backup outside both Docker volumes. The archive
contains database contents and must be protected accordingly:

```bash
backup_dir=/absolute/path/to/secure/backup-directory
install -d -m 700 "$backup_dir"
docker run --rm --entrypoint sh \
  --mount "type=volume,src=$source_volume,dst=/source,readonly" \
  --mount "type=bind,src=$backup_dir,dst=/backup" \
  postgres:18-alpine -ec \
  'test -f /source/18/docker/PG_VERSION; tar -C /source -cpf /backup/pg18-volume.tar .'
sha256sum "$backup_dir/pg18-volume.tar" > "$backup_dir/pg18-volume.tar.sha256"
sha256sum -c "$backup_dir/pg18-volume.tar.sha256"
tar -tf "$backup_dir/pg18-volume.tar" | grep -F './18/docker/PG_VERSION'
```

Verify the archive and its checksum, and retain it before proceeding. If
backup or cluster-state verification fails, leave PostgreSQL stopped and
resolve that failure first.

## Copy to the named volume and start

The target must be empty; the command below refuses to overwrite files. Copy
only while the source cluster remains stopped. If anything starts the container
during the copy, discard the target's contents and repeat the copy from a clean
shutdown. Re-run the `pg_controldata` check above immediately before copying:

```bash
docker run --rm --entrypoint sh \
  --mount "type=volume,src=$target_volume,dst=/target,readonly" \
  postgres:18-alpine -ec 'test -z "$(ls -A /target)"'

docker run --rm --entrypoint sh \
  --mount "type=volume,src=$source_volume,dst=/source,readonly" \
  --mount "type=volume,src=$target_volume,dst=/target" \
  postgres:18-alpine -ec \
  'test -f /source/18/docker/PG_VERSION; test ! -e /target/18; cp -a /source/. /target/; test -f /target/18/docker/PG_VERSION'
```

Confirm that both copies report the same shut-down state and checkpoint before
starting anything:

```bash
for volume in "$source_volume" "$target_volume"; do
  docker run --rm --entrypoint pg_controldata \
    --mount "type=volume,src=$volume,dst=/data,readonly" \
    postgres:18-alpine /data/18/docker |
    grep -E '^(Database cluster state|Latest checkpoint location):'
done
```

Both must show `shut down` and the same `Latest checkpoint location`.

Use the corrected `docker-compose.yml` from this change, whose sole PostgreSQL
mount is `pgdata:/var/lib/postgresql`. Check the rendered configuration before
recreating the container:

```bash
docker compose config --quiet
docker compose up -d --no-deps postgres
docker inspect aq-postgres --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}'
docker exec aq-postgres psql -U agent_queue -tAc 'show data_directory'
docker compose ps postgres
```

Confirm that the named `pgdata` volume is mounted at `/var/lib/postgresql`,
there is no anonymous parent mount, the data directory remains
`/var/lib/postgresql/18/docker`, and the expected databases and application
data are present. If AQ was stopped, an operator may then run
`aq start --no-dashboard` and verify daemon readiness and a live terminal.
Keep the original anonymous volume and backup until those checks pass and the
operator decides retention is no longer needed. If verification fails, stop
PostgreSQL and investigate using the preserved source volume and backup; do
not initialize or delete either copy as a recovery attempt.

The image path change is documented by the
[official PostgreSQL image documentation](https://hub.docker.com/_/postgres#pgdata).
