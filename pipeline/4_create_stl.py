#!/usr/bin/env python3
"""
Step 4: Unified STL Scene Creation

Dispatches to template-based or procedural placement depending on the
category's `placement.mode` config setting.

Usage:
    python 4_create_stl.py --config ../configs/default.yaml
    python 4_create_stl.py --config ../configs/default.yaml --categories chair
    python 4_create_stl.py --config ../configs/default.yaml --categories table --verbose
"""

import argparse
import json
from pathlib import Path
from typing import List, Tuple

import numpy as np
import trimesh

from path_resolver import PathResolver
from stl_utils import (
    PlacedObject,
    load_point_cloud, find_point_cloud_files,
    estimate_vertical_axis,
    check_mesh_watertight, print_watertight_report,
    load_trimesh, load_stl_mesh, get_bounds,
    save_results, find_input_dir,
    NumpyEncoder, create_rotation_matrix, create_rotation_matrix_z,
)


# ============================================================================
# MANNEQUIN PLACEMENT
# ============================================================================
#
# Architecture: mannequin STLs are produced with config values (scale_factor,
# position_offset, pre_rotation) **baked into** the vertex positions.  The
# editor treats its UI controls as **deltas** on top of those baked values:
#
#   final_scale   = config.scale_factor  * editor.scale_factor   (multiply)
#   final_offset  = config.position_offset + editor.offset_{xyz}  (add)
#   final_rot_z   = 0                    + editor.rotation_z      (add)
#
# Neutral editor values (scale=1, offsets=0, rot=0) reproduce the original
# Step 4 output.  The editor's applyMannAdjustments() relies on loading
# CLEAN Step 4 mannequin STLs from mannequins/; if those files are
# overwritten with rebuilt versions (which already include editor deltas),
# the deltas get applied twice.
#
# To prevent this:
#   - mannequins/          is the canonical Step 4 output (read-only)
#   - mannequins_edited/   is produced by rebuild (never loaded by editor)
#   - 4b_edit_scene.py regenerates mannequins/ on every editor launch
# ============================================================================

def get_mannequin_config(resolver: PathResolver, category: str) -> dict:
    """Get mannequin config for a category. Returns empty dict if disabled."""
    cat_config = resolver.get_category_config(category)
    mannequin_cfg = cat_config.get('placement', {}).get('mannequin', {})
    if not mannequin_cfg.get('enabled', False):
        return {}
    return mannequin_cfg


def load_mannequin_template(resolver: PathResolver, mannequin_cfg: dict):
    """Load and pre-rotate the mannequin STL template. Returns (mesh, bounds) or (None, None)."""
    stl_file = mannequin_cfg.get('stl_file', 'person_sitting_1.stl')
    stl_path = resolver.stl_templates_dir / stl_file

    template = load_stl_mesh(stl_path)
    if template is None:
        print(f"    Mannequin STL not found: {stl_path}")
        return None, None

    # Apply pre-rotation to fix STL orientation (e.g. lying on back → sitting)
    pre_rot = mannequin_cfg.get('pre_rotation', [0, 0, 0])
    rx, ry, rz = [np.radians(a) for a in pre_rot]

    template.vertices -= get_bounds(template)['center']
    if abs(rx) > 1e-6:
        R = create_rotation_matrix(np.array([1, 0, 0]), np.degrees(rx))
        template.vertices = template.vertices @ R.T
    if abs(ry) > 1e-6:
        R = create_rotation_matrix(np.array([0, 1, 0]), np.degrees(ry))
        template.vertices = template.vertices @ R.T
    if abs(rz) > 1e-6:
        R = create_rotation_matrix(np.array([0, 0, 1]), np.degrees(rz))
        template.vertices = template.vertices @ R.T

    bounds = get_bounds(template)
    return template, bounds


