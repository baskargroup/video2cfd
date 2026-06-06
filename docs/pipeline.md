# Pipeline detail

All scripts live in `pipeline/` and are run from that directory. Every script
takes `--config <path>`. `path_resolver.py` (shared) loads the YAML config and
resolves all input/output paths.

## Scripts

| File | Step | Role | Needs SAM 3 |
|------|------|------|:----------:|
| `run_pipeline.py` | — | Master orchestrator; runs any subset of steps | only if step 1 |
| `1_segment.py` | 1 | Segment frames with SAM 3 (text prompt → per-frame masks) | yes |
| `2_extract_points.py` | 2 | Project the global point cloud onto masks; multi-view consensus voting + patch depth-band filtering | no |
| `2b_filter_pointcloud.py` | 2b | Octree/union-find (or DBSCAN) connected components; remove small noise | no |
| `3_heal_pointclouds.py` | 3 | Bridge gaps between fragments (KD-tree graph + linear interpolation) | no |
| `4_create_stl.py` | 4 | Template ICP placement or procedural mesh generation; optional mannequins | no |
| `4b_edit_scene.py` | 4b | Browser Three.js scene editor (human-in-the-loop QA) | no |
| `5_enclose_scene.py` | 5 | Room enclosure box (flat / auditorium floor) + ceiling vents | no |
| `path_resolver.py` | — | Config loading + path resolution | no |
| `stl_utils.py` | — | Shared geometry utilities (ICP, mesh I/O) | no |
| `stl_template_placer.py` | — | Template placement via multi-angle ICP | no |
| `stl_procedural_placer.py` | — | Procedural table mesh from point-cloud dimensions | no |
| `combine_stls.py` | — | Combine per-category STLs | no |

## Output structure

```
data/projects/<name>/runs/<run_id>/
  1_segmentation/<category>/<frame>/      # masks (from step 1)
  2_extracted/<category>/{raw,filtered}/  # category point cloud
  3_healed/<category>/merged_groups/      # healed per-instance clouds
  4_stl/
    <category>/                           # per-object STLs
    furniture.stl                         # combined automated furniture
    room.stl                              # step-5 enclosure
    furniture_and_room.stl                # furniture + room
    (furniture_edited*.stl)               # produced only by the 4b editor (human QA)
```

## Step 4 placement modes

- **`mode: "template"`** (chairs, and optionally tables): place a pre-made STL at
  each detected object. Orientation is a fixed `rotation_degrees` or multi-angle
  ICP (8 candidates, best fitness wins). Point clouds are classified by footprint
  ratio → undersized / normal / oversized; undersized can be skipped as false
  positives; overlapping placements are merged. An optional seated mannequin can be
  placed on each chair (`placement.mannequin`).
- **`mode: "procedural"`** (tables): generate a box tabletop + 4 legs sized to the
  point cloud (`table_height`, `tabletop_thickness`, `leg_width`, `leg_inset`).
  Orientation is a PCA estimate + Z-axis ICP refinement.

A longer narrative of step 4 is in `pipeline/step4_create_stl_writeup.md`.

## Step 2 key parameters

| Param | Effect |
|-------|--------|
| `min_views` (τ_mv) | Multi-view consensus: a point must fall inside the mask in ≥ N frames |
| `depth_band_sigma` (σ) | MADs around local median depth retained (lower = stricter) |
| `depth_patch_size` (S) | Pixel patch size for local depth estimation |
| `z_threshold` | Per-pixel Z-buffer tolerance (used when `use_zbuffer: true`) |

## Config structure

`defaults:` provides global fallbacks; each entry under `categories:` (e.g. `chair`,
`table`) overrides them. `PathResolver.get_step_config(category, step)` merges them
(category wins). `room:` controls the step-5 enclosure (`floor_type: flat` or
`auditorium`, padding/height, and optional `vents`). `paths.run_id: "auto"` makes a
timestamped run directory.
