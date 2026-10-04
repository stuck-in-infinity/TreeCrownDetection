#!/usr/bin/env bash
# Build and push <user>/treecrown-workstation:<tag> (CPU) and :<tag>-cu128 (GPU).
# Usage: ./publish.sh <dockerhub-username> [tag, default v1]
# Set NO_PUSH to 1 to build without pushing.
set -euo pipefail
USER="${1:?usage: ./publish.sh <dockerhub-username> [tag]}"
TAG="${2:-v1}"
REPO="$USER/treecrown-workstation"
cd "$(dirname "$0")"

# Pass the host proxy into the build.
PROXY_ARGS=()
for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
  [ -n "${!v:-}" ] && PROXY_ARGS+=(--build-arg "$v=${!v}")
done

docker build "${PROXY_ARGS[@]}" \
  --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cpu \
  -t "$REPO:$TAG" .
docker build "${PROXY_ARGS[@]}" \
  --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu128 \
  -t "$REPO:$TAG-cu128" .

if [ -z "${NO_PUSH:-}" ]; then
  docker push "$REPO:$TAG"
  docker push "$REPO:$TAG-cu128"
fi

echo
echo "Built${NO_PUSH:+ (not pushed)}:"
echo "  $REPO:$TAG         (CPU)"
echo "  $REPO:$TAG-cu128   (GPU)"
echo
echo "On the workstation, set this in .env (or export it) to pin one:"
echo "  IMAGE_API=$REPO:$TAG-cu128     # or :$TAG on a CPU-only host"
echo "then:  docker compose -f docker-compose.hub.yml pull && docker compose -f docker-compose.hub.yml up -d"
