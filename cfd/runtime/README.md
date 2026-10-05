# Runtime environment

A generated case needs two things to run: OpenFOAM v2412 and the scalar solver
`scalarTransportFoamTurbulent`. There are two ways to get them.

## Option 1: native OpenFOAM v2412

Install OpenFOAM v2412 (www.openfoam.com), build the solver from
`cfd/solvers/scalarTransportFoamTurbulent/` with `wmake` (see the README in
that folder), and run `./Allrun` in the case directory. No container is needed.

## Option 2: the runtime container image

```text
jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07
sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c
```

The scripts refer to the image by this digest, because the tag can be moved.
They rely on two things in the image:

- OpenFOAM v2412 with its environment file at
  `/usr/lib/openfoam/openfoam2412/etc/bashrc` (override with `FOAM_BASHRC`);
- a `scalarTransportFoamTurbulent` executable on `PATH` (`/app/bin` is added as
  a fallback).

The solver binary in the image was built outside this repository and its
source is not available. The source in `cfd/solvers/` is a re-implementation;
the image is not built from it, and the two have not been compared.

### Docker

```bash
bash cfd/scripts/run_openfoam_docker.sh runs/cfd/<case_name>
NPROCS=8 bash cfd/scripts/run_openfoam_docker.sh runs/cfd/<case_name>
```

The script pulls and uses the published image directly; no build step is
needed. Environment variables: `IMAGE` (another image), `DOCKER_USER` (user for
`docker run -u`; default is the calling user), `SHM_SIZE` (default `2g`), and
the `Allrun` variables (`NPROCS`, `MESH_ONLY`, `STAGES`, ...).

`cfd/runtime/Dockerfile` only re-tags the published image and adds labels; it
compiles nothing. Building it is optional:

```bash
bash cfd/runtime/build_docker_runtime.sh        # tag video2cfd-openfoam-runtime:latest
IMAGE=video2cfd-openfoam-runtime:latest bash cfd/scripts/run_openfoam_docker.sh runs/cfd/<case_name>
```

### Apptainer / Singularity (HPC)

```bash
bash cfd/runtime/pull_apptainer_image.sh        # writes ./worker_hpc_sct07.sif and its sha256
OUT=/path/to/worker_hpc_sct07.sif bash cfd/runtime/pull_apptainer_image.sh
```

Then, with `CONTAINER` pointing at the `.sif` file (default
`./worker_hpc_sct07.sif`):

```bash
export CONTAINER=/path/to/worker_hpc_sct07.sif
NPROCS=56 bash cfd/scripts/run_openfoam_apptainer.sh runs/cfd/<case_name>
sbatch -p <queue> -A <allocation> cfd/scripts/run_openfoam_case.slurm runs/cfd/<case_name>
sbatch -p <queue> -A <allocation> cfd/scripts/run_scalar_resume.slurm runs/cfd/<case_name>
```

The `.sif` file is not committed to git.

## Not yet checked

The wrapper scripts have not yet been run against the real image, and
`cfd/tests/` does not cover them. The two assumptions above (location of the
OpenFOAM environment file, solver on `PATH`) and MPI start-up inside the
container on a cluster are therefore still to be confirmed.
