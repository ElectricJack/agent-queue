#!/usr/bin/env bash
# Disposable CI database only. Keep registry failures separate from startup failures.
set -euo pipefail

# All sources publish Docker's official PostgreSQL image. Prefer mirrors to
# Docker Hub's anonymous rate limit, but permit fallback when a mirror has
# missing blobs or a hosted runner exhausts ECR Public's anonymous data quota.
# Alpine keeps cold downloads small and matches the local test service.
postgres_image=""
pull_status=1
for candidate_image in \
  mirror.gcr.io/library/postgres:18-alpine \
  public.ecr.aws/docker/library/postgres:18-alpine \
  docker.io/library/postgres:18-alpine; do
  for pull_attempt in 1 2 3; do
    if docker pull "$candidate_image"; then
      postgres_image="$candidate_image"
      break 2
    else
      pull_status=$?
    fi
    if (( pull_attempt < 3 )); then
      echo "::warning::PostgreSQL image pull failed ($candidate_image, attempt $pull_attempt/3); retrying in 5s"
      sleep 5
    fi
  done
  echo "::warning::PostgreSQL registry exhausted ($candidate_image)"
done
if [[ -z "$postgres_image" ]]; then
  echo "::error::PostgreSQL image pull failed from all three registries"
  exit "$pull_status"
fi

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
