# scalarTransportFoamTurbulent

Passive-scalar transport solver for OpenFOAM v2412 used in the second stage of
the video2cfd CFD workflow (Section 3.5 of the paper). It advects and diffuses
a dimensionless scalar on a **frozen**, previously computed turbulent flow
field and adds the turbulent (eddy) diffusivity of that flow to the molecular
diffusivity.

## Status: read this first

- The production simulations reported in the paper were run with a custom
  solver binary of the same name. That binary is preserved only inside the
  runtime container image (see `cfd/runtime/README.md`). **Its original source
  code is not available.**
- The source in this folder is a **re-implementation**, written from the stock
  OpenFOAM v2412 solver `scalarTransportFoam`
  (`applications/solvers/basic/scalarTransportFoam`) so that it solves the
  scalar equation stated in the paper. It is not the production source.
- **It has not yet been compiled, run, or compared against the production
  binary.** Treat it as unverified until the two checks below have been done.

Recommended verification, in this order:

1. Build it in an OpenFOAM v2412 environment (`wmake`, see below).
2. Run this build and the production binary from the same `simpleFoam`
   solution on a small case and compare the monitor-point histories of `T`.

Details of the production binary that are unknown, and may therefore differ
from this re-implementation: how it obtained `nut` (read from file, as here,
or through a turbulence-model object), whether it re-wrote `nut`, and its log
output.

## Equation

```math
\frac{\partial C}{\partial t} + \nabla \cdot (\mathbf{U} C)
- \nabla \cdot \left[ \left( D + \frac{\nu_t}{Sc_t} \right) \nabla C \right] = 0
```

In plain text: `dC/dt + div(U C) - div((D + nu_t/Sc_t) grad C) = 0`.

| Paper symbol | OpenFOAM name | Where it comes from |
|---|---|---|
| `C` (scalar) | field `T` | start time directory, then solved |
| `U` (velocity) | field `U`, face flux `phi` | start time directory, frozen |
| `nu_t` (turbulent viscosity) | field `nut` | start time directory, frozen |
| `D` (molecular diffusivity) | `DT` | `constant/transportProperties` |
| `Sc_t` (turbulent Schmidt number) | `Sct` | `constant/transportProperties` |

The paper uses `D = 1.5e-5 m2/s` and `Sc_t = 0.7`. As in the stock solver, a
source term can be added through `fvOptions`; the paper's cases use none.

## What differs from the stock solver

Everything not listed here is unchanged from `scalarTransportFoam` (v2412).

| File | Change |
|---|---|
| `createFields.H` | reads `Sct` (dimensionless) from `transportProperties`; reads the field `nut` from the start time directory; builds `Deff = DT + nut/Sct` once; prints `DT`, `Sct` and the min/max of `nut` and `Deff` |
| `scalarTransportFoamTurbulent.C` | `fvm::laplacian(DT, T)` becomes `fvm::laplacian(Deff, T)`; solver renamed |
| `Make/files` | executable name; installs to `$FOAM_USER_APPBIN` instead of `$FOAM_APPBIN` |
| `Make/options` | additionally links the turbulence libraries (see Troubleshooting) |

Behaviour worth knowing:

- `U`, `phi` and `nut` are read once and never updated. They are written again
  with `T` at every write time (the stock solver already does this for `U`
  and `phi`), so every written time directory is a complete restart point.
- `phi` is read from the start time directory if it exists, otherwise it is
  interpolated from `U`. Always start from a directory written by the flow
  solver so that the conservative flux is used.
- The boundary conditions of `nut` are never evaluated; wall-function patches
  keep the values stored in the file.
- `Deff` is not written to disk.
- If `nut` is zero everywhere (for example when starting from the initial `0`
  directory instead of the flow solution), the solver reduces to the stock
  laminar `scalarTransportFoam`. The log line `min/max(nut)` shows which case
  you are in.

## Required input

`constant/transportProperties`:

```text
DT              DT  [0 2 -1 0 0 0 0] 1.5e-05;
Sct             Sct [0 0 0 0 0 0 0] 0.7;
```

Start time directory (the last time written by `simpleFoam`): `T`, `U` and
`nut` (required) and `phi` (recommended; if it is missing, the flux is
interpolated from `U`).

Entries the solver requests from the case dictionaries (a `default` entry of
the right kind satisfies each of them):

