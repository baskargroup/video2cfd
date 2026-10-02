#!/usr/bin/env bash
#SBATCH -J video2cfd_openfoam
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#SBATCH -p normal
#SBATCH -N 1
#SBATCH -n 56
#SBATCH -t 48:00:00
#SBATCH -A YOUR_ALLOCATION

set -euo pipefail

CASE_DIR="${1:?Usage: sbatch run_openfoam_case.slurm <case_dir>}"
CONTAINER="${CONTAINER:-worker_hpc_sct07.sif}"
NPROCS="${SLURM_NTASKS:-56}"
MPI_LAUNCHER="${MPI_LAUNCHER:-mpirun -np ${NPROCS}}"

cd "$CASE_DIR"
mkdir -p logs status

echo "CASE_DIR=$PWD"
echo "CONTAINER=$CONTAINER"
echo "NPROCS=$NPROCS"
echo "MPI_LAUNCHER=$MPI_LAUNCHER"
date

run_of()
{
  apptainer exec --bind "$PWD":"$PWD" "$CONTAINER" bash -lc "
    source /usr/lib/openfoam/openfoam2412/etc/bashrc
    cd '$PWD'
    $*
  "
}

if [ ! -f status/01_blockMesh.done ]; then
  run_of "blockMesh | tee log.blockMesh"
  touch status/01_blockMesh.done
fi

if [ ! -f status/02_surfaceFeatureExtract.done ]; then
  run_of "surfaceFeatureExtract | tee log.sFE"
  touch status/02_surfaceFeatureExtract.done
fi

if [ ! -f status/03_snappyHexMesh.done ]; then
  run_of "snappyHexMesh -overwrite | tee log.sHM"
  touch status/03_snappyHexMesh.done
fi

if [ ! -f status/04_topoSet.done ]; then
  run_of "topoSet | tee log.topoSet"
  touch status/04_topoSet.done
fi

if [ ! -f status/05_createPatch.done ]; then
  run_of "createPatch -overwrite | tee log.createPatch"
  touch status/05_createPatch.done
fi

if [ ! -f status/06_decomposePar.done ]; then
  run_of "decomposePar -force | tee log.decomposePar"
  touch status/06_decomposePar.done
fi

if [ ! -f status/07_simpleFoam.done ]; then
  if [ -f system/controlDict.flow ]; then
    run_of "cp system/controlDict.flow system/controlDict"
  fi
  run_of "$MPI_LAUNCHER simpleFoam -parallel | tee log.simpleFoam"
  touch status/07_simpleFoam.done
fi

if [ ! -f status/08_scalarTransport.done ]; then
  if [ -f system/controlDict.scalar ]; then
    run_of "cp system/controlDict.scalar system/controlDict"
  fi
  run_of "$MPI_LAUNCHER scalarTransportFoamTurbulent -parallel | tee log.scalarTransport"
  touch status/08_scalarTransport.done
fi

if [ ! -f status/09_reconstructPar.done ]; then
  run_of "reconstructPar -latestTime | tee log.reconstructPar"
  touch status/09_reconstructPar.done
fi

date
echo "DONE"
