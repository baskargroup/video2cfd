# Third-Party Notices

The `video2cfd` source code in this repository is released under the
MIT License (see `LICENSE`), with one exception: the solver source in
`cfd/solvers/` is derived from OpenFOAM and is licensed under
GPL-3.0-or-later (see "Code derived from OpenFOAM" below).

The repository depends on, but does **not** redistribute, the third-party
components in the table. Each is obtained from its own source and is governed
by its own license.

## Required external software (not bundled)

| Component | Used in | License | Source |
|-----------|---------|---------|--------|
| **SAM 3** (Segment Anything Model 3) | Step 1 (segmentation) | **SAM License** (Meta) — *source-available, not OSI* | https://github.com/facebookresearch/sam3 |
| Meshroom / AliceVision | upstream reconstruction (equirectangular→pinhole) | MPLv2 / others | https://alicevision.org |
| COLMAP | upstream reconstruction (SfM) | BSD | https://colmap.github.io |
| Nerfstudio (`nerfacto`) | upstream reconstruction (NeRF + point-cloud export) | Apache-2.0 | https://nerf.studio |
| OpenFOAM v2412 (OpenCFD/ESI) | downstream CFD step in `cfd/` (run natively or via container) | GPL-3.0-or-later | https://www.openfoam.com |
| ParaView (optional) | optional quick-look rendering (`cfd/scripts/07_render_paraview.sh`) | BSD-3-Clause | https://www.paraview.org |

> **Important:** SAM 3 is governed by Meta's **SAM License**, which is *not* a
> standard open-source license. It is a runtime dependency of Step 1 only and is
> not included here. Review and comply with its terms separately. Steps 2–5 do
> not require SAM 3.

The CFD run scripts can use a container image that contains OpenFOAM v2412
binaries (see `cfd/runtime/README.md`). The image is pulled from its registry;
it is not part of this repository.

## Code derived from OpenFOAM (`cfd/solvers/`)

`cfd/solvers/scalarTransportFoamTurbulent/` is a modified copy of the
OpenFOAM v2412 solver `scalarTransportFoam`
(`applications/solvers/basic/scalarTransportFoam`). The files in that folder
are licensed under the GNU General Public License, version 3 or (at your
option) any later version (GPL-3.0-or-later). The licence text is in
`cfd/solvers/scalarTransportFoamTurbulent/COPYING`; the original copyright
notice and a description of the modifications are kept in the source headers.
The MIT License of the rest of the repository does not apply to that folder.

This offering is not approved or endorsed by OpenCFD Limited, producer and
distributor of the OpenFOAM software via www.openfoam.com, and owner of the
OPENFOAM and OpenCFD trade marks.

## Python runtime dependencies

numpy, pandas, scipy, networkx, open3d, opencv-python, pillow, trimesh, PyYAML,
torch (and, optionally, manifold3d) — each under its own permissive license.
The CFD scripts in `cfd/scripts/` need only numpy, trimesh, PyYAML, scipy and
networkx.

## Bundled STL templates (`assets/stl_templates/`)

The repository bundles a small set of STL template meshes used for template-based
placement and mannequin insertion (`chair_*.stl`, `table_*.stl`,
`person_sitting_1.stl`, `teacher.stl`). These models were obtained from publicly
available online CAD repositories that provide them as free-to-download assets,
and are redistributed here for convenience. If you are a rights holder of any
bundled model and have concerns about its inclusion, please contact the authors.
