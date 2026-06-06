#!/usr/bin/env python3
"""
Procedural STL placement.

Generates meshes procedurally based on point cloud dimensions.
Supports:
- Fixed rotation from config (use_icp: false)
- PCA + ICP orientation (use_icp: true)
- Per-category procedural generators (currently: table only)
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import trimesh

from stl_utils import (
    PlacedObject,
    load_point_cloud, find_point_cloud_files, get_bounds,
    create_rotation_matrix_z, apply_z_rotation,
    run_icp_alignment, find_input_dir,
)


# ============================================================================
# PROCEDURAL GENERATOR REGISTRY
# ============================================================================
# Add new procedural generators here. Each must accept point cloud data +
# config and return a trimesh.Trimesh.

SUPPORTED_PROCEDURAL_CATEGORIES = {'table'}


def validate_procedural_support(category: str):
    """Raise an error if procedural mode is not supported for this category."""
    if category not in SUPPORTED_PROCEDURAL_CATEGORIES:
        raise NotImplementedError(
            f"Procedural mesh generation is not implemented for category '{category}'. "
            f"Supported categories: {SUPPORTED_PROCEDURAL_CATEGORIES}. "
            f"Use mode='template' for '{category}' instead."
        )


# ============================================================================
# PROCEDURAL TABLE GENERATION
# ============================================================================

def create_procedural_table(width: float, depth: float, height: float,
                            tabletop_thickness: float = 0.03,
                            leg_width: float = 0.05,
                            leg_inset: float = 0.02) -> trimesh.Trimesh:
    """
    Create a procedural table mesh centered at origin with bottom at Z=0.

    Args:
        width: Table width (X dimension)
        depth: Table depth (Y dimension)
        height: Total table height (Z dimension)
        tabletop_thickness: Thickness of the tabletop
        leg_width: Width of square legs
        leg_inset: How far legs are inset from edge

    Returns:
        Combined trimesh of the table
    """
    meshes = []

    # Tabletop at the top
    tabletop = trimesh.creation.box(extents=[width, depth, tabletop_thickness])
    tabletop_z = height - tabletop_thickness / 2
    tabletop.apply_translation([0, 0, tabletop_z])
    meshes.append(tabletop)

    # Legs from ground to bottom of tabletop
    leg_height = height - tabletop_thickness
    if leg_height > 0:
        leg_z = leg_height / 2
        leg_offset_x = max(0, width / 2 - leg_width / 2 - leg_inset)
        leg_offset_y = max(0, depth / 2 - leg_width / 2 - leg_inset)

        for x_sign, y_sign in [(1, 1), (-1, 1), (1, -1), (-1, -1)]:
            leg = trimesh.creation.box(extents=[leg_width, leg_width, leg_height])
            leg.apply_translation([x_sign * leg_offset_x, y_sign * leg_offset_y, leg_z])
            meshes.append(leg)

    return trimesh.util.concatenate(meshes)


# ============================================================================
# ORIENTATION: PCA
# ============================================================================

def estimate_orientation_pca(points: np.ndarray) -> Tuple[np.ndarray, float]:
    """
    Estimate primary orientation via PCA in the XY plane.

    Returns:
        rotation_matrix: 3x3 Z-rotation matrix
        angle_degrees: rotation angle around Z
    """
    centroid = points.mean(axis=0)
    centered = points - centroid
    xy_points = centered[:, :2]

    cov = np.cov(xy_points.T)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)
    idx = np.argsort(eigenvalues)[::-1]
    eigenvectors = eigenvectors[:, idx]

    principal_axis = eigenvectors[:, 0]
    angle = np.arctan2(principal_axis[1], principal_axis[0])
    rotation_matrix = create_rotation_matrix_z(angle)

    return rotation_matrix, np.degrees(angle)


def get_oriented_dimensions(points: np.ndarray,
                            rotation_matrix: np.ndarray) -> Tuple[float, float]:
    """
    Get table dimensions after rotating to align with principal axis.

    Returns:
        width (along principal axis), depth (perpendicular)
    """
    centroid = points.mean(axis=0)
    centered = points - centroid
    aligned = centered @ rotation_matrix
    aligned_size = aligned.max(axis=0) - aligned.min(axis=0)
    return aligned_size[0], aligned_size[1]


# ============================================================================
# ORIENTATION: ICP REFINEMENT (Z-ROTATION ONLY)
# ============================================================================

def refine_orientation_icp(source_mesh: trimesh.Trimesh,
                           target_points: np.ndarray,
                           icp_threshold: float = 0.02,
                           icp_iterations: int = 50
                           ) -> Tuple[float, np.ndarray, float]:
    """
    Refine mesh orientation using ICP. Only extracts Z-rotation component.

    Returns:
        z_angle: Z-rotation adjustment (radians)
        translation_xy: XY translation adjustment
        fitness: ICP fitness score
    """
    n_samples = min(5000, len(target_points) * 2)
    source_points = source_mesh.sample(n_samples)

    transform, fitness = run_icp_alignment(
        source_points, target_points,
        threshold=icp_threshold,
        max_iterations=icp_iterations
    )

    rotation_matrix = transform[:3, :3]
    z_angle = np.arctan2(rotation_matrix[1, 0], rotation_matrix[0, 0])
    translation_xy = transform[:2, 3]

    return z_angle, translation_xy, fitness


# ============================================================================
# SINGLE TABLE PROCESSING
# ============================================================================

def process_single_table(pc_path: Path,
                         xyz: np.ndarray,
                         placement_cfg: dict,
                         floor_z: Optional[float] = None,
                         verbose: bool = True
                         ) -> Tuple[Optional[trimesh.Trimesh], Optional[PlacedObject]]:
    """
    Process a single table point cloud → procedural mesh.

    Orientation:
      use_icp=False → fixed rotation_degrees from config
      use_icp=True  → PCA initial estimate + ICP refinement (Z-rotation only)
    """
    bounds = get_bounds(xyz)

    # Config
    use_icp = placement_cfg.get('use_icp', True)
    icp_threshold = placement_cfg.get('icp_threshold', 0.02)
    icp_iterations = placement_cfg.get('icp_iterations', 50)
    icp_fitness_threshold = placement_cfg.get('icp_fitness_threshold', 0.3)
    rotation_degrees = placement_cfg.get('rotation_degrees', 0.0)
    scale_adjustment = placement_cfg.get('scale_adjustment', 1.0)
    z_offset = placement_cfg.get('z_offset', 0.0)
    table_height = placement_cfg.get('table_height', 0.75)
    tabletop_thickness = placement_cfg.get('tabletop_thickness', 0.03)
    leg_width = placement_cfg.get('leg_width', 0.05)
    leg_inset = placement_cfg.get('leg_inset', 0.02)

    if verbose:
        print(f"      Bounding box: {bounds['size'][0]:.3f} x {bounds['size'][1]:.3f} x {bounds['size'][2]:.3f} m")
        print(f"      Points: {len(xyz):,}, Footprint: {bounds['footprint']:.4f} m²")

    if verbose:
        print(f"      Bounding box: {bounds['size'][0]:.3f} x {bounds['size'][1]:.3f} x {bounds['size'][2]:.3f} m")
        print(f"      Points: {len(xyz):,}, Footprint: {bounds['footprint']:.4f} m²")

    # --- Determine orientation and dimensions ---
    if use_icp:
        # PCA for initial orientation + dimensions
        pca_rotation, pca_angle = estimate_orientation_pca(xyz)
        table_width, table_depth = get_oriented_dimensions(xyz, pca_rotation)

        # Convention: width >= depth
        if table_depth > table_width:
            table_width, table_depth = table_depth, table_width
            pca_angle += 90
            pca_rotation = create_rotation_matrix_z(np.radians(pca_angle))

        orientation_angle = pca_angle
        if verbose:
            print(f"      PCA orientation: {pca_angle:.1f}°")
    else:
        # Fixed rotation from config; dimensions from axis-aligned bounding box
        table_width = bounds['size'][0]
        table_depth = bounds['size'][1]

        # Convention: width >= depth
        if table_depth > table_width:
            table_width, table_depth = table_depth, table_width

        orientation_angle = rotation_degrees
        if verbose:
            print(f"      Config orientation: {rotation_degrees:.1f}°")

    # Apply scale
    table_width *= scale_adjustment
    table_depth *= scale_adjustment

    if verbose:
        print(f"      Table dimensions: {table_width:.3f} x {table_depth:.3f} x {table_height:.3f} m")

    # --- Create mesh ---
    table_mesh = create_procedural_table(
        width=table_width, depth=table_depth, height=table_height,
        tabletop_thickness=tabletop_thickness,
        leg_width=leg_width, leg_inset=leg_inset
    )

    # Apply Z-rotation
    rotation_z = create_rotation_matrix_z(np.radians(orientation_angle))
    table_mesh.vertices = table_mesh.vertices @ rotation_z.T

    # Position: XY center, Z bottom
    # Use the caller-supplied floor_z so all tables share a consistent ground
    # plane. Fall back to this table's own min Z only when no global value is
    # available (e.g. single-table runs).
    anchor_z = floor_z if floor_z is not None else bounds['min'][2]
    table_position = np.array([
        bounds['center'][0],
        bounds['center'][1],
        anchor_z,
    ])
    table_mesh.vertices += table_position

    # --- ICP refinement ---
    icp_fitness = 0.0
    final_angle = orientation_angle

    if use_icp and len(xyz) >= 100:
        try:
            z_angle_adj, translation_xy, icp_fitness = refine_orientation_icp(
                table_mesh, xyz,
                icp_threshold=icp_threshold,
                icp_iterations=icp_iterations
            )

            if verbose:
                print(f"      ICP fitness: {icp_fitness:.3f}, Z-rotation adjustment: {np.degrees(z_angle_adj):.1f}°")

            if icp_fitness >= icp_fitness_threshold:
                mesh_center = np.array([
                    table_mesh.vertices[:, 0].mean(),
                    table_mesh.vertices[:, 1].mean(),
                    table_mesh.vertices[:, 2].mean()
                ])
                apply_z_rotation(table_mesh, z_angle_adj, mesh_center)
                final_angle += np.degrees(z_angle_adj)
                table_mesh.vertices[:, 0] += translation_xy[0]
                table_mesh.vertices[:, 1] += translation_xy[1]
                if verbose:
                    print(f"      Applied ICP refinement")
            else:
                if verbose:
                    print(f"      ICP fitness too low, keeping initial orientation")
        except Exception as e:
            if verbose:
                print(f"      ICP failed: {e}, keeping initial orientation")

    # Z offset
    if z_offset != 0:
        table_mesh.vertices[:, 2] += z_offset

    # Result
    final_position = [
        float(table_mesh.vertices[:, 0].mean()),
        float(table_mesh.vertices[:, 1].mean()),
        float(table_mesh.vertices[:, 2].min())
    ]

    placement = PlacedObject(
        category='table',
        source=pc_path.stem,
        position=final_position,
        classification='normal',
        footprint=bounds['footprint'],
        point_count=len(xyz),
        rotation_angle=float(final_angle),
        icp_fitness=float(icp_fitness),
        mode='procedural',
        dimensions={
            'width': float(table_width),
            'depth': float(table_depth),
            'height': float(table_height),
            'tabletop_thickness': float(tabletop_thickness),
            'leg_width': float(leg_width),
            'leg_inset': float(leg_inset),
        }
    )

    return table_mesh, placement


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run_procedural_placement(resolver, category: str,
                             vertical_axis: np.ndarray,
                             min_points: int = 50,
                             verbose: bool = True
                             ) -> Tuple[List[PlacedObject], List[trimesh.Trimesh], dict]:
    """
    Run procedural placement for a category.

    Args:
        resolver: PathResolver
        category: category name
        vertical_axis: estimated vertical axis (unused for procedural but kept for interface parity)
        min_points: min points per point cloud
        verbose: detailed output

    Returns:
        (accepted_candidates, meshes, stats_dict)
    """
    # Validate this category supports procedural mode
    validate_procedural_support(category)

    cat_config = resolver.get_category_config(category)
    placement_cfg = cat_config.get('placement', {})
    scene_cfg = cat_config.get('scene', {})

    # Merge min_points from scene config
    placement_cfg.setdefault('min_points', scene_cfg.get('min_points', min_points))

    use_icp = placement_cfg.get('use_icp', True)
    print(f"\n  [{category}] Procedural placement (use_icp={use_icp})")

    # Input directory
    input_dir = find_input_dir(resolver, category)
    if input_dir is None:
        print(f"    No input directory found")
        return [], [], {}
    print(f"    Input: {input_dir}")

    # Print config summary
    print(f"    Table height: {placement_cfg.get('table_height', 0.75):.3f} m")
    print(f"    Scale adjustment: {placement_cfg.get('scale_adjustment', 1.0):.2f}")
    if use_icp:
        print(f"    ICP threshold: {placement_cfg.get('icp_threshold', 0.02):.3f}")
        print(f"    ICP fitness threshold: {placement_cfg.get('icp_fitness_threshold', 0.3):.2f}")
    else:
        print(f"    Fixed rotation: {placement_cfg.get('rotation_degrees', 0.0):.1f}°")

    # Load point clouds
    pc_files = find_point_cloud_files(input_dir)
    if not pc_files:
        print(f"    No point cloud files found")
        return [], [], {}
    print(f"    Found {len(pc_files)} point cloud files")

    # Diagnostic: show all point clouds with their footprints before filtering
    min_footprint = placement_cfg.get('min_footprint', 0.1)
    min_pts = placement_cfg.get('min_points', min_points)
    print(f"    Filters: min_footprint={min_footprint:.4f} m², min_points={min_pts}")
    print(f"    {'File':<40} {'Points':>8} {'Footprint':>10} {'Status':<15}")
    print(f"    {'-'*75}")

    skipped_points = 0
    skipped_footprint = 0
    skipped_load = 0

    # --- Load all point clouds once, track skip reasons, collect valid ones ---
    # Valid clouds are processed; floor_z is derived from them so all tables
    # share a consistent ground plane even if one cloud missed leg/floor points.
    valid_clouds = []  # list of (pc_path, xyz, bounds)
    for pc_path in pc_files:
        xyz, _ = load_point_cloud(pc_path)
        if xyz is None:
            print(f"    {pc_path.stem:<40} {'—':>8} {'—':>10} {'LOAD FAILED':<15}")
            skipped_load += 1
            continue

        bounds = get_bounds(xyz)
        n_pts = len(xyz)
        fp = bounds['footprint']

        if n_pts < min_pts:
            print(f"    {pc_path.stem:<40} {n_pts:>8,} {fp:>10.4f} {'SKIP (points)':<15}")
            skipped_points += 1
            continue

        if fp < min_footprint:
            print(f"    {pc_path.stem:<40} {n_pts:>8,} {fp:>10.4f} {'SKIP (footprint)':<15}")
            skipped_footprint += 1
            continue

        valid_clouds.append((pc_path, xyz, bounds))

    # Compute a single global floor Z from all valid clouds (10th-pct of min Z).
    # Using a percentile rather than the absolute min guards against stray
    # low-Z outlier points in one cloud pulling the floor down too far.
    if valid_clouds:
        all_min_zs = [b['min'][2] for _, _, b in valid_clouds]
        floor_z = float(np.percentile(all_min_zs, 10))
        print(f"    Global floor Z: {floor_z:.4f} m  (10th-pct of per-cloud min Z)")
    else:
        floor_z = None

    # Process each valid cloud
    all_meshes = []
    all_placements = []

    for pc_path, xyz, bounds in valid_clouds:
        n_pts = len(xyz)
        fp = bounds['footprint']
        print(f"    {pc_path.stem:<40} {n_pts:>8,} {fp:>10.4f} {'PROCESSING':<15}")

        # Dispatch to the right procedural generator
        if category == 'table':
            mesh, placement = process_single_table(
                pc_path, xyz, placement_cfg, floor_z=floor_z, verbose=verbose
            )
        else:
            raise NotImplementedError(f"No procedural generator for '{category}'")

        if mesh is not None and placement is not None:
            all_meshes.append(mesh)
            all_placements.append(placement)
            if verbose and placement.dimensions:
                d = placement.dimensions
                print(f"      ✓ Generated: {d['width']:.2f} x {d['depth']:.2f} x {d['height']:.2f} m")

    # Summary
    print(f"\n    --- Summary ---")
    print(f"    Total files:         {len(pc_files)}")
    print(f"    Load failures:       {skipped_load}")
    print(f"    Skipped (points):    {skipped_points}  (< {min_pts})")
    print(f"    Skipped (footprint): {skipped_footprint}  (< {min_footprint:.4f} m²)")
    print(f"    Tables generated:    {len(all_meshes)}")

    # Stats
    stats = {
        'mode': 'procedural',
        'loaded': len(pc_files),
        'skipped_load': skipped_load,
        'skipped_points': skipped_points,
        'skipped_footprint': skipped_footprint,
        'accepted': len(all_meshes),
        'use_icp': use_icp,
    }

    return all_placements, all_meshes, stats