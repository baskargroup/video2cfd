# Downstream OpenFOAM CFD workflow

This directory contains the downstream CFD workflow for running OpenFOAM airflow
and passive-scalar simulations from STL geometry produced by the `video2cfd`
geometry pipeline.

This is self-contained code. It does **not** require the historical production zip
archive. A user supplies STL files and a YAML config; the scripts generate the
OpenFOAM case dictionaries and run the solver chain.

## Input handoff

The geometry pipeline writes STL outputs under a run directory such as:

```text
data/projects/<project_name>/runs/<run_id>/4_stl/
```

The CFD preparation script expects at least:

```text
room.stl
furniture.stl
```

Optional:

```text
furniture_and_room.stl
furniture_individual_edited/
```

## Included paper-case configs

```text
cfd/configs/class_a_v1_empty.yaml
cfd/configs/class_a_v2_chairs.yaml
cfd/configs/class_a_v3_tables.yaml
cfd/configs/class_a_v4_heavy.yaml
cfd/configs/class_b_complex.yaml
cfd/configs/auditorium.yaml
```

Each config contains the measured height, ventilation patch coordinates,
transport coefficients, turbulence initialization, mesh settings, and solver
settings for that case.

## Prepare a case

```bash
python cfd/scripts/00_validate_inputs.py --config cfd/configs/class_a_v4_heavy.yaml

python cfd/scripts/01_prepare_openfoam_case.py \
  --config cfd/configs/class_a_v4_heavy.yaml \
  --output runs/cfd/class_a_v4_heavy
```

## Run locally with Docker

```bash
bash cfd/scripts/run_openfoam_docker.sh runs/cfd/class_a_v4_heavy
```

## Run on HPC with Apptainer and SLURM

```bash
export CONTAINER=/path/to/worker_hpc_sct07.sif
sbatch -A <allocation> cfd/scripts/run_openfoam_case.slurm runs/cfd/class_a_v4_heavy
```

On TACC/Frontera the script defaults to `ibrun` for parallel launch. Override it
when needed:

```bash
export MPI_LAUNCHER="mpirun -np 56"
```

## Production solver sequence

The compact production log trace supports this OpenFOAM sequence:

```bash
blockMesh
surfaceFeatureExtract
snappyHexMesh -overwrite
topoSet
createPatch -overwrite
decomposePar -force
simpleFoam -parallel
scalarTransportFoamTurbulent -parallel
```

Production logs indicate OpenFOAM 2412. Historical production runs invoked the
custom scalar solver as:

```bash
/app/bin/scalarTransportFoamTurbulent -parallel
```

The reproducibility container exposes the same solver as:

```bash
scalarTransportFoamTurbulent
```


## STL watertightness gate

Before writing STL files into an OpenFOAM case, `01_prepare_openfoam_case.py`
loads `room.stl`, `furniture.stl`, and optional `furniture_and_room.stl` with
`trimesh`, applies conservative repair operations, and fails by default if any
required STL is still not watertight. The script writes:

```text
stl_quality_report.json
geometry_scaling.json
```

Use `--allow-non-watertight` only for debugging; it is not the recommended
reproducibility path.

## Git policy

Do not commit `.sif` images, large STL datasets, OpenFOAM `processor*/`
directories, transient fields, large `postProcessing/` folders, or archive files.
