#!/usr/bin/env bash
# One-off (safe to re-run): pull the models named in config/settings.yaml into the Ollama volume.
#   deploy/pull-models.sh            skip models that are already present
#   deploy/pull-models.sh --force    pull them again (update)
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d ollama
docker compose --profile init run --rm model-pull python -m cat.pullmodels "$@"
