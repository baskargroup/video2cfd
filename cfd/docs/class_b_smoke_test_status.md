# Class B CFD smoke-test status

This repository workflow has been smoke-tested with the Class B reconstructed STL
geometry.

Verified locally:

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
- The original source code for `scalarTransportFoamTurbulent` has not yet been
  recovered.
- Auditorium and all remaining paper cases still need separate smoke tests.
