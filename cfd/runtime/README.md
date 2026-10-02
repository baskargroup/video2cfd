# Runtime environment

The production CFD workflow used OpenFOAM 2412 and a custom scalar solver named
`scalarTransportFoamTurbulent`.

## Published runtime image

The reproducibility Docker image is:

```text
jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07
```

For HPC use, create an Apptainer/Singularity image from that Docker image:

```bash
bash cfd/runtime/pull_apptainer_image.sh
```

which runs:

```bash
apptainer pull worker_hpc_sct07.sif docker://jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07
```

The `.sif` file is intentionally not committed to git.

## Dockerfile

`cfd/runtime/Dockerfile` is a small wrapper around the published runtime image.
Build it with:

```bash
bash cfd/runtime/build_docker_runtime.sh
```

This is enough for users to rebuild a local Docker runtime layer and to pull a
matching Apptainer image on HPC systems.

## Important limitation

The current repository does not contain the source code for
`scalarTransportFoamTurbulent`. Therefore the repo can reproduce the workflow by
using the published runtime image, but it cannot yet rebuild the custom scalar
solver from source. Full source-level reproducibility requires adding the solver
source under a future directory such as:

```text
cfd/solvers/scalarTransportFoamTurbulent/
```

Local Docker run:

```bash
bash cfd/scripts/run_openfoam_docker.sh runs/cfd/<case_name>
```

HPC SLURM run:

```bash
export CONTAINER=/path/to/worker_hpc_sct07.sif
sbatch -A <allocation> cfd/scripts/run_openfoam_case.slurm runs/cfd/<case_name>
```
