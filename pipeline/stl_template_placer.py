#!/usr/bin/env python3
"""
Template-based STL placement.

Places pre-made STL template meshes at point cloud locations.
Supports:
- Fixed rotation from config (use_icp: false)
- Multi-angle ICP orientation (use_icp: true)
- Classification: undersized / normal / oversized
- Grid placement for oversized point clouds
- Overlap resolution
"""

from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import trimesh

from stl_utils import (
    PlacedObject, MeshInfo,
    load_point_cloud, find_point_cloud_files, get_bounds,
    create_rotation_matrix, create_rotation_matrix_z,
    find_best_orientation_icp, load_and_validate_stl,
    resolve_overlaps, set_deterministic_seed,
    compute_object_zs,
)


# ============================================================================
# ORIENTATION
# ============================================================================

def orient_and_place_mesh(mesh: trimesh.Trimesh,
                          target_points: np.ndarray,
                          position: np.ndarray,
                          scale: float,
                          rotation_degrees: float,
                          vertical_axis: np.ndarray,
                          use_icp: bool,
                          icp_threshold: float,
                          icp_iterations: int,
                          icp_fitness_threshold: float,
                          verbose: bool = False
                          ) -> Tuple[trimesh.Trimesh, float, float]:
    """
    Scale, orient, and position a template mesh.

    Orientation strategies:
      use_icp=False → fixed rotation_degrees around vertical_axis
      use_icp=True  → multi-angle ICP picks best discrete angle, falls back to
                       fixed rotation if fitness too low

    Returns:
        positioned_mesh, final_angle_degrees, icp_fitness
    """
    result_mesh = mesh.copy()
    result_mesh.vertices -= result_mesh.centroid
    result_mesh.vertices *= scale

    if use_icp and len(target_points) >= 50:
        best_angle, best_fitness = find_best_orientation_icp(
            result_mesh, target_points,
            icp_threshold=icp_threshold,
            icp_iterations=icp_iterations,
            base_rotation_degrees=rotation_degrees,
            verbose=verbose
        )
    else:
        best_angle = rotation_degrees
        best_fitness = 0.0

    # If ICP fitness is too low, fall back to config rotation
    if use_icp and best_fitness < icp_fitness_threshold and best_fitness > 0:
        if verbose:
            print(f"        ICP fitness {best_fitness:.3f} < threshold {icp_fitness_threshold}, using config rotation")
        best_angle = rotation_degrees

    # Apply the chosen angle as a direct Z rotation
    rotation = create_rotation_matrix_z(np.radians(best_angle))
    result_mesh.vertices = result_mesh.vertices @ rotation.T
    result_mesh.vertices += position

    return result_mesh, best_angle, best_fitness


# ============================================================================
# MAIN PROCESSING
# ============================================================================

