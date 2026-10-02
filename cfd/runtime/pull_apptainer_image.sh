#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07}"
OUT="${OUT:-worker_hpc_sct07.sif}"

apptainer pull "$OUT" "docker://$IMAGE"
sha256sum "$OUT" | tee "${OUT}.sha256"
