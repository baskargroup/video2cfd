# Running the downstream CFD workflow

This page describes how to go from the STL output of the geometry pipeline to an
OpenFOAM v2412 case, run it, and extract the monitor histories and clearance
times (Section 3.5 of the paper). The code is in [`../cfd/`](../cfd/README.md).
All commands are run from the repository root unless stated otherwise.

Before you start, read the "Known limitations" in
[`../cfd/docs/paper_consistency_checklist.md`](../cfd/docs/paper_consistency_checklist.md).
The most important ones: the case dictionaries are generated to match the
settings stated in the paper (they are not the archived production case
folders), the monitor locations in the configs are placeholders, and the
current scripts have not yet been run in OpenFOAM from this repository.

## 1. What you need

- Python with the packages of `requirements.txt` (the CFD scripts use numpy,
  trimesh, PyYAML, scipy and networkx).
- OpenFOAM v2412 (www.openfoam.com), either installed natively or through the
  runtime container image (Docker or Apptainer, see
  [`../cfd/runtime/README.md`](../cfd/runtime/README.md)).
- The solver `scalarTransportFoamTurbulent`: built from
  `cfd/solvers/scalarTransportFoamTurbulent/` (step 4) or taken from the
  container image, which contains a pre-built binary.
- The STL geometry (step 2). It is not stored in this repository.

## 2. Geometry hand-off

The CFD input is the **wall-aligned** output of step 5 of the geometry pipeline:

```text
data/projects/<project>/runs/<run_id>/4_stl/axis_aligned/
    room.stl                              room enclosure (required)
    furniture.stl                         automated furniture, before human QA
    furniture_with_mannequins.stl         the same with seated mannequins
    furniture_edited.stl                  furniture after the scene editor (step 4b)
    furniture_edited_with_mannequins.stl  the same with seated mannequins
    <category>/<category>_combined_edited.stl   one category after the editor
    furniture_individual_edited/          one STL per object after the editor
    furniture_and_room.stl                visualization only, not used
```

Which files exist depends on whether mannequins were placed and whether the
scene editor was used.

Do not use the files of the top-level `4_stl/` folder when an `axis_aligned/`
folder exists: they are in the reconstruction frame, in which the walls are not
parallel to the x and y axes, and the generator stops because the ventilation
patches do not lie on the ceiling. Step 5 writes `axis_aligned/` whenever the
room is rotated relative to the axes (`room.rotation_deg`, set in the geometry
config or in the scene editor). If it reports that the scene is already
axis-aligned, no such folder is written and `4_stl/` itself is the wall-aligned
frame; point `geometry.stl_source_dir` at it.

The six configs in `cfd/configs/` are the six CFD cases of the paper:

| Config | Paper case | `geometry.stl_source_dir` (under `data/projects/`) | `geometry.furniture_stl` |
|---|---|---|---|
| `class_a_v1_empty.yaml` | Mixed-furniture: room shell | `classroom_v2_v2/runs/run_010/4_stl/axis_aligned` | `null` (no furniture; only `room.stl`) |
| `class_a_v2_chairs.yaml` | Mixed-furniture: +32 chairs | same | `chair/chair_combined_edited.stl` |
| `class_a_v3_tables.yaml` | Mixed-furniture: +32 chairs + 16 tables | same | `[chair/chair_combined_edited.stl, table/table_combined_edited.stl]` |
| `class_a_v4_heavy.yaml` | Mixed-furniture: +32 chairs + 16 tables + 25 mannequins | same | `furniture_edited_with_mannequins.stl` |
| `class_b_complex.yaml` | Chair-dominant: 45 chairs + 2 tables + mannequins | `classroom_v1/runs/run_005/4_stl/axis_aligned` | `furniture_edited_with_mannequins.stl` |
| `auditorium.yaml` | Auditorium: 273 chairs + 2 tables | `audi_v1/runs/run_002/4_stl/axis_aligned` | `furniture_edited.stl` |

Notes:

- **Empty room.** `furniture_stl: null` generates a case with the room surface
  only; all furniture entries are left out of the meshing dictionaries.
- **What-if variants.** The generator meshes one furniture surface per case
  (one file, a list of files that is merged, or all `*.stl` of a folder given
  as `geometry.furniture_dir`). The four mixed-furniture cases are different
  selections from the same geometry run. A variant with a different layout or
  occupancy needs its own geometry run or saved editor state (a new `run_id`,
  then steps 4b and 5) and a config that points at that folder.
- **Paper geometry.** The final geometry of the paper went through the scene
  editor (see [`reproduce.md`](reproduce.md)), so it cannot be regenerated from
  the geometry configs alone; the reconstructed STL assets are available from
  the authors on request. The patch centres in the six configs are absolute
  coordinates in the wall-aligned frame of exactly these runs. For any other
  room or geometry run, set new patch centres (`ventilation.patches`) and
  monitors in a copy of a config.
