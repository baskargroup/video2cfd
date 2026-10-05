#!/bin/bash
#SBATCH -J video2cfd_openfoam
#SBATCH -o %x_%j.out
#SBATCH -e %x_%j.err
#SBATCH -N 1
#SBATCH -n 56
#SBATCH -t 48:00:00
# ---- PLACEHOLDERS: replace the next two values, or override them on the
# ---- command line (sbatch -p <queue> -A <allocation> ...). On TACC Frontera,
# ---- for example, one-node jobs go to the "small" queue.
#SBATCH -p YOUR_PARTITION
#SBATCH -A YOUR_ALLOCATION
#
# Full workflow for one generated case on one node: meshing, simpleFoam, field
# transfer, scalarTransportFoamTurbulent, reconstructPar. This is a thin
# wrapper: it enters the Apptainer image, sources the OpenFOAM bashrc and calls
# the case's own ./Allrun. Completed stages leave status/*.done files in the
# case, so a job that ran out of time can be resubmitted with the same command
# (or with run_scalar_resume.slurm if only the scalar run is unfinished).
#
# Usage (from any directory; the job log <jobname>_<jobid>.out/.err is written
# to the directory you submit from, no logs/ folder is needed):
#   export CONTAINER=/path/to/worker_hpc_sct07.sif
#   sbatch -p small -A <allocation> cfd/scripts/run_openfoam_case.slurm <case_dir>
#
# Environment variables (exported to the job by sbatch by default):
#   CONTAINER     .sif image (default ./worker_hpc_sct07.sif in the submit
#                 directory; create it with cfd/runtime/pull_apptainer_image.sh)
#   NPROCS        MPI ranks (default: SLURM_NTASKS, i.e. the -n value)
#   MPI_LAUNCHER  launcher used INSIDE the container (default
#                 "mpirun -np $NPROCS"; the image's own MPI on a single node)
#   MESH_ONLY, STAGES, FORCE, VERBOSE, FLOW_SOLVER, SCALAR_SOLVER, SCALAR_FIELD,
#   REQUIRED_PATCHES   passed through to Allrun (see ./Allrun --help)
#   APPTAINER     container runtime executable (default: apptainer, else
#                 singularity)
#   FOAM_BASHRC   OpenFOAM bashrc inside the image

set -euo pipefail

# On TACC systems the container runtime is a module:
# module load tacc-apptainer

CASE_DIR="${1:?Usage: sbatch -p <queue> -A <allocation> run_openfoam_case.slurm <case_dir>}"
CONTAINER="${CONTAINER:-./worker_hpc_sct07.sif}"
FOAM_BASHRC="${FOAM_BASHRC:-/usr/lib/openfoam/openfoam2412/etc/bashrc}"
NPROCS="${NPROCS:-${SLURM_NTASKS:-56}}"
export NPROCS

if [ ! -d "$CASE_DIR" ]; then
    echo "ERROR: case directory not found: $CASE_DIR (relative paths are resolved from the submit directory)" >&2
    exit 1
fi
case_abs="$(cd "$CASE_DIR" && pwd)"
if [ ! -f "$case_abs/Allrun" ]; then
    echo "ERROR: $case_abs/Allrun not found. Generate the case with cfd/scripts/01_prepare_openfoam_case.py (it copies cfd/templates/Allrun into the case)." >&2
    exit 1
fi
if [ ! -f "$CONTAINER" ]; then
    echo "ERROR: container image not found: $CONTAINER" >&2
    echo "       Create it with 'bash cfd/runtime/pull_apptainer_image.sh' and export CONTAINER=/path/to/worker_hpc_sct07.sif before sbatch." >&2
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
echo "NPROCS=$NPROCS"
echo "MPI_LAUNCHER=${MPI_LAUNCHER:-mpirun -np ${NPROCS}}"
date

"$runtime" exec --bind "$case_abs":"$case_abs" "$container_abs" /bin/bash -c "$inner"

date
echo "DONE"
