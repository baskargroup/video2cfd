# Reproducing the downstream CFD workflow

The `video2cfd` reconstruction pipeline produces STL geometry. The downstream
CFD workflow in `cfd/` consumes `room.stl` and `furniture.stl`, scales them to a
measured room height if requested, generates OpenFOAM dictionaries, and runs
steady airflow followed by transient passive-scalar transport.

## Required inputs

At minimum, the CFD preparation step expects:

```text
room.stl
furniture.stl
```

The path is configured in the case YAML under `geometry.stl_source_dir`.

## Case generation

```bash
python cfd/scripts/01_prepare_openfoam_case.py \
  --config cfd/configs/class_a_v4_heavy.yaml \
  --output runs/cfd/class_a_v4_heavy
```

The generated OpenFOAM case includes:

```text
0/U
0/p
0/k
0/epsilon
0/nut
0/T
constant/transportProperties
constant/turbulenceProperties
constant/triSurface/room.stl
constant/triSurface/furniture.stl
system/blockMeshDict
system/surfaceFeatureExtractDict
system/snappyHexMeshDict
system/topoSetDict
system/createPatchDict
system/decomposeParDict
system/controlDict
system/controlDict.scalar
system/fvSchemes
system/fvSolution
```

## Solver sequence

The production OpenFOAM logs support the following sequence:

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

The steady airflow solve uses `simpleFoam`. The transient scalar solve uses the
custom turbulent scalar solver `scalarTransportFoamTurbulent`.

## Scalar model

The scalar is stored as `T` in OpenFOAM and interpreted as passive scalar
concentration. The transport dictionary written by the generator includes:

```text
DT  = 1.5e-5 m^2/s
Sct = 0.7
```

The intended turbulent effective scalar diffusivity is:

```text
D_eff = DT + nut/Sct
```

## Runtime

The reproducibility Docker image is:

```text
jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07
```

For HPC use, build/pull an Apptainer image:

```bash
apptainer pull worker_hpc_sct07.sif docker://jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07
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

## Data policy

Large CFD outputs are not stored in this git repository. Full production cases,
decomposed processor fields, transient outputs, and container images should be
archived separately with checksums.
