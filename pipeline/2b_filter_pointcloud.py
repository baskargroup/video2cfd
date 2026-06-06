#!/usr/bin/env python3
"""
Step 2b: Filter Point Cloud using Connected Component Analysis

Standalone filtering step that can be run multiple times with different parameters.
Supports both octree-based (CloudCompare-like) and DBSCAN methods.
"""

import argparse
import json
from pathlib import Path
from collections import defaultdict

import numpy as np
import open3d as o3d

from path_resolver import PathResolver


# ==========================================
# OCTREE-BASED CONNECTED COMPONENTS
# (CloudCompare-like approach)
# ==========================================

def find_connected_components_octree(
    points: np.ndarray, 
    octree_level: int,
    random_colors: bool = False
) -> tuple:
    """
    Find connected components using octree/voxel-based approach.
    
    This mimics CloudCompare's behavior:
    - Points in the same voxel cell are connected
    - Points in adjacent cells (26-connectivity) are connected
    
    Args:
        points: Nx3 point coordinates
        octree_level: Subdivision level (higher = finer voxels)
        random_colors: If True, return random colors per component
    
    Returns:
        labels: N-length array of component labels
        voxel_size: The computed voxel size
    """
    n_points = len(points)
    
    # Compute voxel size from octree level
    # CloudCompare computes octree based on the point cloud bounding box
    min_bound = points.min(axis=0)
    max_bound = points.max(axis=0)
    bbox_size = max_bound - min_bound
    
    # Use the maximum dimension for uniform voxels
    max_dim = bbox_size.max()
    
    # Add small padding to avoid boundary issues (like CloudCompare does)
    padding = max_dim * 0.001
    max_dim_padded = max_dim + 2 * padding
    
    num_divisions = 2 ** octree_level
    voxel_size = max_dim_padded / num_divisions
    
    print(f"    Bounding box: {bbox_size}")
    print(f"    Max dimension: {max_dim:.6f}")
    print(f"    Voxel size: {voxel_size:.6f} (level {octree_level}, {num_divisions} divisions)")
    
    # Compute voxel indices for each point
    # Offset by padding to center the point cloud
    adjusted_min = min_bound - padding
    voxel_indices = np.floor((points - adjusted_min) / voxel_size).astype(np.int64)
    
    # Create voxel keys and map to points
    print(f"    Building voxel map...")
    voxel_to_points = defaultdict(list)
    
    for i in range(n_points):
        key = tuple(voxel_indices[i])
        voxel_to_points[key].append(i)
    
    n_occupied = len(voxel_to_points)
    print(f"    Occupied voxels: {n_occupied:,}")
    
    # Union-Find with path compression and union by rank
    parent = np.arange(n_points, dtype=np.int64)
    rank = np.zeros(n_points, dtype=np.int32)
    
    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression
        while parent[x] != root:
            next_x = parent[x]
            parent[x] = root
            x = next_x
        return root
    
    def union(x, y):
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        # Union by rank
        if rank[rx] < rank[ry]:
            rx, ry = ry, rx
        parent[ry] = rx
        if rank[rx] == rank[ry]:
            rank[rx] += 1
    
    # Connect points within same voxel
    print(f"    Connecting points within voxels...")
    for point_list in voxel_to_points.values():
        if len(point_list) > 1:
            first = point_list[0]
            for other in point_list[1:]:
                union(first, other)
    
    # Connect adjacent voxels (26-connectivity)
    print(f"    Connecting adjacent voxels (26-connectivity)...")
    occupied_set = set(voxel_to_points.keys())
    
    # All 26 neighbors (we check all to ensure symmetry)
    neighbor_offsets = []
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            for dz in [-1, 0, 1]:
                if dx == 0 and dy == 0 and dz == 0:
                    continue
                # Only check "forward" neighbors to avoid duplicate unions
                if (dx, dy, dz) > (0, 0, 0) or (dx == 0 and dy == 0 and dz > 0) or (dx == 0 and dy > 0):
                    neighbor_offsets.append((dx, dy, dz))
    
    # Actually, simpler: just check 13 forward neighbors
    neighbor_offsets = [
        (1, 0, 0), (0, 1, 0), (0, 0, 1),  # Face neighbors (forward only)
        (1, 1, 0), (1, -1, 0),  # Edge neighbors in XY
        (1, 0, 1), (1, 0, -1),  # Edge neighbors in XZ
        (0, 1, 1), (0, 1, -1),  # Edge neighbors in YZ
        (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1)  # Corner neighbors
    ]
    
    for voxel_key, point_list in voxel_to_points.items():
        vx, vy, vz = voxel_key
        rep_point = point_list[0]
        
        for dx, dy, dz in neighbor_offsets:
            neighbor_key = (vx + dx, vy + dy, vz + dz)
            if neighbor_key in occupied_set:
                neighbor_rep = voxel_to_points[neighbor_key][0]
                union(rep_point, neighbor_rep)
    
    # Extract final labels with path compression
    print(f"    Finalizing labels...")
    labels = np.array([find(i) for i in range(n_points)], dtype=np.int64)
    
    # Relabel to consecutive integers starting from 0
    unique_roots, inverse = np.unique(labels, return_inverse=True)
    labels = inverse
    
    n_components = len(unique_roots)
    print(f"    Found {n_components:,} connected components")
    
    return labels, voxel_size


