#!/usr/bin/env bash
# Run a generated case inside the Apptainer/Singularity runtime image
# (OpenFOAM v2412 plus the scalarTransportFoamTurbulent binary). This is a thin
# wrapper: it enters the container, sources the OpenFOAM bashrc and calls the
# case's own ./Allrun, which does all the work.
#
# Usage:
#   bash cfd/scripts/run_openfoam_apptainer.sh <case_dir>
#   NPROCS=56 bash cfd/scripts/run_openfoam_apptainer.sh <case_dir>
#   MESH_ONLY=1 bash cfd/scripts/run_openfoam_apptainer.sh <case_dir>
#
# Environment variables:
#   CONTAINER    path to the .sif image (default ./worker_hpc_sct07.sif, the
#                file written by cfd/runtime/pull_apptainer_image.sh).
#   NPROCS, MESH_ONLY, STAGES, FORCE, VERBOSE, MPI_LAUNCHER, FLOW_SOLVER,
#   SCALAR_SOLVER, SCALAR_FIELD, REQUIRED_PATCHES
#                passed through to Allrun (see ./Allrun --help in the case).
#                MPI_LAUNCHER runs inside the container (default
#                "mpirun -np $NPROCS", single node).
#   APPTAINER    container runtime executable (default: apptainer, else
#                singularity).
#   FOAM_BASHRC  OpenFOAM bashrc inside the image
#                (default /usr/lib/openfoam/openfoam2412/etc/bashrc).
set -euo pipefail

CASE_DIR="${1:?Usage: bash cfd/scripts/run_openfoam_apptainer.sh <case_dir>}"
CONTAINER="${CONTAINER:-./worker_hpc_sct07.sif}"
FOAM_BASHRC="${FOAM_BASHRC:-/usr/lib/openfoam/openfoam2412/etc/bashrc}"

if [ ! -d "$CASE_DIR" ]; then
    echo "ERROR: case directory not found: $CASE_DIR" >&2
    exit 1
fi
case_abs="$(cd "$CASE_DIR" && pwd)"
if [ ! -f "$case_abs/Allrun" ]; then
    echo "ERROR: $case_abs/Allrun not found. Generate the case with cfd/scripts/01_prepare_openfoam_case.py (it copies cfd/templates/Allrun into the case)." >&2
    exit 1
fi
if [ ! -f "$CONTAINER" ]; then
    echo "ERROR: container image not found: $CONTAINER" >&2
    echo "       Create it with 'bash cfd/runtime/pull_apptainer_image.sh' or set CONTAINER=/path/to/worker_hpc_sct07.sif" >&2
    exit 1
fi
container_abs="$(cd "$(dirname "$CONTAINER")" && pwd)/$(basename "$CONTAINER")"

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
case_q="$(printf '%q' "$case_abs")"
inner="${forward}if [ ! -f ${bashrc_q} ]; then echo \"ERROR: OpenFOAM bashrc not found in the image: \"${bashrc_q} >&2; exit 1; fi; set +u; source ${bashrc_q}; export PATH=\"\$PATH:/app/bin\"; cd ${case_q} && exec bash ./Allrun"

echo "CASE_DIR=$case_abs"
echo "CONTAINER=$container_abs"
echo "NPROCS=${NPROCS:-1}"

"$runtime" exec --bind "$case_abs":"$case_abs" "$container_abs" /bin/bash -c "$inner"
