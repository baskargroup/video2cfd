# video2cfd

**360-degree indoor video → semantically labeled, simulation-ready 3D geometry for CFD.**

This repository contains the code of the paper *"Semi-automated reconstruction
of indoor geometry from 360-degree video for CFD-based airflow analysis in
classrooms"* ([arXiv:2609.23425](https://arxiv.org/abs/2609.23425)). It has two
parts:

- the **geometry pipeline** (`pipeline/`), which takes a dense 3D point cloud
  (exported from a NeRF reconstruction) plus the corresponding camera frames and
  produces individually editable STL assets (chairs, tables, seated mannequins)
  inside a room enclosure;
- the **downstream CFD workflow** (`cfd/`), which turns those STLs into an
  OpenFOAM case and runs steady airflow followed by a passive-scalar decay
  simulation.

The pipeline uses Meta's **SAM 3** for concept-prompted segmentation (Step 1) and a
custom 5-step geometry pipeline (Steps 2–5) for the 3D processing. **Steps 2–5 do
not depend on SAM 3** — only the scientific Python stack.

> Reconstruction (Meshroom → COLMAP → Nerfstudio/nerfacto) is upstream of this
> repository and is not included here; see the paper. OpenFOAM itself is not
> redistributed either: the CFD step needs OpenFOAM v2412, installed natively or
> through the container image described in [`cfd/runtime/README.md`](cfd/runtime/README.md).

---

## What's in here

```
video2cfd/
├── pipeline/            # the geometry pipeline (run scripts from this directory)
├── configs/            # ready-to-run geometry configs for the three paper environments
├── assets/stl_templates/   # template meshes for placement (chairs, tables, mannequin)
├── cfd/                # downstream OpenFOAM workflow: case configs, case generator,
│                       #   run scripts, scalar-solver source (see cfd/README.md)
├── docs/               # pipeline.md, reproduce.md (geometry), cfd_reproduce.md (CFD)
└── data/               # (you provide) inputs + outputs — see data/README.md
```

## Pipeline overview

```
Input frames + global NeRF point cloud (.ply)
   │
   ├─[1] segment        SAM 3 text prompt ("chair", "big table") → per-frame masks
   ├─[2] extract_points project the point cloud onto the masks; multi-view consensus
   │                    voting + patch-based depth-band filtering → category cloud
   ├─[2b] filter        octree + union-find connected components; drop small noise
   ├─[3] heal           bridge gaps between fragments (KD-tree graph + interpolation)
   ├─[4] create_stl     template ICP placement (chairs) or procedural meshes (tables);
   │                    optional seated-mannequin insertion → furniture.stl
   ├─[4b] edit_scene    optional browser editor (human QA: delete/move/rotate)
   └─[5] enclose_scene  wrap the scene in a room box (flat or auditorium floor) + vents
   │
   └─> furniture.stl, room.stl, furniture_and_room.stl, per-object STLs,
       and wall-aligned copies in 4_stl/axis_aligned/ (the input of the CFD step)
```

See [`docs/pipeline.md`](docs/pipeline.md) for per-step detail and parameters.

## Install

```bash
# Steps 2-5 (the geometry pipeline) and the Python scripts in cfd/:
pip install -r requirements.txt
# A CUDA PyTorch is recommended for step 2; CPU works (slower).

# Step 1 (segmentation) additionally needs Meta's SAM 3 (separate, SAM License):
pip install -r requirements-segment.txt
# then install sam3 from https://github.com/facebookresearch/sam3
```

## Data layout

The pipeline reads inputs from and writes outputs under `data/` (configurable via
`paths.data_root`). The large inputs (point clouds + frames, ~GBs) are **not** part
of this repo — see [`data/README.md`](data/README.md) for the expected layout and
how to obtain them.

