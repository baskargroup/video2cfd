# Downstream OpenFOAM CFD workflow

This directory generates and runs the OpenFOAM cases described in Section 3.5 of
the paper ("CFD Integration and Simulation Setup") from the STL geometry of the
geometry pipeline: steady airflow with `simpleFoam`, then a passive-scalar decay
run with `scalarTransportFoamTurbulent` on the frozen flow field, recorded at
point monitors.

Status in one paragraph: the OpenFOAM dictionaries are written by
`scripts/01_prepare_openfoam_case.py` so that they reproduce the settings stated
in the paper. They are not copies of the archived production case folders. The
current scripts have been checked by generating cases and by code review; they
have not yet been run in OpenFOAM from this repository, neither as a smoke test
nor at production resolution.
[`docs/paper_consistency_checklist.md`](docs/paper_consistency_checklist.md)
lists every setting and the known limitations.

Step-by-step instructions are in [`../docs/cfd_reproduce.md`](../docs/cfd_reproduce.md).
All commands below are run from the repository root.

## Contents

Paths in this table are relative to `cfd/`.

| Path | Purpose |
|---|---|
| `configs/*.yaml` | one config per CFD case of the paper |
| `scripts/00_validate_inputs.py` | dry run of the generator's checks; writes nothing |
| `scripts/01_prepare_openfoam_case.py` | case generator (STLs + config -> OpenFOAM case) |
| `scripts/02_check_stl_watertight.py` | optional stand-alone STL watertightness report |
| `templates/Allrun` | run script; the generator copies it into every case |
| `scripts/run_openfoam_docker.sh` | runs `./Allrun` of a case inside the Docker image |
| `scripts/run_openfoam_apptainer.sh` | runs `./Allrun` of a case inside the Apptainer image |
| `scripts/run_openfoam_case.slurm` | SLURM job: full workflow on one node (Apptainer) |
| `scripts/run_scalar_resume.slurm` | SLURM job: continue only the scalar run |
| `scripts/05_make_run_manifest.py` | SHA-256 manifest of the files that define a run |
| `scripts/06_extract_probe_csv.py` | monitor histories -> CSV; t50, t80, t90, AUC |
| `scripts/07_*`, `paraview/` | optional ParaView quick-look image |
| `solvers/scalarTransportFoamTurbulent/` | source of the scalar solver (GPL-3.0-or-later) |
| `runtime/` | container image reference ([`runtime/README.md`](runtime/README.md)) |
| `tests/` | tests of the generator, of the probe extraction and of `Allrun` (with stand-in tools); no OpenFOAM needed |
| `docs/`, `evidence/` | consistency checklist; earlier smoke-test and log-trace notes |

## Paper cases

| Config | Paper case (table "Final production CFD cases") | Geometry run | Furniture surface |
|---|---|---|---|
| `class_a_v1_empty.yaml` | Mixed-furniture: room shell | `classroom_v2_v2/runs/run_010` | none (`furniture_stl: null`) |
| `class_a_v2_chairs.yaml` | Mixed-furniture: +32 chairs | `classroom_v2_v2/runs/run_010` | `chair/chair_combined_edited.stl` |
| `class_a_v3_tables.yaml` | Mixed-furniture: +32 chairs + 16 tables | `classroom_v2_v2/runs/run_010` | `chair/chair_combined_edited.stl` and `table/table_combined_edited.stl` |
| `class_a_v4_heavy.yaml` | Mixed-furniture: +32 chairs + 16 tables + 25 mannequins | `classroom_v2_v2/runs/run_010` | `furniture_edited_with_mannequins.stl` |
| `class_b_complex.yaml` | Chair-dominant: 45 chairs + 2 tables + mannequins | `classroom_v1/runs/run_005` | `furniture_edited_with_mannequins.stl` |
| `auditorium.yaml` | Auditorium: 273 chairs + 2 tables | `audi_v1/runs/run_002` | `furniture_edited.stl` (mesh settings provisional, see the checklist) |

