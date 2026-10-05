#!/usr/bin/env bash
# Pull the runtime image (OpenFOAM v2412 plus scalarTransportFoamTurbulent) as
# an Apptainer/Singularity .sif file. The image is referenced by digest (tag
# hpc-openfoam2412-sct07 of jamesafful/classroom-cfd-worker), because the tag
# is mutable.
#
# Usage:
#   bash cfd/runtime/pull_apptainer_image.sh           # writes ./worker_hpc_sct07.sif
#   OUT=/path/to/worker_hpc_sct07.sif bash cfd/runtime/pull_apptainer_image.sh
#
# Environment variables:
#   IMAGE      image reference without the docker:// prefix (default: pinned digest)
#   OUT        output file (default ./worker_hpc_sct07.sif)
#   APPTAINER  container runtime executable (default: apptainer, else singularity)
set -euo pipefail

DEFAULT_IMAGE="jamesafful/classroom-cfd-worker@sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c"

IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
OUT="${OUT:-worker_hpc_sct07.sif}"

runtime="${APPTAINER:-}"
if [ -z "$runtime" ]; then
    if command -v apptainer >/dev/null 2>&1; then
        runtime="apptainer"
    elif command -v singularity >/dev/null 2>&1; then
        runtime="singularity"
    else
        echo "ERROR: neither apptainer nor singularity found on PATH (on TACC systems: module load tacc-apptainer)." >&2
        exit 1
    fi
fi

echo "Pulling docker://$IMAGE"
echo "     to $OUT"
"$runtime" pull "$OUT" "docker://$IMAGE"
sha256sum "$OUT" | tee "${OUT}.sha256"
echo "Image reference: docker://$IMAGE"
