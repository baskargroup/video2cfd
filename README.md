# video2cfd

**360-degree indoor video → semantically labeled, simulation-ready 3D geometry for CFD.**

This repository contains the semi-automated reconstruction-to-CFD pipeline from the
paper *"End-to-End Automated Classroom Reconstruction for CFD."* It takes a dense
3D point cloud (exported from a NeRF reconstruction) plus the corresponding camera
frames and produces individually editable, watertight STL assets (chairs, tables,
seated mannequins) inside a room enclosure, ready for meshing and CFD in OpenFOAM.

The pipeline uses Meta's **SAM 3** for concept-prompted segmentation (Step 1) and a
custom 5-step geometry pipeline (Steps 2–5) for the 3D processing. **Steps 2–5 do
not depend on SAM 3** — only the scientific Python stack.

> Reconstruction (Meshroom → COLMAP → Nerfstudio/nerfacto) and CFD (OpenFOAM) are
> upstream/downstream of this repository and are not included here; see the paper.

---

## What's in here

```
video2cfd/
├── pipeline/            # the pipeline (run scripts from this directory)
├── configs/            # ready-to-run configs for the three paper environments
├── assets/stl_templates/   # template meshes for placement (chairs, tables, mannequin)
├── docs/               # pipeline.md (detail) and reproduce.md (the three paper cases)
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
   └─> furniture.stl, room.stl, furniture_and_room.stl, per-object STLs
```

See [`docs/pipeline.md`](docs/pipeline.md) for per-step detail and parameters.

## Install

```bash
# Steps 2-5 (the geometry pipeline):
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
the download link.

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

| Config | Environment | Paper label | Room floor |
|--------|-------------|-------------|------------|
| `configs/classroom_a.yaml` | Mixed-furniture classroom | Class A | flat |
| `configs/classroom_b.yaml` | Chair-dominant classroom  | Class B | flat |
| `configs/auditorium.yaml`  | Tiered lecture-hall auditorium | Auditorium | auditorium |

To reproduce the automated STL outputs of the paper, see
[`docs/reproduce.md`](docs/reproduce.md).

## License & citation

This pipeline is released under the [MIT License](LICENSE). It depends on several
third-party components (notably **SAM 3** under Meta's SAM License) that are **not**
redistributed here — see [`NOTICE.md`](NOTICE.md). If you use this software, please
cite the paper (see [`CITATION.cff`](CITATION.cff)).
