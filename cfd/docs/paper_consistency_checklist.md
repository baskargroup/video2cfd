# CFD paper-consistency checklist

This CFD workflow is intended to reproduce the downstream OpenFOAM workflow described in Section 3.5 and Figure 14 of the paper.

## Required stages from the paper

- [ ] Raw input: STLs and scene.json
- [ ] Sanitization and Python trimesh welding
- [ ] Bounding box and domain initialization
- [ ] OpenFOAM dictionary generation
- [ ] blockMesh
- [ ] surfaceFeatureExtract
- [ ] snappyHexMesh
- [ ] topoSet/createPatch
- [ ] decomposePar using 56-core scotch decomposition
- [ ] simpleFoam steady RANS airflow
- [ ] scalarTransportFoamTurbulent passive-scalar transport
- [ ] reconstructPar
- [ ] Python CSV extraction
- [ ] ParaView rendering/export

## Required model settings

- OpenFOAM version: 2412 for production reproducibility
- Airflow solver: simpleFoam
- Flow model: incompressible steady RANS
- Turbulence closure: standard k-epsilon with wall functions
- Inlet velocity: fixedValue uniform (0 0 -0.5)
- Outlet velocity: zeroGradient
- Outlet pressure: fixedValue uniform 0
- Inlet k: fixedValue uniform 0.015
- Inlet epsilon: fixedValue uniform 0.004
- Scalar field name in OpenFOAM: T
- Manuscript scalar notation: C
- Scalar initial field: uniform 1
- Scalar inlet value: fixedValue uniform 0
- Scalar outlet/wall/furniture value: zeroGradient
- Molecular scalar diffusivity: 1.5e-5 m2/s
- Turbulent Schmidt number: 0.7
- Effective scalar diffusivity: D + nut / Sct
- simpleFoam production stopping rule: pseudo-time/iteration 1000
