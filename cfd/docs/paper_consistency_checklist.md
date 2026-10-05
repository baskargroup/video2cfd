# CFD settings: paper and this repository

This page compares every CFD setting stated in the paper (Section 3.5 "CFD
Integration and Simulation Setup", the implementation details of Section 4, the
table of final production cases and Appendix A) with what
`cfd/scripts/01_prepare_openfoam_case.py`, the six configs in `cfd/configs/`
and `cfd/templates/Allrun` produce. Tables of the paper are named by their
captions.

The column "this repository" was filled in by reading the generator and the
cases it writes. `cfd/tests/test_prepare_case.py` asserts most of these values
on generated cases. Read "Known limitations" at the end before relying on it.

## Mesh (Sec. 3.5.1 "Domain and Mesh")

| Setting | Paper | This repository |
|---|---|---|
| Meshing tools | `blockMesh` base mesh, refined and snapped with `snappyHexMesh` | `system/blockMeshDict` (one uniform hex block around the scaled room, 0.01 m padding) and `system/snappyHexMeshDict` (castellated mesh and snapping) |
| Background cell size | 0.08 m before local refinement | `mesh.background_cell_size_m: 0.08`; cells per axis = round(extent / 0.08), e.g. 69 x 87 x 37 cells of 0.0800 x 0.0802 x 0.0789 m for the mixed-furniture classroom |
| Feature edges | extracted with `surfaceFeatureExtract` | `system/surfaceFeatureExtractDict` for `room.stl` and, if present, `furniture.stl`; used as explicit features in snappyHexMesh |
| Refinement near furniture and mannequins | cells within 0.02 m refined to Level 4 (5 mm) | `refinementRegions { furniture { mode distance; levels ((0.02 4) (0.05 3)); } }`; Level 4 of a 0.08 m cell is 5 mm |
| Refinement below the supply diffusers | `searchableBox` region directly beneath the ceiling diffusers, Level 3 (1 cm), extending down to z = 0.5 m | one `searchableBox` per inlet patch, `mode inside`, level 3 (1 cm), from 0.5 m above the lowest floor point to the ceiling. The horizontal extent is not given in the paper; the footprint of the inlet patch is used |
| Near-wall treatment | wall functions, mesh not wall-resolved | `addLayers false`; wall functions in `0/k`, `0/epsilon`, `0/nut` |
| Surface refinement levels, second distance shell (0.05 m to Level 3), cell caps | not stated | room surface level (2 2), furniture surface level (3 4), `nCellsBetweenLevels 3`; `maxGlobalCells` 20 million (classrooms), 40 million (auditorium). These are the workflow's values |

## Patches and boundary conditions (Sec. 3.5.2 "Boundary Conditions"; tables "Boundary patch definitions" and "Boundary condition summary")

| Setting | Paper | This repository |
|---|---|---|
| How patches are made | rectangular ceiling patches, face sets from `topoSet`, patches from `createPatch` | `system/topoSetDict`: one `boxToFace` per rectangle, then a `boundaryToFace` subset so that only boundary faces remain; `system/createPatchDict`: patches `inlet` and `outlet` from the two face sets |
| Number of patches | four supply diffusers (classrooms), six (auditorium); two exhaust vents at opposing corners | classrooms: 4 inlets of 0.4 x 0.4 m, 2 outlets of 0.6 x 0.6 m; auditorium: 6 inlets of 0.8 x 0.8 m, 2 outlets of 1.49 x 1.49 m. Sizes and positions are not given in the paper; see "Known limitations" |
| `U` | inlets `fixedValue (0 0 -0.5)`; outlets `zeroGradient`; walls and furniture `noSlip` | same (`0/U`) |
| `p` | inlets `zeroGradient`; outlets `fixedValue 0`; walls and furniture `zeroGradient` | same (`0/p`) |
| `k` | inlets `fixedValue 0.015`; outlets `zeroGradient`; walls and furniture `kqRWallFunction 0.015` | same (`0/k`) |
| `epsilon` | inlets `fixedValue 0.004`; outlets `zeroGradient`; walls and furniture `epsilonWallFunction 0.004` | same (`0/epsilon`) |
| Scalar `C` (field `T`) | internal field `uniform 1`; inlets `fixedValue 0`; outlets, walls and furniture `zeroGradient` | same (`0/T`) |
| `nut` | not stated | `nutkWallFunction` on walls and furniture, `calculated` on inlets and outlets (`0/nut`) |

## Models (Sec. 3.5.3 "Governing Equations and Solver Configuration"; table "Flow and passive-scalar transport models")