- **Chair counts.** The case names above are those of the paper: the furniture
  surface used for the paper contains 45 chairs in the chair-dominant classroom
  and 273 chairs and 2 tables in the auditorium. The chair count of an edited
  scene depends on the manual corrections made in the scene editor (chairs can
  be added, removed or moved), so another editor state of the same run can have
  a different count; this applies to both rooms. Check the number of
  `chair_*.stl` files in `furniture_individual_edited/` of the run before
  generating a case. The mesh settings of `auditorium.yaml` are provisional
  (see the checklist).

## 3. Generate a case

```bash
# optional dry run: same checks as the generator, writes nothing
python cfd/scripts/00_validate_inputs.py --config cfd/configs/class_a_v2_chairs.yaml

python cfd/scripts/01_prepare_openfoam_case.py \
    --config cfd/configs/class_a_v2_chairs.yaml \
    --output runs/cfd/class_a_v2_chairs
```

The generator scales `room.stl` and the furniture uniformly so that the room
height equals `geometry.room_height_m`, checks the geometry, and writes:

```text
0/U  0/p  0/k  0/epsilon  0/nut  0/T
constant/transportProperties
constant/turbulenceProperties
constant/triSurface/room.stl
constant/triSurface/furniture.stl        (not for an empty room)
system/blockMeshDict
system/snappyHexMeshDict
system/surfaceFeatureExtractDict
system/topoSetDict
system/createPatchDict
system/decomposeParDict
system/controlDict                       (= controlDict.flow)
system/controlDict.flow
system/controlDict.scalar
system/fvSchemes
system/fvSolution
Allrun
case.foam
cfd_case_metadata.json  geometry_scaling.json  stl_quality_report.json
```

It prints the scale factor, the scaled room size, the background mesh, the
patch areas with their overlap with the ceiling, the supply flow rate and the
nominal air-change rate. The same numbers are in `cfd_case_metadata.json`.

Checks and options:

- The room STL must be watertight. A furniture STL that is not watertight only
  gives a warning (the seated-mannequin template is open); `--strict-watertight`
  makes it an error. `--no-repair-stl` switches off the conservative `trimesh`
  repair that is tried first.
- Every patch rectangle must lie at least 99% on the ceiling of the scaled room;
  `--allow-partial-patches` turns this error into a warning.
- Monitors must be inside the room.
- `--overwrite` replaces an existing case directory.

A failed run writes nothing and keeps an existing case.

## 4. The scalar solver

`scalarTransportFoamTurbulent` is not part of OpenFOAM. `Allrun` uses the
executable of that name on `PATH`.

Build it from the source in this repository, in an OpenFOAM v2412 development
environment (one that provides `wmake`):

```bash
source /usr/lib/openfoam/openfoam2412/etc/bashrc   # or your site's v2412 environment
cd cfd/solvers/scalarTransportFoamTurbulent
wmake
which scalarTransportFoamTurbulent                  # installed in $FOAM_USER_APPBIN
```

Or use the runtime container image (steps 5b and 5c), which contains a
pre-built binary of the same name.

The source in `cfd/solvers/` is a re-implementation based on the stock
`scalarTransportFoam`; the source of the binary used for the production
runs is not available. The re-implementation has not yet been compiled or
compared with the binary in the image. See
[`../cfd/solvers/scalarTransportFoamTurbulent/README.md`](../cfd/solvers/scalarTransportFoamTurbulent/README.md).

## 5. Run

Every generated case contains `Allrun`, which does all the work. It stops at
the first failing tool, writes one `log.<application>` per tool and one
`status/<nn>_<name>.done` marker per finished step, and skips finished steps
when it is started again.

| Stage | Steps |
|---|---|
| `mesh` | `blockMesh`; `surfaceFeatureExtract`; `snappyHexMesh -overwrite`; `topoSet`; `createPatch -overwrite`; `checkMesh`; check that the patches `inlet` and `outlet` exist and have faces |
| `flow` | `decomposePar -force` (parallel runs; scotch); `simpleFoam` to iteration 1000 |
| `scalar` | field transfer: the scalar field `T` of time 0 is copied into the last flow time directory; `scalarTransportFoamTurbulent` with `system/controlDict.scalar`; `reconstructPar -latestTime` (parallel runs) |

The scalar run uses a fixed time step of 0.05 s: 24,000 steps for the classroom
cases (end time 2200) and 80,000 steps for the auditorium (end time 5000).

### 5a. Native OpenFOAM

```bash
source /usr/lib/openfoam/openfoam2412/etc/bashrc   # or your site's v2412 environment
cd runs/cfd/class_a_v2_chairs

./Allrun                                # serial
NPROCS=56 ./Allrun                      # parallel: decomposePar + mpirun -np 56
MESH_ONLY=1 ./Allrun                    # stop after meshing and the patch check
STAGES="mesh flow" NPROCS=56 ./Allrun   # no scalar run
STAGES=scalar NPROCS=56 ./Allrun        # only the scalar stage (also resumes it)
./Allrun --help                         # all environment variables
```