| Dictionary | Entry | Value used in the paper |
|---|---|---|
| `fvSchemes` `ddtSchemes` | `ddt(T)` | `Euler` |
| `fvSchemes` `gradSchemes` | `grad(T)` | `Gauss linear` |
| `fvSchemes` `divSchemes` | `div(phi,T)` | `Gauss upwind` |
| `fvSchemes` `laplacianSchemes` | `laplacian(Deff,T)` | `Gauss linear corrected` |
| `fvSchemes` `interpolationSchemes` | `flux(U)` | `linear` |
| `fvSolution` `solvers` | `T` | `PBiCGStab`, `DILU`, tolerance `1e-8`, relTol `0` |
| `fvSolution` `SIMPLE` | `nNonOrthogonalCorrectors` (optional, default 0) | `0` |
| `fvSolution` `relaxationFactors` `equations` | `T` (optional) | `1.0` |

Note that the stock solver asks for `laplacian(DT,T)`; this solver asks for
`laplacian(Deff,T)`.

## Build

A development installation of OpenFOAM v2412 (www.openfoam.com) is required,
that is, one that provides `wmake` and the headers. For example the
`openfoam2412-default` package, or the `opencfd/openfoam-default:2412`
container image.

```bash
# 1. Load the OpenFOAM v2412 environment (pick the line that matches your system)
source /usr/lib/openfoam/openfoam2412/etc/bashrc        # packages, opencfd images
# source $HOME/OpenFOAM/OpenFOAM-v2412/etc/bashrc       # source build
# module load openfoam/v2412                            # HPC module (site specific)

# 2. Build
cd cfd/solvers/scalarTransportFoamTurbulent
wmake

# 3. Check
which scalarTransportFoamTurbulent
scalarTransportFoamTurbulent -help
```

The executable is installed as `$FOAM_USER_APPBIN/scalarTransportFoamTurbulent`,
which is on `PATH` once the OpenFOAM environment is loaded. `wclean` removes
the build products.

The runtime container image used for the paper already contains the
production binary under the same name. If you build this source inside that
image, `which scalarTransportFoamTurbulent` tells you which of the two will be
run.

## Use in the workflow

The solver is run from the case directory after `simpleFoam` has finished:

1. Copy the initial scalar field into the last flow time directory
   (`0/T` to `<latestTime>/T`; for a decomposed case, inside every
   `processorN` directory).
2. Replace `system/controlDict` with `system/controlDict.scalar`
   (`startFrom latestTime`).
3. Run `scalarTransportFoamTurbulent`, or
   `mpirun -np <N> scalarTransportFoamTurbulent -parallel`.

The generated case's `Allrun` script performs these steps. To continue an
interrupted scalar run, simply start the solver again with
`startFrom latestTime`.

## Troubleshooting

- `Unknown patchField type nutkWallFunction` while reading `nut`: the
  turbulence library that defines the wall-function boundary conditions
  stored in `nut` was not loaded. The generated `system/controlDict.scalar`
  contains `libs (turbulenceModels);`, which loads it independently of how the
  solver binary was linked, so this should only occur with a hand-written
  controlDict: add that entry, or check that `Make/options` still lists
  `-lturbulenceModels` and rebuild.
- The field `nut` cannot be found: the start time directory was not written
  by the flow solver, or the run is starting from the wrong time.
- The scheme `laplacian(Deff,T)` is reported as missing: add that entry, or a
  `default`, to `laplacianSchemes` in `system/fvSchemes`.
- The run stops early with `SIMPLE solution converged`: the `SIMPLE`
  dictionary contains a `residualControl` entry that matches `T`. Remove it;
  the scalar run is meant to stop at `endTime`.

## Licence

The files in this folder are derived from OpenFOAM and are licensed under the
**GNU General Public License, version 3 or (at your option) any later
version** (GPL-3.0-or-later). The full licence text is in `COPYING`. The
original copyright notice (OpenFOAM Foundation) is kept in the source header,
together with a notice describing the modifications.

This applies to this folder only. The rest of the video2cfd repository is
released under the MIT License (see `LICENSE` in the repository root).

This offering is not approved or endorsed by OpenCFD Limited, producer and
distributor of the OpenFOAM software via www.openfoam.com, and owner of the
OPENFOAM and OpenCFD trade marks.
