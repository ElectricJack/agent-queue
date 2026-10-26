#!/usr/bin/env bash
# Disposable CI database only. Keep registry failures separate from startup failures.
set -euo pipefail

# GHCR and the public mirrors serve the same pinned official PostgreSQL index.
# Rotate sources each round so one unavailable registry cannot delay fallback.
# GHCR paths are lowercase even when the repository owner is not.
ghcr_image="ghcr.io/${GHCR_OWNER,,}/agent-queue-postgres:18@$POSTGRES_DIGEST"
for pull_attempt in 1 2 3; do
  for postgres_image in \
    "$ghcr_image" \
    "mirror.gcr.io/library/postgres:18@$POSTGRES_DIGEST" \
    "public.ecr.aws/docker/library/postgres:18@$POSTGRES_DIGEST"; do
    if docker pull "$postgres_image"; then
      break 2
    else
      pull_status=$?
    fi
    echo "::warning::PostgreSQL image pull failed from $postgres_image (round $pull_attempt/3)"
  done
  if (( pull_attempt == 3 )); then
    echo "::error::PostgreSQL image pull failed from all registries after $pull_attempt rounds"
    exit "$pull_status"
  fi
  sleep 5
done

# A container-start error is fatal: fallback only repairs image fetching.
docker run --pull=never --detach --name ci-postgres \
  --publish 127.0.0.1:5432:5432 \
  --env POSTGRES_USER=agent_queue \
  --env POSTGRES_PASSWORD=agent_queue \
  --env POSTGRES_DB=agent_queue \
  "$postgres_image" \
  -c max_connections=300 \
  -c fsync=off \
  -c synchronous_commit=off \
  -c full_page_writes=off
for attempt in {1..60}; do
  # The entrypoint's temporary initialization server listens only on a Unix
  # socket; require TCP so migrations and scenarios see the final server.
  if docker exec ci-postgres pg_isready \
    -h 127.0.0.1 -U agent_queue -d agent_queue; then
    exit 0
  fi
  sleep 1
done
echo "::error::PostgreSQL did not become ready after $attempt attempts"
exit 1
