# Step 4: STL Creation — Technical Writeup

## TL;DR

This step takes 3D point clouds (each representing a detected object like a chair or table) and turns them into solid STL meshes ready for simulation/visualization.

There are two ways it does this:
- **Template mode:** Takes a pre-made STL file (e.g. a chair model) and places a scaled, rotated copy at each point cloud location. Rotation is either a fixed angle from config or automatically determined using ICP (iterative closest point) alignment against the point cloud.
- **Procedural mode:** Generates simple geometry (currently just tables — a box tabletop + 4 box legs) sized to match each point cloud's dimensions.

Both modes classify point clouds by size (undersized objects can be skipped as false positives, oversized ones get multiple placements), and overlapping placements are automatically merged. The final output is per-object STLs and a combined `furniture.stl`.

The code is split across 4 files: `4_create_stl.py` (orchestrator), `stl_template_placer.py`, `stl_procedural_placer.py`, and `stl_utils.py` (shared geometry/ICP/IO utilities).

---

## Detailed Reference

### File Overview

| File | Lines | Role |
|------|-------|------|
| `4_create_stl.py` | 241 | Entry point and orchestrator — dispatches to the correct placer per category |
| `stl_template_placer.py` | 421 | Template-based placement: scale, orient (ICP or fixed), classify, resolve overlaps |
| `stl_procedural_placer.py` | 478 | Procedural generation: creates table mesh from dimensions, PCA + ICP orientation |
| `stl_utils.py` | 761 | Shared utilities: data classes, point cloud I/O, ICP, geometry, overlap resolution, saving |

---

## Entry Point: `4_create_stl.py`

### `main()` (line 101)
CLI entry point. Parses `--config`, `--categories`, `--verbose`. Creates a `PathResolver`, estimates the scene vertical axis, then loops over categories.

### `process_category()` (line 38)
Dispatcher. Reads `placement.mode` from the category config and calls either:
- `run_template_placement()` from `stl_template_placer.py`
- `run_procedural_placement()` from `stl_procedural_placer.py`

Both return the same signature: `(List[PlacedObject], List[trimesh.Trimesh], dict)`.

### `estimate_scene_vertical_axis()` (line 76)
Determines the "up" direction. Uses `scene.vertical_axis` from config if set, otherwise estimates it from the bottom 10th-percentile points across all categories using SVD on the floor plane.

### Post-processing (lines 142–237)
After all categories are processed:
1. Runs watertightness checks on each placed mesh and the combined result
2. Saves per-category STLs via `save_results()`
3. Concatenates all meshes into `furniture.stl`
4. Writes `watertight_log.json` and `scene_log.json`

---

## Template Placement: `stl_template_placer.py`

### `run_template_placement()` (line 90)
Main function. Flow:

1. **Load point clouds** from the best available input directory (healed > filtered > raw)
2. **Compute reference size** — 75th percentile of all point cloud bounding boxes
3. **Load and scale STL template** — scale factor = `cbrt(product(reference_size / stl_size)) * scale_adjustment`
4. **Classify each point cloud** by footprint ratio relative to the scaled template:
   - `ratio = point_cloud_footprint / reference_footprint`
   - `< min_footprint_ratio` → undersized (optionally skipped)
   - `> max_footprint_ratio` → oversized (grid placement)
   - otherwise → normal (single placement)
5. **Place meshes** at each point cloud's XY center, at the median Z of all objects
6. **Resolve overlaps** — greedy removal by bounding-box overlap ratio

### `orient_and_place_mesh()` (line 33)
Handles orientation for a single placement:
- `use_icp=false`: applies fixed `rotation_degrees` as Z-rotation
- `use_icp=true`: calls `find_best_orientation_icp()` with 8 coarse candidates (45° steps) then fine refinement (±20° in 5° steps). Falls back to fixed rotation if ICP fitness < threshold.

### Classification Actions
| Classification | Action |
|---|---|
| normal | Place 1 STL at point cloud center |
| undersized | Skip (if `skip_undersized: true`) or place anyway |
| oversized | Grid placement: divide bounding box into cells, place one STL per cell |