def run_template_placement(resolver, category: str,
                           vertical_axis: np.ndarray,
                           min_points: int = 50,
                           verbose: bool = True
                           ) -> Tuple[List[PlacedObject], List[trimesh.Trimesh], dict]:
    """
    Run template-based STL placement for a category.

    Args:
        resolver: PathResolver
        category: category name (e.g. "chair", "table")
        vertical_axis: estimated vertical axis
        min_points: minimum points to consider a point cloud valid
        verbose: detailed output

    Returns:
        (accepted_candidates, accepted_meshes, stats_dict)
    """
    from stl_utils import find_input_dir

    # Ensure deterministic results across runs
    set_deterministic_seed(42)

    cat_config = resolver.get_category_config(category)
    placement = cat_config.get('placement', {})

    # Read all config parameters
    rotation_degrees = placement.get('rotation_degrees', 0.0)
    scale_adjustment = placement.get('scale_adjustment', 1.0)
    z_offset = placement.get('z_offset', 0.0)
    z_mode = placement.get('z_mode', 'global_median')  # global_median | per_object | floor_plane
    skip_undersized = placement.get('skip_undersized', False)
    use_stl_footprint = placement.get('use_stl_footprint', True)
    single_footprint = placement.get('single_footprint', 0.1)
    min_ratio = placement.get('min_footprint_ratio', 0.5)
    max_ratio = placement.get('max_footprint_ratio', 2.0)
    merge_overlap_ratio = placement.get('merge_overlap_ratio', 0.3)
    use_icp = placement.get('use_icp', False)
    icp_threshold = placement.get('icp_threshold', 0.02)
    icp_iterations = placement.get('icp_iterations', 50)
    icp_fitness_threshold = placement.get('icp_fitness_threshold', 0.3)

    # ---- Header ----
    print(f"\n  [{category}] Template placement")
    print(f"    --- Config ---")
    print(f"    STL file:             {placement.get('stl_file', '?')}")
    print(f"    Orientation:          {'ICP (multi-angle)' if use_icp else f'Fixed ({rotation_degrees}°)'}")
    if use_icp:
        print(f"    ICP threshold:        {icp_threshold}")
        print(f"    ICP iterations:       {icp_iterations}")
        print(f"    ICP fitness min:      {icp_fitness_threshold}")
        print(f"    Base rotation:        {rotation_degrees}°")
    print(f"    Scale adjustment:     {scale_adjustment}")
    print(f"    Z mode:               {z_mode}")
    print(f"    Skip undersized:      {skip_undersized}")
    print(f"    Footprint source:     {'Scaled STL' if use_stl_footprint else f'Config ({single_footprint} m²)'}")
    print(f"    Footprint ratios:     [{min_ratio}, {max_ratio}] (under/over thresholds)")
    print(f"    Merge overlap ratio:  {merge_overlap_ratio}")

    # --- Find input ---
    input_dir = find_input_dir(resolver, category)
    if input_dir is None:
        print(f"    No input directory found")
        return [], [], {}
    print(f"    Input: {input_dir}")

    # --- Load STL template ---
    stl_path = resolver.get_stl_template_path(category)

    # --- Load point clouds (with tracking of skipped files) ---
    pc_files = find_point_cloud_files(input_dir)
    print(f"\n    --- Loading Point Clouds ---")
    print(f"    Found {len(pc_files)} files, min_points={min_points}")

    point_clouds = []
    bounds_list = []
    skipped_too_small = 0
    skipped_load_failed = 0

    for pc_file in pc_files:
        xyz, _ = load_point_cloud(pc_file)
        if xyz is None:
            skipped_load_failed += 1
            continue
        if len(xyz) < min_points:
            skipped_too_small += 1
            continue
        point_clouds.append((pc_file, xyz))
        bounds_list.append(get_bounds(xyz))

    if skipped_load_failed > 0:
        print(f"    ⚠ {skipped_load_failed} files failed to load")
    if skipped_too_small > 0:
        print(f"    ⚠ {skipped_too_small} files skipped (< {min_points} points)")

    if not point_clouds:
        print(f"    No valid point clouds found")
        return [], [], {}
    print(f"    Valid: {len(point_clouds)} point clouds")

    # --- Compute reference size and load STL ---
    sizes = np.array([b['size'] for b in bounds_list])
    reference_size = np.percentile(sizes, 75, axis=0)

    print(f"\n    --- STL Template ---")
    mesh_info = load_and_validate_stl(
        stl_path, category, reference_size,
        scale_adjustment, rotation_degrees, vertical_axis
    )
    if mesh_info is None:
        return [], [], {}
    print(f"    Reference size (75th %ile): {reference_size[0]:.3f} x {reference_size[1]:.3f} x {reference_size[2]:.3f}")
    print(f"    Scale factor: {mesh_info.scale:.4f}")

    # --- Reference footprint for classification ---
    if use_stl_footprint and mesh_info.scaled_footprint > 0:
        ref_footprint = mesh_info.scaled_footprint
        print(f"    Reference footprint (scaled STL): {ref_footprint:.4f} m²")
    else:
        ref_footprint = single_footprint
        print(f"    Reference footprint (config):     {ref_footprint:.4f} m²")

    # --- Classify and display table ---
    print(f"\n    --- Classification ---")
    print(f"    Thresholds: ratio < {min_ratio} → undersized, ratio > {max_ratio} → oversized")
    print(f"    Undersized handling: {'SKIP' if skip_undersized else 'PLACE ANYWAY'}")
    print(f"    {'File':<35} {'Points':>7} {'Footprint':>10} {'Ratio':>7} {'Class':<12} {'Action':<10}")
    print(f"    {'-'*85}")

    classifications = []
    for (pc_file, xyz), bounds in zip(point_clouds, bounds_list):
        ratio = bounds['footprint'] / ref_footprint
        if ratio < min_ratio:
            cls = 'undersized'
        elif ratio > max_ratio:
            cls = 'oversized'
        else:
            cls = 'normal'
        classifications.append((cls, ratio))

        if cls == 'undersized' and skip_undersized:
            action = 'SKIP'
        elif cls == 'oversized':
            n_obj = max(1, round(bounds['footprint'] / ref_footprint))
            action = f'GRID({n_obj})'
        else:
            action = 'PLACE'

        print(f"    {pc_file.stem:<35} {len(xyz):>7,} {bounds['footprint']:>10.4f} {ratio:>7.2f} {cls:<12} {action:<10}")

    n_normal = sum(1 for c, _ in classifications if c == 'normal')
    n_over = sum(1 for c, _ in classifications if c == 'oversized')
    n_under = sum(1 for c, _ in classifications if c == 'undersized')
    n_under_skipped = sum(1 for (c, _) in classifications if c == 'undersized') if skip_undersized else 0

    print(f"    ────────────────────────────────────────────────────────────────────────────────────")
    print(f"    Normal: {n_normal}  |  Oversized: {n_over}  |  Undersized: {n_under}" +
          (f" ({n_under_skipped} skipped)" if skip_undersized and n_under > 0 else ""))

    # Tuning hints
    if n_under > n_normal and not skip_undersized:
        print(f"    💡 Many undersized — consider lowering min_footprint_ratio (currently {min_ratio})")
        print(f"       or lowering single_footprint (currently {single_footprint})")
    if n_over > n_normal:
        print(f"    💡 Many oversized — consider raising max_footprint_ratio (currently {max_ratio})")
        print(f"       or raising single_footprint (currently {single_footprint})")

    # --- Z positions (one per point cloud) ---
    print(f"\n    --- Z Positioning ({z_mode}) ---")
    object_zs = compute_object_zs(bounds_list, z_mode, mesh_info,
                                   z_offset=z_offset, verbose=verbose)

    # --- Place meshes ---
    print(f"\n    --- Placement ---")
    candidates = []
    meshes = []
    skipped_undersized = []
    icp_fallback_count = 0

    for (pc_path, xyz), bounds, (classification, ratio), object_z in zip(
            point_clouds, bounds_list, classifications, object_zs):
        if classification == 'undersized' and skip_undersized:
            skipped_undersized.append(pc_path.stem)
            continue

        if classification == 'oversized':
            # Grid placement
            footprint = bounds['footprint']
            n_objects = max(1, round(footprint / ref_footprint))
            width, depth = bounds['size'][0], bounds['size'][1]
            n_cols = max(1, round(np.sqrt(n_objects * width / depth)))
            n_rows = max(1, round(n_objects / n_cols))
            step_x, step_y = width / n_cols, depth / n_rows

            if verbose:
                print(f"      {pc_path.stem}: GRID {n_cols}x{n_rows} = {n_cols*n_rows} placements (ratio={ratio:.2f})")

            for i in range(n_cols):
                for j in range(n_rows):
                    x = bounds['min'][0] + (i + 0.5) * step_x
                    y = bounds['min'][1] + (j + 0.5) * step_y
                    pos = np.array([x, y, object_z])

                    cell_mask = (
                        (xyz[:, 0] >= bounds['min'][0] + i * step_x) &
                        (xyz[:, 0] < bounds['min'][0] + (i + 1) * step_x) &
                        (xyz[:, 1] >= bounds['min'][1] + j * step_y) &
                        (xyz[:, 1] < bounds['min'][1] + (j + 1) * step_y)
                    )
                    cell_points = xyz[cell_mask] if np.any(cell_mask) else xyz

                    mesh, angle, fitness = orient_and_place_mesh(
                        mesh_info.mesh, cell_points, pos, mesh_info.scale,
                        rotation_degrees, vertical_axis,
                        use_icp, icp_threshold, icp_iterations, icp_fitness_threshold,
                        verbose=False  # Too noisy for grid cells
                    )
                    if use_icp and fitness < icp_fitness_threshold and fitness > 0:
                        icp_fallback_count += 1
                    meshes.append(mesh)
                    mesh_bounds = (mesh.vertices.min(axis=0), mesh.vertices.max(axis=0))
                    candidates.append(PlacedObject(
                        category, f"{pc_path.stem}_g{i}_{j}",
                        pos.tolist(), classification,
                        footprint=footprint, point_count=len(cell_points),
                        rotation_angle=angle, icp_fitness=fitness,
                        mesh_bounds=mesh_bounds, mode="template"
                    ))
        else:
            # Normal / undersized (not skipping)
            pos = np.array([bounds['center'][0], bounds['center'][1], object_z])

            mesh, angle, fitness = orient_and_place_mesh(
                mesh_info.mesh, xyz, pos, mesh_info.scale,
                rotation_degrees, vertical_axis,
                use_icp, icp_threshold, icp_iterations, icp_fitness_threshold,
                verbose=verbose
            )

            if use_icp and fitness < icp_fitness_threshold and fitness > 0:
                icp_fallback_count += 1

            status = f"angle={angle:.0f}°"
            if use_icp:
                status += f"  fitness={fitness:.3f}"
                if fitness < icp_fitness_threshold and fitness > 0:
                    status += " ⚠ FALLBACK"

            print(f"      {pc_path.stem:<35} {classification:<10} ratio={ratio:.2f}  {status}")

            meshes.append(mesh)
            mesh_bounds = (mesh.vertices.min(axis=0), mesh.vertices.max(axis=0))
            candidates.append(PlacedObject(
                category, pc_path.stem,
                pos.tolist(), classification,
                footprint=bounds['footprint'], point_count=len(xyz),
                rotation_angle=angle, icp_fitness=fitness,
                mesh_bounds=mesh_bounds, mode="template"
            ))

    print(f"\n    Candidates generated: {len(candidates)}")
    if use_icp and icp_fallback_count > 0:
        print(f"    ⚠ ICP fallback to config rotation: {icp_fallback_count}/{len(candidates)}")
        print(f"      💡 If many fallbacks, try raising icp_fitness_threshold (currently {icp_fitness_threshold})")
        print(f"         or raising icp_threshold (currently {icp_threshold}) for looser matching")

    # --- Overlap resolution ---
    print(f"\n    --- Overlap Resolution ---")
    if merge_overlap_ratio <= 0:
        print(f"    Disabled (merge_overlap_ratio=0)")
        accepted, accepted_meshes, merged = candidates, meshes, []
    else:
        print(f"    Merge threshold: {merge_overlap_ratio} (bbox overlap ratio)")
        accepted, accepted_meshes, merged = resolve_overlaps(
            candidates, meshes, merge_overlap_ratio, verbose=verbose
        )

    print(f"    Before: {len(candidates)}  →  After: {len(accepted)}  (merged {len(merged)})")

    if len(merged) > 0 and verbose:
        print(f"    Merged pairs:")
        for m in merged[:10]:
            print(f"      {m['source']} → {m['merged_with']}")
        if len(merged) > 10:
            print(f"      ... and {len(merged) - 10} more")

    # Tuning hint for overlap
    if len(merged) > len(accepted):
        print(f"    💡 More merged than accepted — consider raising merge_overlap_ratio (currently {merge_overlap_ratio})")
    if len(merged) == 0 and len(candidates) > 1:
        print(f"    💡 No merges occurred — if duplicates visible, try lowering merge_overlap_ratio (currently {merge_overlap_ratio})")

    # --- Final summary ---
    print(f"\n    --- Final Summary ---")
    print(f"    Input files:          {len(pc_files)}")
    print(f"    Load failures:        {skipped_load_failed}")
    print(f"    Too few points:       {skipped_too_small}")
    print(f"    Valid point clouds:   {len(point_clouds)}")
    print(f"    Skipped undersized:   {len(skipped_undersized)}")
    print(f"    Candidates:           {len(candidates)}")
    print(f"    Merged (overlap):     {len(merged)}")
    print(f"    ✓ Final placed:       {len(accepted)}")

    if use_icp and accepted:
        fitnesses = [c.icp_fitness for c in accepted if c.icp_fitness > 0]
        if fitnesses:
            print(f"\n    --- ICP Fitness Distribution ---")
            print(f"    Min: {min(fitnesses):.4f}  Median: {np.median(fitnesses):.4f}  Max: {max(fitnesses):.4f}")
            low_fitness = sum(1 for f in fitnesses if f < icp_fitness_threshold)
            if low_fitness > 0:
                print(f"    Below threshold ({icp_fitness_threshold}): {low_fitness}/{len(fitnesses)}")

    if verbose and accepted:
        print(f"\n    --- Placed Objects ---")
        print(f"    {'Source':<35} {'Angle':>7} {'Fitness':>8} {'Class':<12}")
        print(f"    {'-'*65}")
        for c in accepted:
            fitness_str = f"{c.icp_fitness:.3f}" if use_icp else "—"
            print(f"    {c.source:<35} {c.rotation_angle:>6.0f}° {fitness_str:>8} {c.classification:<12}")

    stats = {
        'mode': 'template',
        'input_files': len(pc_files),
        'load_failures': skipped_load_failed,
        'too_few_points': skipped_too_small,
        'loaded': len(point_clouds),
        'classifications': {'normal': n_normal, 'oversized': n_over, 'undersized': n_under},
        'skipped_undersized': len(skipped_undersized),
        'candidates': len(candidates),
        'accepted': len(accepted),
        'merged': len(merged),
        'use_icp': use_icp,
        'icp_fallbacks': icp_fallback_count if use_icp else 0,
    }

    return accepted, accepted_meshes, stats