#!/usr/bin/env bash
# Builds the container image and runs it as the README tells an owner to: beside a PostgreSQL,
# with nothing set but where that is. Fails unless the collector starts, with no login yet to
# collect with, and the image's own health check finds it healthy.
set -euo pipefail
cd "$(dirname "$0")/.."

image=navimow-collector:smoke
docker build -q -t "$image" . >/dev/null

name=navimow-collector-smoke-$$
dsn=postgresql://postgres@$name-postgres/postgres
docker network create "$name" >/dev/null
trap 'docker rm -f "$name" "$name-postgres" >/dev/null 2>&1; docker network rm "$name" >/dev/null' EXIT

# No password: a throwaway database, reachable only on this network.
docker run -d --name "$name-postgres" --network "$name" -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16 >/dev/null
# Over TCP, which the server that only sets the database up does not listen on.
ready=
for _ in $(seq 60); do
  docker exec "$name-postgres" pg_isready -q -h 127.0.0.1 -U postgres && ready=yes && break
  sleep 1
done
[[ $ready ]] || { echo "PostgreSQL did not start within a minute" >&2; exit 1; }

docker run -d --name "$name" --network "$name" -e NAVIMOW_STORAGE_DSN="$dsn" "$image" >/dev/null
health=
for _ in $(seq 90); do
  health=$(docker inspect -f '{{.State.Health.Status}}' "$name")
  [[ $health == healthy ]] && break
  sleep 1
done
[[ $health == healthy ]] || {
  echo "the collector is ${health:-not running}, not healthy, 90 seconds after starting:" >&2
  docker logs --tail 20 "$name" >&2
  exit 1
}
echo "the collector runs from $image and is healthy"