def _place_single_mannequin(template: trimesh.Trimesh,
                            mann_bounds: dict,
                            chair_mesh: trimesh.Trimesh,
                            rotation_angle: float,
                            extra_scale: float,
                            position_offset: np.ndarray,
                            ) -> trimesh.Trimesh:
    """Place one mannequin on one chair.

    The mannequin is scaled to match the chair's Z-height, rotated by the
    same angle as the chair, and translated to the chair's bounding-box
    centre.  ``position_offset`` is in the chair's local frame (rotated
    into world space before application).

    The output mesh has **world-space coordinates baked into its vertices**.

    Args:
        template:       Pre-rotated mannequin template (from load_mannequin_template).
        mann_bounds:    Bounding box of the template.
        chair_mesh:     The placed chair mesh (world-space vertices).
        rotation_angle: Chair's placement rotation (degrees, same convention
                        as scene_log.json ``rotation_degrees``).
        extra_scale:    config.scale_factor * editor.scale_factor.
        position_offset: config.position_offset + editor.offset (3-vec, metres).

    Returns:
        A new trimesh with world-space vertices.
    """
    chair_bounds = get_bounds(chair_mesh)
    chair_height = chair_bounds['size'][2]

    scale = (chair_height / mann_bounds['size'][2]) * extra_scale

    m = template.copy()
    m.vertices -= mann_bounds['center']
    m.vertices *= scale

    # Rotate by the same angle as the chair so the mannequin faces
    # the same direction (both templates face -Y by default).
    rotation = create_rotation_matrix_z(np.radians(rotation_angle))
    m.vertices = m.vertices @ rotation.T

    # position_offset is in the chair's local frame; rotate to world space.
    rotated_offset = rotation @ position_offset
    m.vertices += chair_bounds['center'] + rotated_offset

    return m


def place_mannequins(resolver: PathResolver,
                     category: str,
                     candidates: List[PlacedObject],
                     chair_meshes: List[trimesh.Trimesh],
                     verbose: bool = True,
                     overrides: dict = None,
                     ) -> List[trimesh.Trimesh]:
    """
    Place a sitting mannequin on each placed chair.

    Args:
        overrides: optional dict with keys 'scale_factor', 'rotation_z',
                   'offset_x', 'offset_y', 'offset_z' from the editor UI.
                   These override config values.

    Returns a list of mannequin meshes (one per chair).
    Does NOT modify candidates or chair_meshes.
    """
    mannequin_cfg = get_mannequin_config(resolver, category)
    if not mannequin_cfg:
        return []

    extra_scale = mannequin_cfg.get('scale_factor', 1.0)
    stl_file = mannequin_cfg.get('stl_file', 'person_sitting_1.stl')
    cfg_offset = mannequin_cfg.get('position_offset', [0, 0, 0])
    position_offset = np.array(cfg_offset, dtype=np.float64)

    # Editor overrides are deltas on top of config values:
    #   scale_factor is multiplied (1.0 = no change), offsets are added (0.0 = no change)
    if overrides:
        extra_scale *= overrides.get('scale_factor', 1.0)
        position_offset += np.array([
            overrides.get('offset_x', 0.0),
            overrides.get('offset_y', 0.0),
            overrides.get('offset_z', 0.0),
        ], dtype=np.float64)

    template, mann_bounds = load_mannequin_template(resolver, mannequin_cfg)
    if template is None:
        return []

    # Apply extra rotation override from editor
    if overrides and abs(overrides.get('rotation_z', 0)) > 1e-6:
        R = create_rotation_matrix_z(np.radians(overrides['rotation_z']))
        template.vertices = template.vertices @ R.T
        mann_bounds = get_bounds(template)

    print(f"\n    --- Mannequin Placement ---")
    print(f"    STL: {stl_file}")
    print(f"    Pre-rotation: {mannequin_cfg.get('pre_rotation', [0, 0, 0])}")
    print(f"    After pre-rotation size: {mann_bounds['size'][0]:.3f} x {mann_bounds['size'][1]:.3f} x {mann_bounds['size'][2]:.3f}")
    print(f"    Scale factor: {extra_scale}")
    print(f"    Position offset: [{position_offset[0]:.4f}, {position_offset[1]:.4f}, {position_offset[2]:.4f}]")

    mannequin_meshes = []

    for cand, chair_mesh in zip(candidates, chair_meshes):
        m = _place_single_mannequin(
            template, mann_bounds, chair_mesh,
            cand.rotation_angle, extra_scale, position_offset
        )
        mannequin_meshes.append(m)

        if verbose:
            scaled_bounds = get_bounds(m)
            print(f"      {cand.source}: scale={get_bounds(chair_mesh)['size'][2] / mann_bounds['size'][2] * extra_scale:.4f}  "
                  f"size={scaled_bounds['size'][0]:.3f}x{scaled_bounds['size'][1]:.3f}x{scaled_bounds['size'][2]:.3f}")

    print(f"    Mannequins placed: {len(mannequin_meshes)}")
    return mannequin_meshes


