#!/usr/bin/env bash
# Optional: create a local Docker tag for the runtime image.
#
# cfd/runtime/Dockerfile only re-tags the published runtime image (pinned by
# digest) and adds labels; nothing is compiled. cfd/scripts/run_openfoam_docker.sh
# does not need this step, because it uses the published image by digest unless
# IMAGE is set. To run with the local tag built here:
#
#   bash cfd/runtime/build_docker_runtime.sh
#   IMAGE=video2cfd-openfoam-runtime:latest bash cfd/scripts/run_openfoam_docker.sh <case_dir>
set -euo pipefail

TAG="${TAG:-video2cfd-openfoam-runtime:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build -t "$TAG" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"
echo "Built $TAG, image id:"
docker image inspect "$TAG" --format '{{.Id}}'