The geometry runs are those of `configs/classroom_a.yaml`, `configs/classroom_b.yaml`
and `configs/auditorium.yaml` (project names in `data/README.md`). All file names
are relative to `data/projects/<project>/runs/<run_id>/4_stl/axis_aligned/`.
The chair counts in the table are those of the paper (45 chairs in the
chair-dominant classroom; 273 chairs and 2 tables in the auditorium). The chair
count of an edited scene depends on the manual corrections made in the scene
editor, so another editor state of the same run can have a different count in
either room (count the `chair_*.stl` files in `furniture_individual_edited/`).

## Geometry hand-off

- **Use `4_stl/axis_aligned/`.** Step 5 of the geometry pipeline writes a
  wall-aligned copy of every STL there when the room is rotated relative to the
  coordinate axes. The patch coordinates in the configs are only valid in that
  frame; with the rotated files of the top-level `4_stl/` folder the generator
  stops because the patches are not on the ceiling. (If step 5 reports that the
  scene is already axis-aligned, it writes no `axis_aligned/` folder and
  `4_stl/` itself is the wall-aligned frame; point `geometry.stl_source_dir`
  at it.)
- **Files.** `room.stl` is the room enclosure. Furniture files:
  `furniture.stl` (automated output), `furniture_with_mannequins.stl`,
  `furniture_edited.stl` and `furniture_edited_with_mannequins.stl` (after the
  scene editor, step 4b), the per-category files
  `<category>/<category>_combined_edited.stl`, and per-object STLs in
  `furniture_individual_edited/`. `furniture_and_room.stl` is for visualization
  and is not used.
- **One furniture surface per case.** `geometry.furniture_stl` is a file name, a
  list of file names (merged), or `null` for an empty room;
  `geometry.furniture_dir` merges all `*.stl` of a folder instead.
- **What-if variants.** Each variant needs its own furniture selection. The four
  mixed-furniture cases select different files of the same run. A variant that
  changes the layout needs its own geometry run or saved editor state (a new
  `run_id`, then steps 4b and 5), and a config that points at that folder.
- **Scale.** The STLs are in the scale-normalised reconstruction frame. The
  generator scales room and furniture uniformly about the origin so that the
  room height equals `geometry.room_height_m`.
- **The STL data are not in this repository.** The reconstructed STL assets are
  available from the authors on request (see `docs/cfd_reproduce.md`). The six
  configs are tied to the final geometry of the paper: their patch centres are
  absolute coordinates in the wall-aligned frame of the runs listed above. For
  another room or another geometry run, set new patch centres.

## Quick start

```bash
# 1. Check the config and the STL hand-off (writes nothing)
python cfd/scripts/00_validate_inputs.py --config cfd/configs/class_a_v2_chairs.yaml

# 2. Generate the case
python cfd/scripts/01_prepare_openfoam_case.py \
    --config cfd/configs/class_a_v2_chairs.yaml \
    --output runs/cfd/class_a_v2_chairs

# 3a. Run in a native OpenFOAM v2412 environment
#     (scalarTransportFoamTurbulent must be on PATH, see "Scalar solver")
cd runs/cfd/class_a_v2_chairs
NPROCS=8 ./Allrun            # ./Allrun alone runs in serial; ./Allrun --help
cd -

# 3b. ... or in the Docker image
NPROCS=8 bash cfd/scripts/run_openfoam_docker.sh runs/cfd/class_a_v2_chairs

# 3c. ... or with Apptainer, directly or as a SLURM job
bash cfd/runtime/pull_apptainer_image.sh                  # writes ./worker_hpc_sct07.sif
NPROCS=56 bash cfd/scripts/run_openfoam_apptainer.sh runs/cfd/class_a_v2_chairs
sbatch -p <queue> -A <allocation> cfd/scripts/run_openfoam_case.slurm runs/cfd/class_a_v2_chairs

# 4. Monitor histories and clearance times (t0 = start of the scalar run, C0 = initial
#    scalar value of the case, as in the paper; --t0 / --c0 override)
python cfd/scripts/06_extract_probe_csv.py runs/cfd/class_a_v2_chairs --metrics
```