def _compute_occupied_indices(n_chairs: int, occupancy_pct: int, seed: int) -> set:
    """Compute which chair indices should receive mannequins based on occupancy %.

    Uses a Linear Congruential Generator (LCG) PRNG with a Fisher-Yates
    shuffle, identical to the JavaScript implementation in
    ``editor/index.html:recomputeOccupiedSet()``.  Both implementations
    MUST stay in sync — if one changes, the other must change too, or the
    editor preview will show different chairs occupied than the rebuild
    actually produces.

    LCG parameters: multiplier=1664525, increment=1013904223,
    modulus=2^32 (masked with 0xFFFFFFFF to emulate unsigned 32-bit
    arithmetic, matching JS ``>>> 0``).
    """
    if occupancy_pct >= 100:
        return set(range(n_chairs))
    if occupancy_pct <= 0:
        return set()

    n_to_fill = round(n_chairs * occupancy_pct / 100)
    if n_to_fill <= 0:
        return set()
    if n_to_fill >= n_chairs:
        return set(range(n_chairs))

    indices = list(range(n_chairs))
    s = seed
    def next_rand():
        nonlocal s
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        return s / 0x100000000

    # Fisher-Yates shuffle (same direction as JS: high to low)
    for i in range(len(indices) - 1, 0, -1):
        j = int(next_rand() * (i + 1))
        indices[i], indices[j] = indices[j], indices[i]

    return set(indices[:n_to_fill])


def place_mannequins_on_meshes(resolver: PathResolver,
                               category: str,
                               chair_meshes: List[trimesh.Trimesh],
                               rotation_angles: List[float],
                               labels: List[str] = None,
                               verbose: bool = True,
                               overrides: dict = None,
                               ) -> List[trimesh.Trimesh]:
    """Place mannequins on a list of chair meshes.

    Used by rebuild_from_edits (with overrides) and regenerate_mannequins
    (without overrides).

    Args:
        overrides: Editor adjustments applied as **deltas** on top of
            config values.  None or empty dict → pure config values.

            scale_factor: multiplied with config (1.0 = no change)
            offset_x/y/z: added to config offset (0.0 = no change)
            rotation_z:   extra Z rotation in degrees (0 = no change)
            occupancy_pct: percentage of chairs to fill (100 = all)
            occupancy_seed: seed for deterministic selection

    Returns:
        List of mannequin meshes.  When occupancy < 100%, the list is
        shorter than chair_meshes (only occupied chairs are included).
    """
    mannequin_cfg = get_mannequin_config(resolver, category)
    if not mannequin_cfg:
        return []

    extra_scale = mannequin_cfg.get('scale_factor', 1.0)
    cfg_offset = mannequin_cfg.get('position_offset', [0, 0, 0])
    position_offset = np.array(cfg_offset, dtype=np.float64)

    occupancy_pct = 100
    occupancy_seed = 0

    if overrides:
        # Editor adjustments are deltas on top of config values:
        #   scale_factor is multiplied (1.0 = no change from config)
        #   offsets are added (0.0 = no change from config)
        extra_scale *= overrides.get('scale_factor', 1.0)
        position_offset += np.array([
            overrides.get('offset_x', 0.0),
            overrides.get('offset_y', 0.0),
            overrides.get('offset_z', 0.0),
        ], dtype=np.float64)
        occupancy_pct = overrides.get('occupancy_pct', 100)
        occupancy_seed = overrides.get('occupancy_seed', 0)

    template, mann_bounds = load_mannequin_template(resolver, mannequin_cfg)
    if template is None:
        return []

    if overrides and abs(overrides.get('rotation_z', 0)) > 1e-6:
        R = create_rotation_matrix_z(np.radians(overrides['rotation_z']))
        template.vertices = template.vertices @ R.T
        mann_bounds = get_bounds(template)

    if labels is None:
        labels = [f"obj_{i}" for i in range(len(chair_meshes))]

    # Determine which chairs get mannequins based on occupancy %
    occupied = _compute_occupied_indices(len(chair_meshes), occupancy_pct, occupancy_seed)

    if occupancy_pct < 100:
        print(f"    Occupancy: {occupancy_pct}% -> {len(occupied)}/{len(chair_meshes)} chairs (seed={occupancy_seed})")

    mannequin_meshes = []

    for i, (chair_mesh, angle, label) in enumerate(zip(chair_meshes, rotation_angles, labels)):
        if i not in occupied:
            continue

        m = _place_single_mannequin(
            template, mann_bounds, chair_mesh,
            angle, extra_scale, position_offset
        )
        mannequin_meshes.append(m)

        if verbose:
            scaled_bounds = get_bounds(m)
            print(f"      {label}: scale={get_bounds(chair_mesh)['size'][2] / mann_bounds['size'][2] * extra_scale:.4f}  "
                  f"size={scaled_bounds['size'][0]:.3f}x{scaled_bounds['size'][1]:.3f}x{scaled_bounds['size'][2]:.3f}")

    return mannequin_meshes


