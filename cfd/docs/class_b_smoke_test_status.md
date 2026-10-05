# Class B CFD smoke-test status

> **Note added 2026-10-04.** The record below describes a smoke test of the CFD
> workflow as it was first committed (commit `be684df`, 2026-10-01). The case
> generator, the configs, the run scripts and the probe-extraction script were
> rewritten after that test, and a re-implemented solver source was added. The
> smoke test has **not** been repeated with the current code, so this record
> says nothing about the current scripts. Two points to keep in mind when
> reading it:
>
> - The input path below is the one used at that time. The Class B geometry is
>   now read from `data/projects/classroom_v1/runs/run_005/4_stl/axis_aligned/`.
> - The current state of the workflow, and what has and has not been checked,
>   is described in `paper_consistency_checklist.md` ("Known limitations").

## Record of the smoke test (earlier version of the scripts)

The first committed version of this workflow (commit `be684df`) was smoke-tested
with the Class B reconstructed STL geometry.

Steps run locally in that test:

- STL handoff into `data/projects/classroom_b/runs/complex/4_stl/`
- measured-height scaling
- STL quality reporting
- conservative `trimesh` repair attempt
- OpenFOAM case generation
- `blockMesh`
- `surfaceFeatureExtract`
- `snappyHexMesh`
- `topoSet`
- `createPatch`
- `checkMesh`
- `simpleFoam` smoke run
- `scalarTransportFoamTurbulent` smoke run
- OpenFOAM probe output generation
- CSV extraction to `monitor_history.csv`
- ParaView executable discovery
- scripted ParaView rendering to `paraview_scalar_smoke.png`

Known limitations:

- The tested Class B furniture STL is not watertight even after conservative
  `trimesh` repair, although the generated OpenFOAM mesh passed `checkMesh`.
- The smoke-test mesh/run is not a claim of exact production-resolution
  reproduction.
- The source code of the `scalarTransportFoamTurbulent` binary used in that
  test is not available. (2026-10-04: `cfd/solvers/` now contains a
  re-implementation.)
- Auditorium and all remaining paper cases still need separate smoke tests.
