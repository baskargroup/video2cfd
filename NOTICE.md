# Third-Party Notices

The `video2cfd` source code in this repository is released under the
MIT License (see `LICENSE`). It depends on, but does **not** redistribute, the
third-party components below. Each is obtained from its own source and is
governed by its own license.

## Required external software (not bundled)

| Component | Used in | License | Source |
|-----------|---------|---------|--------|
| **SAM 3** (Segment Anything Model 3) | Step 1 (segmentation) | **SAM License** (Meta) — *source-available, not OSI* | https://github.com/facebookresearch/sam3 |
| Meshroom / AliceVision | upstream reconstruction (equirectangular→pinhole) | MPLv2 / others | https://alicevision.org |
| COLMAP | upstream reconstruction (SfM) | BSD | https://colmap.github.io |
| Nerfstudio (`nerfacto`) | upstream reconstruction (NeRF + point-cloud export) | Apache-2.0 | https://nerf.studio |
| OpenFOAM | downstream CFD (outside this repo) | GPLv3 | https://openfoam.org |

> **Important:** SAM 3 is governed by Meta's **SAM License**, which is *not* a
> standard open-source license. It is a runtime dependency of Step 1 only and is
> not included here. Review and comply with its terms separately. Steps 2–5 do
> not require SAM 3.

## Python runtime dependencies

numpy, pandas, scipy, networkx, open3d, opencv-python, pillow, trimesh, PyYAML,
torch (and, optionally, manifold3d) — each under its own permissive license.

## Bundled STL templates (`assets/stl_templates/`)

The repository bundles a small set of STL template meshes used for template-based
placement and mannequin insertion (`chair_*.stl`, `table_*.stl`,
`person_sitting_1.stl`, `teacher.stl`). These models were obtained from publicly
available online CAD repositories that provide them as free-to-download assets,
and are redistributed here for convenience. If you are a rights holder of any
bundled model and have concerns about its inclusion, please contact the authors.