def process_category(resolver: PathResolver,
                     category: str,
                     vertical_axis: np.ndarray,
                     min_points: int = 50,
                     verbose: bool = True
                     ) -> Tuple[List[PlacedObject], List[trimesh.Trimesh], dict]:
    """
    Process a single category using its configured placement mode.

    Returns:
        (candidates, meshes, stats)
    """
    cat_config = resolver.get_category_config(category)
    mode = cat_config.get('placement', {}).get('mode', 'template')

    if mode == 'template':
        from stl_template_placer import run_template_placement
        return run_template_placement(
            resolver, category, vertical_axis,
            min_points=min_points, verbose=verbose
        )
    elif mode == 'procedural':
        from stl_procedural_placer import run_procedural_placement
        return run_procedural_placement(
            resolver, category, vertical_axis,
            min_points=min_points, verbose=verbose
        )
    else:
        raise ValueError(
            f"Unknown placement mode '{mode}' for category '{category}'. "
            f"Must be 'template' or 'procedural'."
        )


# ============================================================================
# VERTICAL AXIS ESTIMATION
# ============================================================================

def estimate_scene_vertical_axis(resolver: PathResolver,
                                  min_points: int = 50) -> np.ndarray:
    """Estimate vertical axis from config or point cloud data."""
    scene_config = resolver.config.get('defaults', {}).get('scene', {})
    vertical_axis_cfg = scene_config.get('vertical_axis')

    if vertical_axis_cfg:
        return np.array(vertical_axis_cfg, dtype=np.float32)

    all_point_clouds = []
    for cat_name in resolver.get_enabled_categories():
        input_dir = find_input_dir(resolver, cat_name)
        if input_dir is not None:
            for pc_file in find_point_cloud_files(input_dir):
                xyz, _ = load_point_cloud(pc_file)
                if xyz is not None and len(xyz) >= min_points:
                    all_point_clouds.append((pc_file, xyz))

    return estimate_vertical_axis(all_point_clouds)


# ============================================================================
# MAIN (standalone usage — run_pipeline.py uses its own orchestration)
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Step 4: Unified STL Scene Creation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Pathways:
  template  + use_icp=false  -> fixed rotation from config
  template  + use_icp=true   -> multi-angle ICP orientation
  procedural + use_icp=false -> fixed rotation + procedural mesh
  procedural + use_icp=true  -> PCA + ICP + procedural mesh

