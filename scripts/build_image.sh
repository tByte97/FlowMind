#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

commit=$(git rev-parse HEAD)
export FLOWMIND_GIT_COMMIT="$commit"

docker compose -f compose.yaml -f compose.server.yaml build "$@"

image_commit=$(docker image inspect flowmind:local \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
if [ "$image_commit" != "$commit" ]; then
  echo "FlowMind image revision mismatch: expected $commit, got $image_commit" >&2
  exit 1
fi

echo "Built flowmind:local from $commit"