| Setting | Paper | This repository |
|---|---|---|
| Flow model and solver | incompressible steady RANS, `simpleFoam` | `application simpleFoam` in `system/controlDict.flow` |
| Turbulence closure | standard k-epsilon with wall functions, standard coefficients | `RASModel kEpsilon` with the OpenFOAM default coefficients (`constant/turbulenceProperties`) |
| Kinematic viscosity | 1.5e-5 m2/s | `nu 1.5e-05` (`constant/transportProperties`) |
| Molecular scalar diffusivity D | 1.5e-5 m2/s | `DT 1.5e-05` |
| Turbulent Schmidt number | 0.7 | `Sct 0.7` |
| Effective scalar diffusivity | D + nu_t / Sc_t | `Deff = DT + nut/Sct` in `cfd/solvers/scalarTransportFoamTurbulent/createFields.H`, used in `fvm::laplacian(Deff, T)` |
| Scalar solver | custom `scalarTransportFoamTurbulent` | `application scalarTransportFoamTurbulent` in `system/controlDict.scalar`; source in `cfd/solvers/` (a re-implementation, see "Known limitations") |
| OpenFOAM version | v2412 | `simulation.openfoam_version: "2412"`; `Allrun` warns for another version; the container scripts load the v2412 environment of the runtime image |

## Numerics (table "Numerical schemes and solver settings"; Sec. 3.5.3)

