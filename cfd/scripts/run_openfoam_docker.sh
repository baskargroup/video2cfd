#!/usr/bin/env bash
set -euo pipefail

CASE_DIR="${1:?Usage: bash run_openfoam_docker.sh <case_dir>}"
IMAGE="${IMAGE:-video2cfd-openfoam-runtime:latest}"

cd "$CASE_DIR"

docker run --rm \
  --entrypoint /bin/bash \
  -v "$PWD":"$PWD" \
  -w "$PWD" \
  "$IMAGE" \
  -lc '
    source /usr/lib/openfoam/openfoam2412/etc/bashrc
    blockMesh | tee log.blockMesh
    surfaceFeatureExtract | tee log.sFE
    snappyHexMesh -overwrite | tee log.sHM
    topoSet | tee log.topoSet
    createPatch -overwrite | tee log.createPatch
    checkMesh | tee log.checkMesh
  '