```
data/
├── stl_templates/                      # copy/symlink of assets/stl_templates/
└── projects/<name>/
    ├── input/
    │   ├── point_cloud_cropped.ply      # dense NeRF-exported cloud
    │   ├── transforms.json              # camera poses (COLMAP/Nerfstudio)
    │   ├── dataparser_transforms.json   # Nerfstudio dataparser transform
    │   └── frames/<category>/*.jpg      # frames for segmentation
    └── runs/<run_id>/                   # pipeline outputs (created)
```

## Run

All commands are run from the `pipeline/` directory and take `--config`.

```bash
cd pipeline

# Full pipeline for all enabled categories:
python run_pipeline.py --config ../configs/classroom_a.yaml

# Skip segmentation (reuse existing masks) and run steps 2 onward:
python run_pipeline.py --config ../configs/classroom_a.yaml --steps 2,2b,3,4,5

# Single category / single step:
python run_pipeline.py --config ../configs/classroom_a.yaml --categories chair --steps 4

# Optional browser scene editor (human-in-the-loop QA after step 4):
python 4b_edit_scene.py --config ../configs/classroom_a.yaml

# Standalone step scripts also work:
python 4_create_stl.py --config ../configs/classroom_a.yaml --categories table -v
python 5_enclose_scene.py --config ../configs/classroom_a.yaml
```

## The three paper environments

| Config | Environment | Short name | Room floor |
|--------|-------------|-------------|------------|
| `configs/classroom_a.yaml` | Mixed-furniture classroom | Class A | flat |
| `configs/classroom_b.yaml` | Chair-dominant classroom  | Class B | flat |
| `configs/auditorium.yaml`  | Tiered lecture-hall auditorium | Auditorium | auditorium |

To reproduce the automated STL outputs of the paper, see
[`docs/reproduce.md`](docs/reproduce.md).

## Downstream CFD (OpenFOAM)

`cfd/` generates an OpenFOAM v2412 case from the wall-aligned STLs that step 5
writes to `data/projects/<name>/runs/<run_id>/4_stl/axis_aligned/` and runs it:
`blockMesh` and `snappyHexMesh`, inlet/outlet patches on the ceiling,
`simpleFoam`, then the passive-scalar decay run with the solver
`scalarTransportFoamTurbulent` (source in `cfd/solvers/`). There is one config
per CFD case of the paper in `cfd/configs/`.

Status: the cases are generated so that they match the settings stated in the
paper, and the tests in `cfd/tests/` run without OpenFOAM. The current scripts
have not yet been run in OpenFOAM, the solver source in `cfd/solvers/` is a
re-implementation that has not yet been compiled, and the monitor locations in
the configs are placeholders, so the clearance times of the paper are not
expected to be reproduced as is (see the known limitations linked below).

```bash
# from the repository root
python cfd/scripts/01_prepare_openfoam_case.py \
    --config cfd/configs/class_a_v2_chairs.yaml \
    --output runs/cfd/class_a_v2_chairs

# inside an OpenFOAM v2412 environment (or use the Docker/Apptainer wrappers)
cd runs/cfd/class_a_v2_chairs
NPROCS=8 ./Allrun
```

- How to run it (native OpenFOAM, Docker, Apptainer + SLURM), how to build the
  solver and how to extract the monitor histories and t50:
  [`docs/cfd_reproduce.md`](docs/cfd_reproduce.md).
- What is in the folder: [`cfd/README.md`](cfd/README.md).
- Which settings match the paper, and the known limitations:
  [`cfd/docs/paper_consistency_checklist.md`](cfd/docs/paper_consistency_checklist.md).

## License & citation

This repository is released under the [MIT License](LICENSE), except
`cfd/solvers/scalarTransportFoamTurbulent/`, which is derived from OpenFOAM and
licensed under GPL-3.0-or-later. It depends on several third-party components
(notably **SAM 3** under Meta's SAM License, and OpenFOAM) that are **not**
redistributed here — see [`NOTICE.md`](NOTICE.md). If you use this software,
please cite the paper (arXiv:2609.23425; see [`CITATION.cff`](CITATION.cff)).
