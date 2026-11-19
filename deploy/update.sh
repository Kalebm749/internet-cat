#!/usr/bin/env bash
# Update to the latest code and restart the services (data and models are untouched).
#   deploy/update.sh             git pull, rebuild our image, restart what changed
#   deploy/update.sh --images    also pull a newer ollama image
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
[ "${1:-}" = "--images" ] && docker compose pull ollama
docker compose build
docker compose up -d
docker compose ps
