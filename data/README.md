# Data directory

The pipeline reads its inputs from and writes its outputs under this `data/`
directory (set by `paths.data_root` in each config; the shipped configs use
`../data`). **The large inputs are not stored in git** — this folder is
`.gitignore`d except for this README.

## Expected layout

```
data/
├── stl_templates/                       # copy or symlink of ../assets/stl_templates/
│   ├── chair_3.stl  table_2.stl  person_sitting_1.stl  ...
└── projects/<name>/
    └── input/
        ├── point_cloud_cropped.ply       # dense NeRF-exported point cloud (GBs)
        ├── transforms.json               # camera intrinsics + poses
        ├── dataparser_transforms.json    # Nerfstudio dataparser transform
        └── frames/
            ├── chair/*.jpg               # frames for the "chair" category
            └── big_table/*.jpg           # frames for the "table" category
```

Outputs are written to `data/projects/<name>/runs/<run_id>/` (see `docs/pipeline.md`).

## Project names for the three paper environments

| Config | `project.name` |
|--------|----------------|
| `configs/classroom_a.yaml` | `classroom_v2_v2` |
| `configs/classroom_b.yaml` | `classroom_v1` |
| `configs/auditorium.yaml`  | `audi_v1` |

## Getting the input data

Set up the `stl_templates/` folder (copy or symlink `../assets/stl_templates/`),
then place each environment's `input/` under `data/projects/<name>/`.

The reconstruction inputs (NeRF point clouds, camera transforms, and frames; ~7 GB
total for the three environments) are available from the authors on reasonable
request.

If you generate your own data, produce `point_cloud_cropped.ply` by training a
Nerfstudio `nerfacto` model and running `ns-export pointcloud`, and place the
matching `transforms.json` / `dataparser_transforms.json` and per-category frames
as shown above.