### Overlap Resolution
Candidates are sorted by point count (descending). For each candidate, its bounding box is checked against all accepted placements. If `intersection_volume / smaller_volume >= merge_overlap_ratio`, the candidate is discarded.

---

## Procedural Placement: `stl_procedural_placer.py`

### `run_procedural_placement()` (line 333)
Main function. Currently only supports `category='table'`. Flow:

1. **Load and filter point clouds** — skip if `< min_points` or `< min_footprint`
2. **Compute global floor Z** — 10th percentile of per-cloud minimum Z values, so all tables share a consistent ground plane
3. **Process each cloud** via `process_single_table()`

### `process_single_table()` (line 170)
Generates one table mesh:

1. **Determine orientation and dimensions:**
   - `use_icp=true`: PCA on XY plane → principal axis angle, oriented bounding box for width/depth
   - `use_icp=false`: axis-aligned bounding box for width/depth, fixed `rotation_degrees`
   - Convention: width ≥ depth (swaps + 90° rotation if needed)
2. **Create procedural mesh** via `create_procedural_table()`: a box tabletop + 4 box legs
3. **Apply Z-rotation** from PCA or config
4. **Position** at XY center of point cloud, Z at `floor_z`
5. **ICP refinement** (if enabled): refine Z-rotation and XY translation. Only applied if fitness ≥ threshold.

### `create_procedural_table()` (line 49)
Generates a simple table mesh centered at origin:
- Tabletop: box at `(0, 0, height - thickness/2)` with dimensions `(width, depth, thickness)`
- 4 legs: square boxes from `Z=0` to bottom of tabletop, inset from edges by `leg_inset`

### `estimate_orientation_pca()` (line 94)
Projects points to XY, computes covariance matrix, extracts principal eigenvector, returns Z-rotation angle.

### `refine_orientation_icp()` (line 137)
Runs point-to-plane ICP between sampled mesh surface points and the target cloud. Extracts only the Z-rotation component from the 4×4 transform using `atan2(R[1,0], R[0,0])`.

---

## Shared Utilities: `stl_utils.py`

### Data Classes

**`PlacedObject`** (line 147) — Record for each placed mesh:
- `category`, `source` (filename stem), `position`, `classification`
- `footprint`, `point_count`, `rotation_angle`, `icp_fitness`
- `mesh_bounds` (min/max arrays for overlap checks)
- `mode` ("template" or "procedural")
- `dimensions` (optional, for procedural: width/depth/height)

**`MeshInfo`** (line 163) — Loaded STL template with computed scale and metrics.

### Point Cloud I/O
- `load_point_cloud()` — auto-detects `.ply` (via Open3D) or `.txt` (numpy, `X Y Z R G B` format)
- `find_point_cloud_files()` — globs `*.txt` and `*.ply`, sorted

### ICP Functions

**`find_best_orientation_icp()`** (line 356) — Two-phase search:
1. Phase 1: 8 angles at 45° intervals from `base_rotation_degrees`
2. Phase 2: ±20° around best in 5° steps
- Builds the target point cloud with normals once, reuses for all candidates
- Uses adaptive threshold: `max(icp_threshold, target_spacing * 3)`

**`run_icp_alignment()`** (line 325) — Wraps Open3D's point-to-plane ICP. Returns 4×4 transform and fitness score (0–1).

### Geometry
- `get_bounds()` — bounding box dict with `min`, `max`, `size`, `center`, `footprint`, `volume`
- `create_rotation_matrix()` — Rodrigues' rotation around arbitrary axis
- `create_rotation_matrix_z()` — Z-axis rotation
- `estimate_vertical_axis()` — SVD on floor-level points across all clouds

### Overlap Resolution
`resolve_overlaps()` (line 488) — greedy algorithm: sort by point count descending, accept if no existing accepted placement overlaps above `merge_overlap_ratio`.

### Watertightness
`check_mesh_watertight()` — reports boundary edges, non-manifold edges, degenerate faces. Used for quality assurance logging.

