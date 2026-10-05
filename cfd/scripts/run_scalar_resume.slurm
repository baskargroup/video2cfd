#!/bin/bash
#SBATCH -J video2cfd_scalar
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
# Resume (or re-run) ONLY the scalar stage of a case whose mesh and flow solve
# are complete, e.g. after run_openfoam_case.slurm ran out of wall time during
# the scalar decay run. It calls the case's ./Allrun with STAGES=scalar, which
#   - leaves an existing scalar field untouched (the field transfer from time 0
#     is only done if the last time directory has no scalar field yet),
#   - installs system/controlDict.scalar (startFrom latestTime),
#   - runs scalarTransportFoamTurbulent from the latest time directory,
#   - runs reconstructPar -latestTime.
# The previous solver log is kept as log.scalarTransportFoamTurbulent.<date>.
# The probe output of the resumed run is written to a new folder
# postProcessing/monitors/<restartTime>/; cfd/scripts/06_extract_probe_csv.py
# joins the pieces.
#
# Usage (job log <jobname>_<jobid>.out/.err goes to the submit directory):
#   export CONTAINER=/path/to/worker_hpc_sct07.sif
#   sbatch -p small -A <allocation> cfd/scripts/run_scalar_resume.slurm <case_dir>
#
# Environment variables: CONTAINER, NPROCS (default SLURM_NTASKS; must equal the
# number of processor directories of the case), MPI_LAUNCHER, VERBOSE,
# SCALAR_SOLVER, SCALAR_FIELD, APPTAINER, FOAM_BASHRC - as in
# run_openfoam_case.slurm.

set -euo pipefail

# On TACC systems the container runtime is a module:
# module load tacc-apptainer

CASE_DIR="${1:?Usage: sbatch -p <queue> -A <allocation> run_scalar_resume.slurm <case_dir>}"
CONTAINER="${CONTAINER:-./worker_hpc_sct07.sif}"
FOAM_BASHRC="${FOAM_BASHRC:-/usr/lib/openfoam/openfoam2412/etc/bashrc}"
NPROCS="${NPROCS:-${SLURM_NTASKS:-56}}"
STAGES="scalar"
export NPROCS STAGES

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

# The scalar solve and the reconstruction are run again; the field-transfer
# marker (status/10_fieldTransfer.done) is kept.
rm -f "$case_abs/status/11_scalarTransport.done" "$case_abs/status/12_reconstructPar.done"

# Settings forwarded to Allrun inside the container (only those that are set).
forward=""
for var in NPROCS MPI_LAUNCHER STAGES VERBOSE SCALAR_SOLVER SCALAR_FIELD; do
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
echo "STAGES=$STAGES"
date

"$runtime" exec --bind "$case_abs":"$case_abs" "$container_abs" /bin/bash -c "$inner"

date
echo "DONE"
