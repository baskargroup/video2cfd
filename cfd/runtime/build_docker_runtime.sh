#!/usr/bin/env bash
set -euo pipefail

TAG="${TAG:-video2cfd-openfoam-runtime:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build -t "$TAG" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"
docker image inspect "$TAG" --format '{{.Id}}'