### Result Saving
`save_results()` — writes individual STLs to `{category}/individual/`, combined STL, and `scene_log.json` with placement metadata.

### Input Directory Resolution
`find_input_dir()` — checks directories in priority order: `merged_groups` (healed) → `filtered` → `raw`. Returns the first that contains `.txt` or `.ply` files.

---

## Configuration (relevant section from YAML)

```yaml
categories:
  chair:
    placement:
      mode: "template"          # or "procedural"
      stl_file: "chair_2.stl"   # Template STL filename (template mode)

      # Orientation
      use_icp: true             # false = fixed rotation, true = ICP search
      rotation_degrees: -120    # Fixed angle or ICP base offset
      icp_threshold: 0.002      # ICP distance threshold (meters)
      icp_iterations: 500       # Max ICP iterations
      icp_fitness_threshold: 0.05  # Min fitness to accept ICP result

      # Transform
      scale_adjustment: 0.8     # Multiplier on auto-calculated scale
      z_offset: 0.0             # Vertical shift (meters)

      # Classification (template mode)
      single_footprint: 0.026   # Manual reference footprint (m²)
      use_stl_footprint: false  # true = use scaled STL footprint instead
      min_footprint_ratio: 0.5  # Below → undersized
      max_footprint_ratio: 1.5  # Above → oversized
      skip_undersized: true     # true = don't place, false = place anyway

      # Overlap
      merge_overlap_ratio: 0.2  # bbox overlap threshold for merging

  table:
    placement:
      mode: "procedural"
      use_icp: true
      table_height: 0.2        # Fixed table height (meters)
      tabletop_thickness: 0.03
      leg_width: 0.005
      leg_inset: 0.002
      min_footprint: 0.02      # Skip clouds smaller than this (m²)
```

---

## Call Graph

```
4_create_stl.py::main()
├── PathResolver(config)
├── estimate_scene_vertical_axis()
│   └── estimate_vertical_axis()          [stl_utils]
│
├── for each category:
│   └── process_category()
│       ├── [template] run_template_placement()       [stl_template_placer]
│       │   ├── find_input_dir()                      [stl_utils]
│       │   ├── load_and_validate_stl()               [stl_utils]
│       │   ├── for each point cloud:
│       │   │   ├── load_point_cloud()                [stl_utils]
│       │   │   ├── classify (undersized/normal/oversized)
│       │   │   └── orient_and_place_mesh()
│       │   │       └── find_best_orientation_icp()   [stl_utils]
│       │   │           └── run_icp_alignment()       [stl_utils]
│       │   └── resolve_overlaps()                    [stl_utils]
│       │
│       └── [procedural] run_procedural_placement()   [stl_procedural_placer]
│           ├── find_input_dir()                      [stl_utils]
│           ├── compute global floor_z
│           └── for each point cloud:
│               └── process_single_table()
│                   ├── estimate_orientation_pca()
│                   ├── create_procedural_table()
│                   └── refine_orientation_icp()
│                       └── run_icp_alignment()       [stl_utils]
│
├── check_mesh_watertight() for all meshes            [stl_utils]
├── save_results() per category                       [stl_utils]
└── concatenate → furniture.stl
```

---

## Key Dependencies

| Library | Usage |
|---------|-------|
| `trimesh` | Mesh creation, manipulation, export, watertightness checks |
| `open3d` | Point cloud I/O, KD-tree, ICP registration, visualization |
| `numpy` | All numerical operations |
| `manifold3d` | Optional: boolean union of watertight meshes |

---

## Output Files

```
runs/{run_id}/4_stl/
├── {category}/
│   ├── individual/
│   │   ├── {category}_001.stl
│   │   ├── {category}_002.stl
│   │   └── ...
│   ├── {category}_combined.stl
│   └── scene_log.json          # Per-category placement metadata
├── furniture.stl               # All categories combined
├── watertight_log.json         # Quality checks for all meshes
└── scene_log.json              # Scene-level metadata
```
