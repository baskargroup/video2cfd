#!/usr/bin/env bash
#SBATCH -J video2cfd_scalar
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#SBATCH -p normal
#SBATCH -N 1
#SBATCH -n 56
#SBATCH -t 48:00:00
#SBATCH -A YOUR_ALLOCATION

set -euo pipefail

CASE_DIR="${1:?Usage: sbatch run_scalar_resume.slurm <case_dir>}"
CONTAINER="${CONTAINER:-worker_hpc_sct07.sif}"
NPROCS="${SLURM_NTASKS:-56}"

cd "$CASE_DIR"
mkdir -p logs status

apptainer exec --bind "$PWD":"$PWD" "$CONTAINER" bash -lc "
  source /usr/lib/openfoam/openfoam2412/etc/bashrc
  cd '$PWD'
  mpirun -np $NPROCS scalarTransportFoamTurbulent -parallel | tee -a log.scalarTransport_resume
  reconstructPar -latestTime | tee -a log.reconstructPar
"

touch status/09_reconstructPar.done