Examples:
  python 4_create_stl.py --config ../configs/default.yaml
  python 4_create_stl.py --config ../configs/default.yaml --categories chair
  python 4_create_stl.py --config ../configs/default.yaml --categories table -v
        """
    )
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    parser.add_argument("--categories", type=str, default=None,
                        help="Comma-separated categories (default: all enabled)")
    parser.add_argument("--verbose", "-v", action="store_true", default=True,
                        help="Show detailed output")

    args = parser.parse_args()
    resolver = PathResolver(args.config)

    categories = ([c.strip() for c in args.categories.split(',')]
                   if args.categories else resolver.get_enabled_categories())

    print("=" * 60)
    print("STEP 4: CREATE STL SCENE")
    print("=" * 60)

    output_dir = resolver.get_stl_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Vertical axis
    print("\nEstimating vertical axis...")
    vertical_axis = estimate_scene_vertical_axis(resolver)
    print(f"  Vertical axis: {vertical_axis}")

    # Process each category
    all_meshes = []
    all_mannequin_meshes = []
    all_stats = {}
    all_wt_checks = []

    for cat_name in categories:
        cat_config = resolver.get_category_config(cat_name)
        placement = cat_config.get('placement', {})
        mode = placement.get('mode', 'template')

        print(f"\n{'_'*60}")
        print(f"  Category: {cat_name} | Mode: {mode} | ICP: {placement.get('use_icp', False)}")
        print(f"{'_'*60}")

        # Check input template watertightness
        if mode == 'template':
            stl_path = resolver.get_stl_template_path(cat_name)
            template = load_trimesh(stl_path)
            if template is not None:
                chk = check_mesh_watertight(template, f"Input template: {stl_path.name}")
                chk['stage'] = 'input_template'
                chk['category'] = cat_name
                all_wt_checks.append(chk)

        try:
            candidates, meshes, stats = process_category(
                resolver, cat_name, vertical_axis,
                verbose=args.verbose
            )
        except (NotImplementedError, ValueError) as e:
            print(f"  ERROR: {e}")
            continue

        if not meshes:
            continue

        # Check individual placed meshes
        print(f"\n    --- Watertight Check: Individual Meshes ({cat_name}) ---")
        n_wt = 0
        for mesh, cand in zip(meshes, candidates):
            chk = check_mesh_watertight(mesh, f"{cand.source} ({cand.mode})", indent=6)
            chk['stage'] = 'individual_placed'
            chk['category'] = cat_name
            all_wt_checks.append(chk)
            if chk['is_watertight']:
                n_wt += 1
        print(f"    Summary: {n_wt}/{len(meshes)} individual meshes are watertight")

        # Save per-category results
        cat_output_dir = resolver.get_stl_dir(cat_name)
        merged = stats.get('merged', []) if isinstance(stats.get('merged'), list) else []
        save_results(
            cat_output_dir, cat_name, meshes, candidates, stats, merged,
            save_individual=True
        )

        all_meshes.extend(meshes)
        all_stats[cat_name] = stats

        # Place mannequins on chairs (additive — does not affect placements)
        mannequin_meshes = place_mannequins(
            resolver, cat_name, candidates, meshes, verbose=args.verbose
        )
        if mannequin_meshes:
            mann_dir = cat_output_dir / "mannequins"
            mann_dir.mkdir(parents=True, exist_ok=True)
            mann_combined = trimesh.util.concatenate(mannequin_meshes)
            mann_combined.export(str(cat_output_dir / f"{cat_name}_mannequins.stl"))
            for i, m in enumerate(mannequin_meshes):
                m.export(str(mann_dir / f"mannequin_{i+1:03d}.stl"))
            print(f"  Saved: {len(mannequin_meshes)} mannequin STLs in {mann_dir}")
            all_mannequin_meshes.extend(mannequin_meshes)

    # Save combined scene — furniture only (no mannequins)
    if all_meshes:
        combined = trimesh.util.concatenate(all_meshes)

        chk = check_mesh_watertight(
            combined, f"furniture.stl ({len(all_meshes)} meshes)")
        chk['stage'] = 'furniture_combined'
        chk['category'] = 'ALL'
        all_wt_checks.append(chk)

        output_stl = output_dir / "furniture.stl"
        combined.export(str(output_stl))
        print(f"\n  Saved: {output_stl}")

        # Save furniture_with_mannequins.stl if mannequins exist
        if all_mannequin_meshes:
            combined_with_mann = trimesh.util.concatenate(all_meshes + all_mannequin_meshes)
            output_mann_stl = output_dir / "furniture_with_mannequins.stl"
            combined_with_mann.export(str(output_mann_stl))
            print(f"  Saved: {output_mann_stl}")

        print_watertight_report(all_wt_checks)

        # Save watertightness log
        wt_log_path = output_dir / "watertight_log.json"
        with open(wt_log_path, 'w') as f:
            json.dump(all_wt_checks, f, indent=2)
        print(f"  Saved: {wt_log_path}")

        # Scene log
        log = {
            'vertical_axis': vertical_axis.tolist(),
            'total_objects': sum(s.get('accepted', 0) for s in all_stats.values()),
            'categories': {k: v for k, v in all_stats.items()},
        }
        log_path = output_dir / "scene_log.json"
        with open(log_path, 'w') as f:
            json.dump(log, f, indent=2, cls=NumpyEncoder)
        print(f"  Saved: {log_path}")

    print("\n" + "=" * 60)
    total = sum(s.get('accepted', 0) for s in all_stats.values())
    print(f"STEP 4 COMPLETE: {total} objects placed")
    print("=" * 60)


if __name__ == "__main__":
    main()