| Setting | Paper | This repository |
|---|---|---|
| Time discretisation | Euler | `ddtSchemes { default Euler; }` (one `system/fvSchemes` serves both the flow and the scalar stage) |
| Gradients | Gauss linear | `gradSchemes { default Gauss linear; }` |
| Scalar convection | Gauss upwind | `div(phi,T) Gauss upwind` |
| Laplacian | Gauss linear corrected | `laplacianSchemes { default Gauss linear corrected; }`, `snGradSchemes { default corrected; }` |
| `p` solver | PCG + DIC, tolerance 1e-6, relTol 0.01 | same (`system/fvSolution`) |
| `U`, `k`, `epsilon` solver | PBiCGStab + DILU, tolerance 1e-7, relTol 0.1 | same |
| `C` solver | PBiCGStab + DILU, tolerance 1e-8, relTol 0 | same, separate entry for `T` |
| SIMPLE | `nNonOrthogonalCorrectors 0`, `consistent yes` | same |
| Pressure under-relaxation | unity (SIMPLEC) | `relaxationFactors { fields { p 1.0; } }` |
| Relaxation factors | `U`, `k`, `epsilon`: 0.7; `C`: 1.0 | `equations { U 0.7; k 0.7; epsilon 0.7; T 1.0; }` |
| Stopping rule of the flow solve | fixed budget of 1000 `simpleFoam` iterations, no residual threshold | `endTime 1000`, `deltaT 1`, no `residualControl` block; `Allrun` stops if the last flow time is not 1000 |
| Convection of `U`, `k`, `epsilon` | not stated | `Gauss upwind` (the workflow's value) |
| Scalar time step | not stated | 0.05 s, fixed (`simulation.scalar_delta_t`; the workflow's value) |

## Execution (workflow figure; Sec. 4 "Implementation Details"; table "Final production CFD cases")

| Setting | Paper | This repository |
|---|---|---|
| Sequence | geometry preparation, dictionary generation, `blockMesh`, `snappyHexMesh`, `topoSet`/`createPatch`, `decomposePar`, `simpleFoam`, field transfer, `scalarTransportFoamTurbulent`, `reconstructPar`, CSV extraction, ParaView | `01_prepare_openfoam_case.py`, then `Allrun`: `blockMesh`, `surfaceFeatureExtract`, `snappyHexMesh`, `topoSet`, `createPatch`, `checkMesh`, patch check, `decomposePar`, `simpleFoam`, field transfer, `scalarTransportFoamTurbulent`, `reconstructPar`; then `06_extract_probe_csv.py` and the optional `07_render_paraview.sh` |
| Field transfer | a step between the flow and the scalar solve | `Allrun` copies `T` of time 0 into the last flow time directory (per processor directory in parallel; a later time directory without `T`, the incomplete write of an interrupted scalar run, stops the script) and installs `system/controlDict.scalar` (`startFrom latestTime`; `libs (turbulenceModels)` so that the wall-function patch types of the frozen `nut` field can be read whatever the solver binary was linked with) |
| Decomposition | `scotch`, 56 cores, one node | `method scotch; numberOfSubdomains 56;` (`Allrun` sets the number to `NPROCS`); the SLURM scripts request one node and 56 tasks |
| Airflow endpoint | iteration 1000 in all six cases | `simulation.airflow_end_iter: 1000` |
| Scalar window | about 1200 s (classrooms), 3999.8 s (auditorium), counted from time 1000 | `simulation.scalar_end_time: 2200` (classrooms) and `5000` (auditorium) |

## Monitors and clearance metrics (Sec. 3.5.4 "Clearance Metrics")

| Setting | Paper | This repository |
|---|---|---|
| Monitors | one point monitor in the occupied zone per classroom; three in the auditorium (front, middle, back rows); no coordinates given | `probes` function object `monitors` for `T` in `system/controlDict.scalar`, written every time step. One monitor per classroom config, three (`front`, `middle`, `back`) in the auditorium config. **The positions are placeholders** |
| t50, t80, t90 | times at which C/C0 falls to 0.5, 0.2, 0.1, linear interpolation between samples, measured from the start of the decay window; C0 is the uniform initial concentration | `06_extract_probe_csv.py --metrics`: t0 defaults to the start time of the scalar run (the `<startTime>` folder of the probe output, 1000) and C0 to `ventilation.scalar_initial_value` of the case (1); `--t0` and `--c0` override them |
| AUC | integral of C/C0 over the decay window | same script (trapezoidal rule over the recorded samples, anchored at (t0, C0)) |

## Geometry scale (Appendix A "Room dimensions and metric scale assignment")

| Setting | Paper | This repository |
|---|---|---|
| Room height | modelled enclosure height 2.90 m for both classrooms (physical reference 2.87 m) and 12.87 m for the auditorium (reference 12.88 m) | `geometry.room_height_m: 2.90` and `12.87`; room and furniture are scaled uniformly about the origin so that `room.stl` has this height |

## Known limitations

- **Generated, not archived.** The dictionaries are generated by this
  repository's script and reproduce the settings stated in the paper. They are
  not copies of the archived production case folders.
- **Monitor locations are placeholders.** The production monitor coordinates
  are not recorded in this repository. The configs use the centre of the room
  footprint at 1.1 m above the floor (classrooms) and three points on the centre
  line of the room at 2.0 m above the local floor (auditorium). Clearance times
  obtained with them are not expected to reproduce the values in the paper.
- **Vent patches.** The patch centres are given for a room height of 3.00 m
  (classrooms) and 12.85 m (auditorium)
  (`ventilation.patch_reference_height_m`). They are rescaled to the modelled
  height (`room_height_m / patch_reference_height_m`); the patch sizes are
  kept in metres, so the supply flow rate is 0.32 m3/s in the classrooms and
  1.92 m3/s in the auditorium for any room height. The scale-sensitivity
  estimate in the paper's discussion of the CFD limitations assumes supply
  patches that scale with the room; to follow that convention when changing
  `room_height_m`, scale `size_xy` by the same factor.
- **Room footprint.** The generator applies one uniform scale factor, so the
  in-plane room size follows from the room height: 5.50 x 6.96 m
  (mixed-furniture classroom) and 5.82 x 6.06 m (chair-dominant classroom) at
  2.90 m, and 30.04 x 29.00 m for the auditorium at 12.87 m. The configs use
  the modelled heights stated in the paper. The paper's room-dimension table
  lists the in-plane dimensions of the final production enclosures (5.71 x
  7.22, 6.04 x 6.29, 30.01 x 28.98 m), which were regularized during CFD
  preparation (Appendix A of the paper); the generated rooms differ from them
  by about 4% in the classrooms and by about 0.1% in the auditorium.
- **Scalar solver.** The source in `cfd/solvers/scalarTransportFoamTurbulent/`
  is a re-implementation written from the stock OpenFOAM v2412
  `scalarTransportFoam`; the source of the original solver binary is not
  available. The re-implementation has not yet been compiled or compared with
  the binary in the runtime image.
- **Values that are not in the paper.** The convection schemes for `U`, `k` and
  `epsilon` (`Gauss upwind`) and the scalar time step (0.05 s) are the
  workflow's values. The same holds for the surface refinement levels, the
  second distance shell around the furniture, the horizontal extent of the
  diffuser refinement boxes, and the patch sizes and positions.
- **Auditorium mesh size.** With 0.08 m background cells and this workflow's
  refinement settings, the auditorium background mesh alone has about 22
  million cells (about 18 million inside the fluid; the tiered floor removes
  the rest), and the generator's rough estimate for the refined mesh is about
  170 million, far above `max_global_cells` (40 million), at which
  snappyHexMesh stops refining; the generator prints a warning. The paper
  reports 27.85 million cells for the production auditorium case. The
  refinement settings of that case that the paper does not list (surface
  levels, cell caps) are not recorded in this repository, so this workflow's
  settings are not expected to reproduce that cell count.
  `cfd/configs/auditorium.yaml` contains a commented alternative for a
  smaller mesh, `background_cells: [120, 116, 52]` (0.25 m cells, at which
  Level 4 is 15.6 mm and Level 3 is 31 mm). The auditorium mesh settings in
  this repository are provisional.
- **How this was checked.** The workflow has been checked by generating cases
  from the reconstructed geometry of the three rooms, by the tests in
  `cfd/tests/` (generator, probe extraction, and `Allrun` against stand-in
  tools in `test_allrun.sh`), and by code review. The current scripts have
  not yet been run in OpenFOAM from this repository, neither as a smoke test
  nor at production resolution. `class_b_smoke_test_status.md` records a
  smoke test of an earlier version of the scripts.
