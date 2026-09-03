#!/usr/bin/env bash
# Tag the locally-built images and push them to Docker Hub.
# Usage:  ./publish.sh <dockerhub-username> [tag]
set -euo pipefail
USER="${1:?usage: ./publish.sh <dockerhub-username> [tag]}"
TAG="${2:-latest}"

docker tag treecrown-workstation:latest          "$USER/treecrown-workstation:$TAG"
docker tag treecrown-workstation-frontend:latest "$USER/treecrown-frontend:$TAG"

docker push "$USER/treecrown-workstation:$TAG"
docker push "$USER/treecrown-frontend:$TAG"

echo
echo "Pushed:"
echo "  $USER/treecrown-workstation:$TAG"
echo "  $USER/treecrown-frontend:$TAG"
echo
echo "On the workstation, set these in .env (or export them):"
echo "  IMAGE_API=$USER/treecrown-workstation:$TAG"
echo "  IMAGE_FRONTEND=$USER/treecrown-frontend:$TAG"
echo "then:  docker compose -f docker-compose.hub.yml pull && docker compose -f docker-compose.hub.yml up -d"
