#!/usr/bin/env bash
set -euo pipefail

CASE_DIR="${1:?Usage: bash cfd/scripts/run_openfoam_apptainer.sh <case_dir>}"
CONTAINER="${CONTAINER:-worker_hpc_sct07.sif}"
NPROCS="${NPROCS:-1}"
MPI_LAUNCHER="${MPI_LAUNCHER:-mpirun -np ${NPROCS}}"
case_abs="$(cd "$CASE_DIR" && pwd)"

run_of() {
  apptainer exec --bind "$case_abs":"$case_abs" "$CONTAINER" bash -lc "
    source /usr/lib/openfoam/openfoam2412/etc/bashrc
    cd '$case_abs'
    $*
  "
}

cd "$case_abs"
mkdir -p logs status

run_of "blockMesh | tee log.blockMesh"
run_of "surfaceFeatureExtract | tee log.sFE"
run_of "snappyHexMesh -overwrite | tee log.sHM"
run_of "topoSet | tee log.topoSet"
run_of "createPatch -overwrite | tee log.createPatch"

if [ "$NPROCS" -gt 1 ]; then
  run_of "decomposePar -force | tee log.decomposePar"
  run_of "$MPI_LAUNCHER simpleFoam -parallel | tee log.simpleFoam"
  run_of "cp system/controlDict.scalar system/controlDict"
  run_of "$MPI_LAUNCHER scalarTransportFoamTurbulent -parallel | tee log.scalarTransport"
else
  run_of "simpleFoam | tee log.simpleFoam"
  run_of "cp system/controlDict.scalar system/controlDict"
  run_of "scalarTransportFoamTurbulent | tee log.scalarTransport"
fi