If the executable bit is missing (for example when the case was generated on
Windows), call it as `bash ./Allrun`. `MPI_LAUNCHER` replaces the default
`mpirun -np $NPROCS`. `FORCE=1` runs the selected stages again; the scalar
solver still continues from the latest time directory, so to repeat the flow or
the scalar run of a serial case first remove the time directories later than
the flow end time (`Allrun` stops with this message before the flow solve; in a
parallel case `decomposePar -force` removes them).

### 5b. Docker

```bash
bash cfd/scripts/run_openfoam_docker.sh runs/cfd/class_a_v2_chairs            # serial
NPROCS=8 bash cfd/scripts/run_openfoam_docker.sh runs/cfd/class_a_v2_chairs   # parallel
MESH_ONLY=1 bash cfd/scripts/run_openfoam_docker.sh runs/cfd/class_a_v2_chairs
```

The script starts the published runtime image (referenced by digest; set
`IMAGE` to use another one), loads the OpenFOAM environment and calls
`./Allrun` in the case. No local image build is needed.

### 5c. Apptainer and SLURM

```bash
# once: pull the runtime image as a .sif file
bash cfd/runtime/pull_apptainer_image.sh          # writes ./worker_hpc_sct07.sif

# interactive, on one node
export CONTAINER=$PWD/worker_hpc_sct07.sif
NPROCS=56 bash cfd/scripts/run_openfoam_apptainer.sh runs/cfd/class_a_v2_chairs

# as a SLURM job: one node, 56 tasks, 48 h
sbatch -p <queue> -A <allocation> cfd/scripts/run_openfoam_case.slurm runs/cfd/class_a_v2_chairs

# if the job ran out of time during the scalar run, continue it
sbatch -p <queue> -A <allocation> cfd/scripts/run_scalar_resume.slurm runs/cfd/class_a_v2_chairs
```

The queue and the allocation in the job scripts are placeholders; give them on
the command line as shown or edit the `#SBATCH` lines. Load your site's
Apptainer module before `sbatch` (on TACC systems `module load tacc-apptainer`).
`NPROCS` defaults to the number of SLURM tasks. MPI is started inside the
container with the image's own `mpirun`, on a single node. The job log
`video2cfd_openfoam_<jobid>.out` is written to the directory you submit from.

## 6. Monitor histories and clearance times

The scalar run writes the monitors (OpenFOAM probes of `T`) at every time step to
`postProcessing/monitors/<startTime>/T` in the case.

```bash
python cfd/scripts/06_extract_probe_csv.py runs/cfd/class_a_v2_chairs --metrics
```

This writes `monitor_history.csv` into the case (`time` and one column per
monitor; a resumed run is joined automatically) and prints, for every monitor,
t50, t80 and t90 (the times at which C/C0 falls to 0.5, 0.2 and 0.1, by linear
interpolation between samples) and the area under the curve.

- As in the paper, time is measured from the start of the scalar run and C0 is
  the uniform initial concentration: t0 defaults to the start time of the
  scalar run (the `<startTime>` folder of the probe output; 1000, the end of
  the flow solve) and C0 to `ventilation.scalar_initial_value` of
  `cfd_case_metadata.json` (1). The first recorded sample is one time step
  after t0; the history is anchored at (t0, C0). `--t0` and `--c0` override
  the defaults (for example `--t0 1000.05 --c0 <first sample>` to measure from
  the first sample instead).
- `--metrics-output clearance_metrics.csv` also writes the metrics to a CSV in
  the case.

The monitor locations in the six configs are placeholders, so these numbers
are not expected to reproduce the clearance times reported in the paper.

## 7. Optional

```bash
# SHA-256 manifest of the dictionaries, logs, STLs and monitor output of a run
python cfd/scripts/05_make_run_manifest.py runs/cfd/class_a_v2_chairs \
    --image-digest sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c

# quick-look image of the scalar field (needs ParaView's pvpython or pvbatch)
bash cfd/scripts/07_render_paraview.sh runs/cfd/class_a_v2_chairs

# tests (no OpenFOAM needed): generator, probe extraction, Allrun with stand-in tools
python cfd/tests/test_prepare_case.py
python cfd/tests/test_extract_probe_csv.py
bash cfd/tests/test_allrun.sh
```

## Scalar model

The scalar is stored as `T` in OpenFOAM and is the dimensionless concentration
`C` of the paper. `constant/transportProperties` contains `DT = 1.5e-5 m2/s` and
`Sct = 0.7`; the solver uses the effective diffusivity `DT + nut/Sct`, with
`nut` taken from the frozen `simpleFoam` solution.

## Data policy

Generated cases, processor directories, solver output and container images are
not stored in this repository. Generate cases under `runs/` (ignored by git)
and archive full cases separately.