def find_connected_components_dbscan(
    points: np.ndarray,
    eps: float = None,
    octree_level: int = None
) -> tuple:
    """
    Find connected components using DBSCAN.
    
    Args:
        points: Nx3 point coordinates
        eps: Neighborhood radius. If None, computed from octree_level
        octree_level: Used to compute eps if eps is None
    
    Returns:
        labels: N-length array of component labels
        eps: The eps value used
    """
    if eps is None:
        min_bound = points.min(axis=0)
        max_bound = points.max(axis=0)
        bbox_size = (max_bound - min_bound).max()
        eps = bbox_size / (2 ** octree_level)
    
    print(f"    Using DBSCAN with eps={eps:.6f}")
    
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    
    labels = np.array(pcd.cluster_dbscan(eps=eps, min_points=1, print_progress=True))
    
    # Handle noise points (label -1): assign each to its own component
    noise_mask = labels == -1
    if np.any(noise_mask):
        max_label = labels.max()
        noise_indices = np.where(noise_mask)[0]
        for i, idx in enumerate(noise_indices):
            labels[idx] = max_label + 1 + i
    
    n_components = len(np.unique(labels))
    print(f"    Found {n_components:,} connected components")
    
    return labels, eps


def filter_by_component_size(
    points: np.ndarray,
    colors: np.ndarray,
    labels: np.ndarray,
    min_points: int
) -> tuple:
    """Filter components by minimum point count."""
    
    # Count points per component using bincount
    counts = np.bincount(labels)
    
    # Create lookup for valid labels
    valid_labels_mask = counts >= min_points
    
    # Build mask for points
    valid_mask = valid_labels_mask[labels]
    
    # Filter
    filtered_points = points[valid_mask]
    filtered_colors = colors[valid_mask] if colors is not None else None
    filtered_labels = labels[valid_mask]
    
    # Relabel to consecutive integers
    unique_labels, inverse = np.unique(filtered_labels, return_inverse=True)
    filtered_labels = inverse
    
    kept = len(unique_labels)
    removed = len(np.unique(labels)) - kept
    
    return filtered_points, filtered_colors, filtered_labels, kept, removed, counts