## Config keys

| Section | Keys |
|---|---|
| top level | `case_name`, `description`, `paper_case` |
| `geometry` | `stl_source_dir` (relative paths are tried from the current directory, the repository root, then the config's folder), `room_stl`, `furniture_stl` or `furniture_dir`, `room_height_m`, `scale_to_room_height` |
| `ventilation` | `inlet_velocity`, `scalar_initial_value`, `scalar_inlet_value`, `ceiling_selection_half_thickness_m`, `patch_reference_height_m`, `patches` (`name`, `role` inlet/outlet, `shape: rectangle`, `center_xy`, `size_xy`) |
| `monitors` | list of `name` plus `position_rel: [fx, fy]` with `height_above_floor_m`, or `position_rel: [fx, fy, fz]`, or `position_xyz` (metres in the scaled case) |
| `transport` | `scalar_field_name` (must be `T`), `molecular_diffusivity_m2_s`, `turbulent_schmidt_number` (optional `kinematic_viscosity_m2_s`, default 1.5e-5) |
| `turbulence` | `k`, `epsilon` (inlet and initial values); optional `model` (default `kEpsilon`) |
| `mesh` | `background_cell_size_m` (or explicit `background_cells`), `background_padding_m`, `max_local_cells`, `max_global_cells`, `room_refinement_level`, `furniture_refinement_level`, `furniture_distance_refinement`, `diffuser_refinement`; optional `location_in_mesh` (overrides the generator's choice of the snappyHexMesh seed point) |
| `simulation` | `openfoam_version`, `airflow_solver`, `scalar_solver`, `airflow_end_iter`, `scalar_end_time`, `scalar_delta_t`, `write_interval`, `scalar_write_interval`, `monitor_write_interval_steps`, `default_tasks`, `decomposition_method`; `decomposition_method` values are `scotch`, `simple` or `hierarchical`; optional `delta_t` (flow pseudo-time step, default 1) and `decomposition: [nx, ny, nz]` (required for `simple` and `hierarchical`; the product must equal `default_tasks`, and the case must then be run with `NPROCS` equal to `default_tasks`) |
| `runtime` | image names, for the record only; the run scripts use the environment variables `IMAGE` and `CONTAINER` |

Two points that are easy to miss:

- **Patches.** `center_xy` are absolute coordinates defined for a room of height
  `patch_reference_height_m`. Because the STLs are scaled about the origin, the
  centres are multiplied by `room_height_m / patch_reference_height_m`; the
  sizes stay in metres.
- **Monitors.** The positions in the six configs are placeholders (centre of the
  room footprint in the classrooms; three points on the centre line of the
  auditorium). The production monitor coordinates are not recorded in this
  repository, so clearance times from these configs are not expected to
  reproduce the values in the paper.
- **Solver names.** `simulation.airflow_solver` and `simulation.scalar_solver`
  only set the `application` entry of `controlDict.flow` and
  `controlDict.scalar`. `Allrun` runs `simpleFoam` and
  `scalarTransportFoamTurbulent` unless the environment variables
  `FLOW_SOLVER` / `SCALAR_SOLVER` are set.

## What the generator checks

All checks run before anything is written, and the case is written to a
temporary directory first, so a failed run leaves no half-written case.

- The room STL must be watertight (a conservative `trimesh` repair is tried
  first; `--no-repair-stl` disables it).
- A furniture STL that is not watertight gives a warning and a per-body report in
  `stl_quality_report.json`. The seated-mannequin template of the geometry
  pipeline is open, so the two mannequin cases warn. `--strict-watertight`
  turns the warning into an error.
- Every patch rectangle must lie at least 99% on the ceiling of the scaled room
  (`--allow-partial-patches` turns the error into a warning).
- Every monitor must be inside the room; a monitor inside a furniture body
  gives a warning.
- A rough cell-count estimate above `mesh.max_global_cells` gives a warning.

Other options: `--overwrite` replaces an existing case directory.

## Generated case

```text
0/{U,p,k,epsilon,nut,T}
constant/{transportProperties,turbulenceProperties}
constant/triSurface/room.stl  [furniture.stl]
system/{blockMeshDict,snappyHexMeshDict,surfaceFeatureExtractDict,topoSetDict,createPatchDict}
system/{decomposeParDict,controlDict,controlDict.flow,controlDict.scalar,fvSchemes,fvSolution}
Allrun  case.foam
cfd_case_metadata.json  geometry_scaling.json  stl_quality_report.json
```

`cfd_case_metadata.json` records the scale factor, the scaled room size, the
patch boxes, the supply flow rate and nominal air-change rate, the background
mesh, the monitor coordinates and all warnings.

## Running: `Allrun`

`Allrun` runs inside an OpenFOAM v2412 environment, from the case directory. It
stops at the first failing tool and can be started again; finished stages are
skipped.

| Stage | Steps (marker `status/<nn>_<name>.done`, log `log.<application>`) |
|---|---|
| `mesh` | `blockMesh`, `surfaceFeatureExtract`, `snappyHexMesh -overwrite`, `topoSet`, `createPatch -overwrite`, `checkMesh`, check that the patches `inlet` and `outlet` exist and are not empty |
| `flow` | `decomposePar -force` (if `NPROCS` > 1), `simpleFoam` |
| `scalar` | field transfer (`0/T` is copied into the last flow time directory; a later time directory without `T`, the incomplete write of an interrupted scalar run, stops the script), `scalarTransportFoamTurbulent` with `system/controlDict.scalar`, `reconstructPar -latestTime` (if `NPROCS` > 1) |

Environment variables: `NPROCS` (default 1), `MPI_LAUNCHER` (default
`mpirun -np $NPROCS`), `MESH_ONLY=1`, `STAGES` (any of `mesh flow scalar`),
`FORCE=1`, `VERBOSE=1`. `./Allrun --help` lists all of them. The Docker,
Apptainer and SLURM scripts only enter the container, load the OpenFOAM
environment and call `./Allrun`; they pass these variables through.

## Scalar solver

`scalarTransportFoamTurbulent` solves the scalar equation of the paper with the
effective diffusivity `D + nu_t/Sc_t` on the frozen flow field. Two ways to get it:

- **Container image.** The runtime image contains a pre-built binary
  (see [`runtime/README.md`](runtime/README.md)).
- **From source.** In an OpenFOAM v2412 development environment:
  `cd cfd/solvers/scalarTransportFoamTurbulent && wmake`. The source is a
  re-implementation based on the stock `scalarTransportFoam`; the source of
  the original solver binary is not available. The re-implementation has not
  yet been compiled or compared with the binary in the image; read the README
  in that folder first.

## Tests

```bash
python cfd/tests/test_prepare_case.py       # case generator
python cfd/tests/test_extract_probe_csv.py  # probe extraction and clearance metrics
bash   cfd/tests/test_allrun.sh             # Allrun, with stand-in OpenFOAM tools
```

The generator tests build a small synthetic room, run the generator and check
the generated dictionaries against the settings stated in the paper. The probe
test writes fake probe files and checks the CSV and the t50/t80/t90 defaults.
The `Allrun` test runs the script against stand-in tools that only create the
files `Allrun` inspects (serial and parallel path, field transfer, interrupted
scalar run, failing tool). None of them runs OpenFOAM.

## Git policy

Do not commit `.sif` images, STL datasets, generated cases, OpenFOAM
`processor*/` directories, `postProcessing/` folders or archives. Generate cases
under `runs/`, which is ignored.
