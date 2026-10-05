#!/usr/bin/env bash
# Run a generated case inside the Docker runtime image (OpenFOAM v2412 plus the
# scalarTransportFoamTurbulent binary). This is a thin wrapper: it starts the
# container, sources the OpenFOAM bashrc and calls the case's own ./Allrun,
# which does all the work (meshing, flow solve, field transfer, scalar run).
#
# Usage:
#   bash cfd/scripts/run_openfoam_docker.sh <case_dir>
#   NPROCS=8 bash cfd/scripts/run_openfoam_docker.sh <case_dir>
#   MESH_ONLY=1 bash cfd/scripts/run_openfoam_docker.sh <case_dir>
#
# Environment variables:
#   IMAGE        Docker image (default: the published runtime image, pinned by
#                digest; tag hpc-openfoam2412-sct07 of
#                jamesafful/classroom-cfd-worker).
#   NPROCS, MESH_ONLY, STAGES, FORCE, VERBOSE, MPI_LAUNCHER, FLOW_SOLVER,
#   SCALAR_SOLVER, SCALAR_FIELD, REQUIRED_PATCHES
#                passed through to Allrun (see ./Allrun --help in the case).
#   DOCKER_USER  user for "docker run -u" (default: the calling user, so the
#                results are not owned by root). With rootless Docker or Podman
#                use DOCKER_USER=0:0, which maps to the calling user there.
#   SHM_SIZE     size of /dev/shm in the container (default 2g; MPI uses it).
#   FOAM_BASHRC  OpenFOAM bashrc inside the image
#                (default /usr/lib/openfoam/openfoam2412/etc/bashrc).
set -euo pipefail

DEFAULT_IMAGE="jamesafful/classroom-cfd-worker@sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c"

CASE_DIR="${1:?Usage: bash cfd/scripts/run_openfoam_docker.sh <case_dir>}"
IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
FOAM_BASHRC="${FOAM_BASHRC:-/usr/lib/openfoam/openfoam2412/etc/bashrc}"
SHM_SIZE="${SHM_SIZE:-2g}"

if [ ! -d "$CASE_DIR" ]; then
    echo "ERROR: case directory not found: $CASE_DIR" >&2
    exit 1
fi
case_abs="$(cd "$CASE_DIR" && pwd)"
if [ ! -f "$case_abs/Allrun" ]; then
    echo "ERROR: $case_abs/Allrun not found. Generate the case with cfd/scripts/01_prepare_openfoam_case.py (it copies cfd/templates/Allrun into the case)." >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found on PATH." >&2
    exit 1
fi

# Settings forwarded to Allrun inside the container (only those that are set).
forward=""
for var in NPROCS MPI_LAUNCHER MESH_ONLY STAGES FORCE VERBOSE FLOW_SOLVER SCALAR_SOLVER SCALAR_FIELD REQUIRED_PATCHES; do
    if [ -n "${!var:-}" ]; then
        forward+="$(printf 'export %s=%q; ' "$var" "${!var}")"
    fi
done

# The scalar solver is expected on PATH inside the image; /app/bin (its location
# in the production layout) is appended as a fallback.
bashrc_q="$(printf '%q' "$FOAM_BASHRC")"
inner="${forward}if [ ! -f ${bashrc_q} ]; then echo \"ERROR: OpenFOAM bashrc not found in the image: \"${bashrc_q} >&2; exit 1; fi; set +u; source ${bashrc_q}; export PATH=\"\$PATH:/app/bin\"; exec bash ./Allrun"

user_args=()
if [ -n "${DOCKER_USER:-}" ]; then
    user_args=(-u "$DOCKER_USER")
elif uid="$(id -u 2>/dev/null)" && gid="$(id -g 2>/dev/null)"; then
    user_args=(-u "${uid}:${gid}")
fi

echo "CASE_DIR=$case_abs"
echo "IMAGE=$IMAGE"
echo "NPROCS=${NPROCS:-1}"

# HOME=/tmp: the calling user usually has no home directory inside the image.
# OMPI_ALLOW_RUN_AS_ROOT*: lets mpirun start if the container user is root.
docker run --rm --init \
    ${user_args[@]+"${user_args[@]}"} \
    -e HOME=/tmp \
    -e OMPI_ALLOW_RUN_AS_ROOT=1 \
    -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
    --shm-size "$SHM_SIZE" \
    -v "$case_abs":"$case_abs" \
    -w "$case_abs" \
    --entrypoint /bin/bash \
    "$IMAGE" \
    -c "$inner"