def save_components_as_txt(
    points: np.ndarray,
    colors: np.ndarray,
    labels: np.ndarray,
    output_dir: Path
):
    """Save each component as a separate .txt file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Clear existing component files
    for old_file in output_dir.glob("component_*.txt"):
        old_file.unlink()
    
    unique_labels = np.unique(labels)
    
    for label in unique_labels:
        mask = (labels == label)
        comp_points = points[mask]
        comp_colors = colors[mask] if colors is not None else np.ones_like(comp_points) * 0.5
        
        # Ensure colors are in 0-1 range
        if comp_colors.max() > 1.0:
            comp_colors = comp_colors / 255.0
        
        data = np.hstack((comp_points, comp_colors))
        txt_path = output_dir / f"component_{label:06d}.txt"
        
        with open(txt_path, 'w') as f:
            f.write("//X Y Z R G B\n")
            np.savetxt(f, data, fmt='%.6f')
    
    print(f"    Exported {len(unique_labels)} components as .txt files")


def save_debug_colored_ply(
    points: np.ndarray,
    labels: np.ndarray,
    output_path: Path
):
    """Save point cloud with random colors per component for debugging."""
    n_components = len(np.unique(labels))
    
    # Generate random colors for each component
    np.random.seed(42)
    component_colors = np.random.rand(n_components, 3)
    
    # Assign colors
    colors = component_colors[labels]
    
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    pcd.colors = o3d.utility.Vector3dVector(colors)
    o3d.io.write_point_cloud(str(output_path), pcd)
    print(f"    Debug colored PLY: {output_path}")


# ==========================================
# MAIN
# ==========================================

def run_filtering(
    resolver: PathResolver, 
    octree_level: int = None, 
    min_points: int = None, 
    input_ply: str = None,
    method: str = None,
    eps: float = None,
    debug: bool = False
):
    """Run connected component filtering."""
    
    print("=" * 60)
    print("STEP 2b: POINT CLOUD FILTERING")
    print("=" * 60)
    
    # Get parameters (command line overrides config)
    if octree_level is None:
        octree_level = resolver.get('filtering', 'octree_level', default=10)
    if min_points is None:
        min_points = resolver.get('filtering', 'min_points_per_component', default=100)
    if method is None:
        method = resolver.get('filtering', 'method', default='octree')
    
    print(f"\nParameters:")
    print(f"  Method: {method}")
    print(f"  Octree level: {octree_level}")
    print(f"  Min points per component: {min_points}")
    if eps:
        print(f"  Eps override: {eps}")
    
    # Load input point cloud
    if input_ply:
        input_path = Path(input_ply)
    else:
        input_path = resolver.pointclouds_raw_dir / "FINAL_combined_all_images.ply"
    
    print(f"\nLoading: {input_path}")
    
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        print("Run step 2 (extraction) first.")
        return
    
    pcd = o3d.io.read_point_cloud(str(input_path))
    points = np.asarray(pcd.points).astype(np.float64)  # Use float64 for precision
    colors = np.asarray(pcd.colors).astype(np.float32) if pcd.has_colors() else None
    
    print(f"  Loaded {len(points):,} points")
    
    # Run connected components
    print(f"\nFinding connected components ({method} method)...")
    
    if method == 'dbscan':
        labels, used_eps = find_connected_components_dbscan(
            points, eps=eps, octree_level=octree_level
        )
    else:  # octree (default)
        labels, voxel_size = find_connected_components_octree(points, octree_level)
    
    n_components = len(np.unique(labels))
    
    # Save debug visualization before filtering
    output_dir = resolver.pointclouds_filtered_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if debug:
        debug_path = output_dir / "DEBUG_all_components_colored.ply"
        save_debug_colored_ply(points, labels, debug_path)
    
    # Filter by size
    print(f"\nFiltering components with < {min_points} points...")
    (filtered_points, filtered_colors, filtered_labels, 
     kept, removed, counts) = filter_by_component_size(
        points, colors, labels, min_points
    )
    
    print(f"    Kept: {kept:,} components ({len(filtered_points):,} points)")
    print(f"    Removed: {removed:,} components")
    
    # Show top components
    sorted_counts = sorted(enumerate(counts), key=lambda x: x[1], reverse=True)
    print(f"\n    Top 10 component sizes:")
    for i, (label, count) in enumerate(sorted_counts[:10]):
        status = "✓" if count >= min_points else "✗"
        print(f"      {status} Component {label}: {count:,} points")
    
    # Save filtered PLY
    filtered_ply = output_dir / "FINAL_combined_filtered.ply"
    filtered_pcd = o3d.geometry.PointCloud()
    filtered_pcd.points = o3d.utility.Vector3dVector(filtered_points)
    if filtered_colors is not None:
        filtered_pcd.colors = o3d.utility.Vector3dVector(filtered_colors)
    o3d.io.write_point_cloud(str(filtered_ply), filtered_pcd)
    print(f"\nSaved: {filtered_ply}")
    
    # Save debug visualization after filtering
    if debug:
        debug_filtered_path = output_dir / "DEBUG_filtered_components_colored.ply"
        save_debug_colored_ply(filtered_points, filtered_labels, debug_filtered_path)
    
    # Save individual components
    print("\nExporting individual components...")
    save_components_as_txt(filtered_points, filtered_colors, filtered_labels, output_dir)
    
    # Save stats
    top_counts = sorted(counts, reverse=True)[:50]
    stats = {
        'input_points': len(points),
        'output_points': len(filtered_points),
        'method': method,
        'octree_level': octree_level,
        'min_points_per_component': min_points,
        'total_components_found': n_components,
        'components_kept': kept,
        'components_removed': removed,
        'top_component_sizes': [int(c) for c in top_counts]
    }
    
    if method == 'octree':
        stats['voxel_size'] = float(voxel_size)
    elif method == 'dbscan':
        stats['eps'] = float(used_eps)
    
    stats_path = output_dir / "filtering_stats.json"
    with open(stats_path, 'w') as f:
        json.dump(stats, f, indent=2)
    print(f"Saved: {stats_path}")
    
    print("\n" + "=" * 60)
    print(f"FILTERING COMPLETE: {len(filtered_points):,} points in {kept} components")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Step 2b: Filter Point Cloud using Connected Components",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use config defaults (octree method)
  python 2b_filter_pointcloud.py --config ../configs/default.yaml
  
  # Override parameters
  python 2b_filter_pointcloud.py --config ../configs/default.yaml --octree-level 10 --min-points 500
  
  # Use DBSCAN method instead
  python 2b_filter_pointcloud.py --config ../configs/default.yaml --method dbscan --octree-level 10
  
  # Use DBSCAN with explicit eps
  python 2b_filter_pointcloud.py --config ../configs/default.yaml --method dbscan --eps 0.01
  
  # Enable debug output (colored component visualization)
  python 2b_filter_pointcloud.py --config ../configs/default.yaml --debug
  
  # Use custom input file
  python 2b_filter_pointcloud.py --config ../configs/default.yaml --input my_cloud.ply
"""
    )
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    parser.add_argument("--octree-level", type=int, help="Octree level (6-12, higher=finer)")
    parser.add_argument("--min-points", type=int, help="Min points to keep component")
    parser.add_argument("--input", type=str, help="Input PLY file (default: raw extraction output)")
    parser.add_argument("--method", choices=['octree', 'dbscan'], help="CC method: 'octree' or 'dbscan'")
    parser.add_argument("--eps", type=float, help="DBSCAN eps (neighborhood radius)")
    parser.add_argument("--debug", action="store_true", help="Save debug colored PLY files")
    
    args = parser.parse_args()
    
    resolver = PathResolver(args.config)
    run_filtering(
        resolver,
        octree_level=args.octree_level,
        min_points=args.min_points,
        input_ply=args.input,
        method=args.method,
        eps=args.eps,
        debug=args.debug
    )


if __name__ == "__main__":
    main()