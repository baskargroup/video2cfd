#!/usr/bin/env python3
"""
Shared utilities for Step 4: STL placement.

Contains all common code used by both template-based and procedural placers:
- Data classes
- Point cloud I/O
- Geometry utilities
- ICP alignment
- Overlap resolution
- Visualization
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import open3d as o3d
import trimesh

# ============================================================================
# REPRODUCIBILITY
# ============================================================================

def set_deterministic_seed(seed: int = 42):
    """Set random seeds for reproducibility across all libraries."""
    np.random.seed(seed)
    try:
        o3d.utility.random.seed(seed)
    except AttributeError:
        pass  # Older Open3D versions don't expose this

# ============================================================================
# WATERTIGHTNESS CHECKING
# ============================================================================

def check_mesh_watertight(mesh: trimesh.Trimesh, label: str, indent: int = 4) -> dict:
    """
    Check and log watertightness details for a mesh.

    Checks:
      - is_watertight: no boundary edges + consistent winding
      - is_volume: is_watertight + positive enclosed volume
      - boundary edges: edges shared by only 1 face (holes)
      - non-manifold edges: edges shared by >2 faces (overlapping geometry)
      - degenerate faces: zero-area triangles

    Args:
        mesh: trimesh to check
        label: description for logging
        indent: spaces for indentation

    Returns:
        dict with check results
    """
    pad = " " * indent
    is_wt = mesh.is_watertight
    is_vol = mesh.is_volume

    # Count boundary (open) and non-manifold edges
    edges_sorted = np.sort(mesh.edges, axis=1)
    edge_keys = edges_sorted[:, 0].astype(np.int64) * (edges_sorted.max() + 1) + edges_sorted[:, 1]
    unique, counts = np.unique(edge_keys, return_counts=True)
    boundary_edges = int(np.sum(counts == 1))
    non_manifold_edges = int(np.sum(counts > 2))

    # Degenerate faces
    degenerate = int(mesh.degenerate_faces.sum()) if hasattr(mesh, 'degenerate_faces') else 0

    status = "WATERTIGHT" if is_wt else "NOT WATERTIGHT"

    print(f"{pad}[{status}] {label}")
    print(f"{pad}  Vertices: {len(mesh.vertices):,}  |  Faces: {len(mesh.faces):,}")
    print(f"{pad}  is_watertight: {is_wt}  |  is_volume: {is_vol}")
    if boundary_edges > 0:
        print(f"{pad}  Open edges: {boundary_edges}  <-- holes in the surface")
    if non_manifold_edges > 0:
        print(f"{pad}  Non-manifold edges (>2 faces): {non_manifold_edges}")
    if degenerate > 0:
        print(f"{pad}  Degenerate (zero-area) faces: {degenerate}")

    return {
        'label': label,
        'vertices': len(mesh.vertices),
        'faces': len(mesh.faces),
        'is_watertight': is_wt,
        'is_volume': is_vol,
        'boundary_edges': boundary_edges,
        'non_manifold_edges': non_manifold_edges,
        'degenerate_faces': degenerate,
    }


def print_watertight_report(checks: List[dict]):
    """Print a formatted watertightness report table."""
    print(f"\n{'='*60}")
    print(f"  WATERTIGHTNESS REPORT")
    print(f"{'='*60}")
    print(f"  {'Stage':<25} {'Label':<40} {'WT':>4} {'Open':>6}")
    print(f"  {'-'*77}")
    for c in checks:
        wt_str = "YES" if c['is_watertight'] else "NO"
        label = c['label'][:38] + '..' if len(c['label']) > 40 else c['label']
        stage = c.get('stage', '?')[:23] + '..' if len(c.get('stage', '?')) > 25 else c.get('stage', '?')
        print(f"  {stage:<25} {label:<40} {wt_str:>4} {c['boundary_edges']:>6}")
    n_pass = sum(1 for c in checks if c['is_watertight'])
    n_fail = len(checks) - n_pass
    print(f"  {'-'*77}")
    print(f"  Checks: {len(checks)}  |  Pass: {n_pass}  |  Fail: {n_fail}")


# ============================================================================
# MANIFOLD3D HELPERS
# ============================================================================

def manifold_to_trimesh(manifold_obj) -> trimesh.Trimesh:
    """Convert manifold3d.Manifold to trimesh.Trimesh."""
    mesh_result = manifold_obj.to_mesh()
    if isinstance(mesh_result, tuple):
        verts, faces = mesh_result
    else:
        verts = np.array(mesh_result.vert_properties)[:, :3]
        faces = np.array(mesh_result.tri_verts)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces)
    mesh.fix_normals()
    return mesh


def trimesh_to_manifold(mesh: trimesh.Trimesh):
    """Convert trimesh.Trimesh to manifold3d.Manifold."""
    import manifold3d
    verts = np.array(mesh.vertices, dtype=np.float64)
    faces = np.array(mesh.faces, dtype=np.int32)
    try:
        return manifold3d.Manifold.from_mesh(verts, faces)
    except (TypeError, AttributeError):
        m = manifold3d.Mesh(vert_properties=verts, tri_verts=faces)
        return manifold3d.Manifold(m)

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class PlacedObject:
    """Unified placement record for any category/mode."""
    category: str
    source: str
    position: List[float]
    classification: str
    footprint: float = 0.0
    point_count: int = 0
    rotation_angle: float = 0.0
    icp_fitness: float = 0.0
    mesh_bounds: Optional[Tuple[np.ndarray, np.ndarray]] = None
    mode: str = "template"  # "template" or "procedural"
    dimensions: Optional[Dict[str, float]] = None  # For procedural: width, depth, height


@dataclass
class MeshInfo:
    """Holds a loaded STL template and its computed metrics."""
    mesh: trimesh.Trimesh
    bounds: Dict[str, Any]
    is_watertight: bool
    scale: float
    rotation_matrix: np.ndarray
    scaled_size: np.ndarray = None
    scaled_footprint: float = 0.0
    # Distance (in original mesh units) from the bottom of the bounding box to
    # the centroid along Z.  Always >= 0.  Used by floor_plane z_mode to convert
    # a fitted floor Z into the correct centroid placement Z:
    #   position_z = floor_z + centroid_above_bottom * scale
    centroid_above_bottom: float = 0.0


# ============================================================================
# POINT CLOUD I/O
# ============================================================================

def load_point_cloud_txt(file_path: Path) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Load point cloud from txt file (X Y Z R G B format)."""
    try:
        data = np.loadtxt(file_path, comments='//', dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        xyz = data[:, :3]
        rgb = data[:, 3:6] if data.shape[1] >= 6 else None
        return xyz, rgb
    except Exception as e:
        print(f"    Error loading {file_path}: {e}")
        return None, None


def load_point_cloud_ply(file_path: Path) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Load point cloud from PLY file."""
    try:
        pcd = o3d.io.read_point_cloud(str(file_path))
        xyz = np.asarray(pcd.points, dtype=np.float32)
        rgb = np.asarray(pcd.colors, dtype=np.float32) if pcd.has_colors() else None
        return xyz, rgb
    except Exception as e:
        print(f"    Error loading {file_path}: {e}")
        return None, None


def load_point_cloud(file_path: Path) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Load point cloud from txt or ply file (auto-detect)."""
    if file_path.suffix.lower() == '.ply':
        return load_point_cloud_ply(file_path)
    else:
        return load_point_cloud_txt(file_path)


def find_point_cloud_files(directory: Path) -> List[Path]:
    """Find and sort all point cloud files in a directory."""
    pc_files = []
    for ext in ['*.txt', '*.ply']:
        pc_files.extend(directory.glob(ext))
    return sorted(pc_files)


# ============================================================================
# GEOMETRY UTILITIES
# ============================================================================

def get_bounds(points) -> Dict[str, Any]:
    """Get bounding box of points (numpy array or trimesh)."""
    if hasattr(points, 'bounds'):
        min_c, max_c = points.bounds[0], points.bounds[1]
    else:
        min_c, max_c = points.min(axis=0), points.max(axis=0)

    size = max_c - min_c
    return {
        'min': min_c, 'max': max_c, 'size': size,
        'center': (min_c + max_c) / 2,
        'footprint': size[0] * size[1],
        'volume': size[0] * size[1] * size[2]
    }


def create_rotation_matrix(axis: np.ndarray, angle_degrees: float) -> np.ndarray:
    """Create 3x3 rotation matrix around an arbitrary axis."""
    angle = np.radians(angle_degrees)
    axis = axis / np.linalg.norm(axis)
    c, s = np.cos(angle), np.sin(angle)
    t = 1 - c
    x, y, z = axis
    return np.array([
        [t*x*x + c,    t*x*y - s*z,  t*x*z + s*y],
        [t*x*y + s*z,  t*y*y + c,    t*y*z - s*x],
        [t*x*z - s*y,  t*y*z + s*x,  t*z*z + c]
    ], dtype=np.float64)


def create_rotation_matrix_z(angle_radians: float) -> np.ndarray:
    """Create 3x3 rotation matrix around Z axis."""
    c, s = np.cos(angle_radians), np.sin(angle_radians)
    return np.array([
        [c, -s, 0],
        [s,  c, 0],
        [0,  0, 1]
    ], dtype=np.float64)


def apply_z_rotation(mesh: trimesh.Trimesh, angle_radians: float, center: np.ndarray):
    """Apply rotation around Z-axis at a specific center point (in-place)."""
    mesh.vertices -= center
    rotation_matrix = create_rotation_matrix_z(angle_radians)
    mesh.vertices = mesh.vertices @ rotation_matrix.T
    mesh.vertices += center


def estimate_vertical_axis(all_point_clouds):
    """Estimate vertical axis from floor plane of multiple point clouds.

    Args:
        all_point_clouds: list of (path, xyz) tuples
    """
    floor_points = []
    for _, xyz in all_point_clouds:
        z_thresh = np.percentile(xyz[:, 2], 10)
        bottom = xyz[xyz[:, 2] <= z_thresh]
        if len(bottom) > 10:
            floor_points.append(bottom.mean(axis=0))

    if len(floor_points) < 3:
        return np.array([0, 0, 1], dtype=np.float32)

    floor_points = np.array(floor_points)
    _, _, vh = np.linalg.svd(floor_points - floor_points.mean(axis=0))
    normal = vh[-1]
    return (normal if normal[2] >= 0 else -normal).astype(np.float32)


# ============================================================================
# ICP ALIGNMENT
# ============================================================================

def _estimate_point_spacing(points: np.ndarray, k: int = 5) -> float:
    """Estimate average nearest-neighbor spacing in a point cloud."""
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    kd_tree = o3d.geometry.KDTreeFlann(pcd)
    
    n_check = min(200, len(points))
    step = max(1, len(points) // n_check)
    dists = []
    for i in range(0, len(points), step):
        _, _, d2 = kd_tree.search_knn_vector_3d(points[i], k + 1)
        dists.extend([np.sqrt(d) for d in d2[1:]])
    return float(np.median(dists)) if dists else 0.01


def _prepare_target_pcd(target_points: np.ndarray,
                        normal_radius: float) -> o3d.geometry.PointCloud:
    """Build an Open3D point cloud with normals (done once, reused across angles)."""
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(target_points)
    pcd.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=normal_radius, max_nn=30)
    )
    pcd.orient_normals_towards_camera_location(camera_location=np.array([0.0, 0.0, 100.0]))
    return pcd


def run_icp_alignment(source_points: np.ndarray,
                      target_pcd: o3d.geometry.PointCloud,
                      threshold: float = 0.02,
                      max_iterations: int = 50,
                      normal_radius: float = 0.1) -> Tuple[np.ndarray, float]:
    """
    Run point-to-plane ICP to align source points to a pre-built target.

    Returns:
        transformation: 4x4 transformation matrix
        fitness: ICP fitness score (0-1)
    """
    source_pcd = o3d.geometry.PointCloud()
    source_pcd.points = o3d.utility.Vector3dVector(source_points)
    source_pcd.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=normal_radius, max_nn=30)
    )
    source_pcd.orient_normals_towards_camera_location(camera_location=np.array([0.0, 0.0, 100.0]))

    result = o3d.pipelines.registration.registration_icp(
        source_pcd,
        target_pcd,
        threshold,
        np.eye(4),
        o3d.pipelines.registration.TransformationEstimationPointToPlane(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=max_iterations)
    )

    return result.transformation, result.fitness


def find_best_orientation_icp(mesh: trimesh.Trimesh,
                               target_points: np.ndarray,
                               icp_threshold: float = 0.02,
                               icp_iterations: int = 50,
                               base_rotation_degrees: float = 0.0,
                               verbose: bool = False) -> Tuple[float, float]:
    """
    Find the best Z-rotation angle for a mesh by trying candidate angles
    and picking the one with the highest ICP fitness.

    Two-phase approach:
      Phase 1: Coarse search with 8 candidates (45 deg steps)
      Phase 2: Fine search +/-20 deg around best coarse angle (5 deg steps)

    Returns:
        best_angle: Best rotation angle in degrees
        best_fitness: Best ICP fitness score
    """
    # Deterministic surface sampling
    n_samples = min(5000, max(3000, len(target_points) * 3))
    np.random.seed(42)
    try:
        mesh_points = mesh.sample(n_samples)
    except Exception:
        mesh_points = mesh.vertices.copy()

    # Center both point sets
    mesh_centered = mesh_points - mesh_points.mean(axis=0)
    target_centered = target_points - target_points.mean(axis=0)

    # Compute spacing and adaptive threshold ONCE
    target_spacing = _estimate_point_spacing(target_centered)
    adaptive_threshold = max(icp_threshold, target_spacing * 3)
    normal_radius = max(target_spacing * 5, adaptive_threshold)

    # Build target point cloud with normals ONCE (reused for all angles)
    target_pcd = _prepare_target_pcd(target_centered, normal_radius)

    # ---- Phase 1: Coarse search (8 candidates, 45 deg steps) ----
    coarse_angles = [base_rotation_degrees + a for a in range(0, 360, 45)]

    best_fitness = -1
    best_angle = base_rotation_degrees

    if verbose:
        print(f"\n        Phase 1: {len(coarse_angles)} coarse angles (threshold={adaptive_threshold:.4f})")

    for angle in coarse_angles:
        rotation = create_rotation_matrix_z(np.radians(angle))
        rotated = mesh_centered @ rotation.T

        try:
            _, fitness = run_icp_alignment(
                rotated, target_pcd,
                threshold=adaptive_threshold,
                max_iterations=icp_iterations,
                normal_radius=normal_radius
            )

            if verbose:
                print(f"          {angle:6.1f} deg: fitness={fitness:.4f}")

            if fitness > best_fitness:
                best_fitness = fitness
                best_angle = angle

        except Exception as e:
            if verbose:
                print(f"          {angle:6.1f} deg: FAILED - {e}")

    # ---- Phase 2: Fine search (+/-20 deg around best, 5 deg steps) ----
    fine_angles = [best_angle + offset for offset in range(-20, 25, 5) if offset != 0]

    if verbose:
        print(f"        Phase 2: refining around {best_angle:.1f} deg")

    for angle in fine_angles:
        rotation = create_rotation_matrix_z(np.radians(angle))
        rotated = mesh_centered @ rotation.T

        try:
            _, fitness = run_icp_alignment(
                rotated, target_pcd,
                threshold=adaptive_threshold,
                max_iterations=icp_iterations,
                normal_radius=normal_radius
            )

            if verbose:
                print(f"          {angle:6.1f} deg: fitness={fitness:.4f}")

            if fitness > best_fitness:
                best_fitness = fitness
                best_angle = angle

        except Exception as e:
            if verbose:
                print(f"          {angle:6.1f} deg: FAILED - {e}")

    if verbose:
        print(f"        -> Best: {best_angle:.1f} deg (fitness={best_fitness:.4f})")

    return best_angle, best_fitness


# ============================================================================
# OVERLAP RESOLUTION
# ============================================================================

def compute_bbox_overlap_ratio(bounds1: Tuple[np.ndarray, np.ndarray],
                                bounds2: Tuple[np.ndarray, np.ndarray]) -> float:
    """Compute overlap ratio: intersection_volume / smaller_volume."""
    min1, max1 = bounds1
    min2, max2 = bounds2

    inter_min = np.maximum(min1, min2)
    inter_max = np.minimum(max1, max2)

    if np.any(inter_max <= inter_min):
        return 0.0

    inter_volume = np.prod(inter_max - inter_min)
    vol1 = np.prod(max1 - min1)
    vol2 = np.prod(max2 - min2)

    smaller_volume = min(vol1, vol2)
    if smaller_volume <= 0:
        return 0.0

    return inter_volume / smaller_volume


def resolve_overlaps(candidates: List[PlacedObject],
                     meshes: List[trimesh.Trimesh],
                     merge_overlap_ratio: float,
                     verbose: bool = False
                     ) -> Tuple[List[PlacedObject], List[trimesh.Trimesh], List[dict]]:
    """
    Sort candidates by quality and remove overlapping placements.

    Returns:
        accepted_candidates, accepted_meshes, merged_list
    """
    if merge_overlap_ratio <= 0 or not candidates:
        return candidates, meshes, []

    combined = sorted(
        zip(candidates, meshes),
        key=lambda x: (-x[0].point_count, -x[0].footprint)
    )

    accepted_idx = []
    merged = []

    for idx, (cand, _) in enumerate(combined):
        if not accepted_idx:
            accepted_idx.append(idx)
            continue

        should_merge = False
        if cand.mesh_bounds is not None:
            for acc_idx in accepted_idx:
                acc_cand = combined[acc_idx][0]
                if acc_cand.mesh_bounds is not None:
                    overlap = compute_bbox_overlap_ratio(cand.mesh_bounds, acc_cand.mesh_bounds)
                    if overlap >= merge_overlap_ratio:
                        should_merge = True
                        merged.append({'source': cand.source, 'merged_with': acc_cand.source})
                        if verbose:
                            print(f"    Merge: {cand.source} -> {acc_cand.source} (overlap={overlap:.2f})")
                        break

        if not should_merge:
            accepted_idx.append(idx)

    accepted_candidates = [combined[i][0] for i in accepted_idx]
    accepted_meshes = [combined[i][1] for i in accepted_idx]

    return accepted_candidates, accepted_meshes, merged


# ============================================================================
# BOOLEAN UNION
# ============================================================================

def boolean_union_meshes(meshes: List[trimesh.Trimesh], verbose: bool = True) -> trimesh.Trimesh:
    """Boolean union of watertight meshes via manifold3d. Falls back to concatenation."""
    if len(meshes) == 0:
        return None
    if len(meshes) == 1:
        return meshes[0]

    try:
        manifolds = [trimesh_to_manifold(m) for m in meshes if m.is_watertight]

        if verbose:
            print(f"    Boolean union: {len(manifolds)} watertight of {len(meshes)} total")

        if manifolds:
            result = manifolds[0]
            for m in manifolds[1:]:
                result = result + m
            return manifold_to_trimesh(result)

        return trimesh.util.concatenate(meshes)

    except ImportError:
        if verbose:
            print(f"    manifold3d not available, using concatenation...")
        return trimesh.util.concatenate(meshes)


# ============================================================================
# Z POSITIONING MODES
# ============================================================================

def compute_object_zs(bounds_list: List[Dict[str, Any]],
                      z_mode: str,
                      mesh_info: 'MeshInfo',
                      z_offset: float = 0.0,
                      verbose: bool = True) -> List[float]:
    """
    Compute a per-object placement Z for the template placer.

    Three modes
    -----------
    global_median
        All objects share the same Z: the median of each cloud's bounding-box
        centre Z.  The mesh *centroid* is placed at that Z (current default).
        Best for flat-floor rooms.

    per_object
        Each object's placement Z is its own bounding-box centre Z.  Simple
        but can be noisy when point-cloud extents vary.

    floor_plane
        A least-squares plane is fitted through the per-cloud floor estimates
        (cx, cy, min_z).  For each object the plane is evaluated at its XY
        centre to give a smooth, consistent floor Z.  The placement Z is then
        adjusted so the *bottom* of the scaled STL sits on that floor:
            position_z = floor_z_fitted + mesh_info.centroid_above_bottom * mesh_info.scale
        Best for auditorium / tiered-seating rooms.
        Falls back to global_median when fewer than 3 clouds are available.

    Returns
    -------
    List of per-object Z values (one per entry in bounds_list), already
    including z_offset.  Pass directly as the Z component of each placement
    position.
    """
    n = len(bounds_list)
    if n == 0:
        return []

    if z_mode == 'per_object':
        zs = [b['center'][2] + z_offset for b in bounds_list]
        if verbose:
            print(f"    Z mode: per_object  (range {min(zs):.3f} – {max(zs):.3f} m)")
        return zs

    if z_mode == 'floor_plane' and n >= 3:
        # Fit plane z = a*x + b*y + c through (cx, cy, min_z) for each cloud
        pts = np.array([[b['center'][0], b['center'][1], b['min'][2]]
                        for b in bounds_list], dtype=np.float64)
        A = np.column_stack([pts[:, 0], pts[:, 1], np.ones(n)])
        b_vec = pts[:, 2]
        coeffs, _, _, _ = np.linalg.lstsq(A, b_vec, rcond=None)
        a, b_coef, c = coeffs

        # Evaluate plane at each XY centre → smooth floor Z per object
        floor_zs = a * pts[:, 0] + b_coef * pts[:, 1] + c

        # Residuals: how well the plane explains the actual min Z values
        residuals = pts[:, 2] - floor_zs
        rmse = float(np.sqrt(np.mean(residuals ** 2)))

        # Adjust from floor Z to centroid Z so orient_and_place_mesh places
        # the bottom of the STL on the fitted floor surface.
        centroid_lift = mesh_info.centroid_above_bottom * mesh_info.scale
        zs = [float(fz) + centroid_lift + z_offset for fz in floor_zs]

        if verbose:
            print(f"    Z mode: floor_plane")
            print(f"      Plane: z = {a:.4f}·x + {b_coef:.4f}·y + {c:.4f}")
            print(f"      Floor Z range: {floor_zs.min():.3f} – {floor_zs.max():.3f} m")
            print(f"      Fit RMSE: {rmse:.4f} m  (lower = more planar floor)")
            print(f"      Centroid lift (scaled): {centroid_lift:.4f} m")
        return zs

    # Default / fallback: global_median (also used for floor_plane with < 3 clouds)
    if z_mode == 'floor_plane' and n < 3:
        print(f"    Z mode: floor_plane → fallback to global_median (only {n} clouds)")
    center_zs = [b['center'][2] for b in bounds_list]
    global_z = float(np.median(center_zs)) + z_offset
    if verbose:
        print(f"    Z mode: global_median  z={global_z:.4f} m")
    return [global_z] * n


# ============================================================================
# STL LOADING
# ============================================================================

def load_stl_mesh(stl_path: Path) -> Optional[trimesh.Trimesh]:
    """Load an STL file, handling Scene containers."""
    if not stl_path.exists():
        print(f"    ERROR: STL not found: {stl_path}")
        return None

    mesh = trimesh.load(str(stl_path))

    if not isinstance(mesh, trimesh.Trimesh):
        if isinstance(mesh, trimesh.Scene):
            geometries = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
            if geometries:
                mesh = trimesh.util.concatenate(geometries) if len(geometries) > 1 else geometries[0]
            else:
                print(f"    ERROR: No geometry in STL: {stl_path}")
                return None
        else:
            print(f"    ERROR: Unexpected type from STL: {type(mesh)}")
            return None

    return mesh


def load_trimesh(path) -> Optional[trimesh.Trimesh]:
    """Load a mesh file, unwrapping Scene containers. Returns None if not found."""
    path = Path(path)
    if not path.exists():
        return None
    return load_stl_mesh(path)


def load_and_validate_stl(stl_path: Path, category_name: str,
                           reference_size: np.ndarray,
                           scale_adjustment: float,
                           rotation_degrees: float,
                           vertical_axis: np.ndarray) -> Optional[MeshInfo]:
    """Load STL template, compute scale and rotation matrix."""
    mesh = load_stl_mesh(stl_path)
    if mesh is None:
        return None

    is_watertight = mesh.is_watertight
    print(f"    {category_name}: {'Watertight' if is_watertight else 'NOT watertight'}")

    bounds = get_bounds(mesh)
    scale = np.cbrt(np.prod(reference_size / bounds['size'])) * scale_adjustment
    rotation_matrix = create_rotation_matrix(vertical_axis, rotation_degrees)

    scaled_size = bounds['size'] * scale
    scaled_footprint = scaled_size[0] * scaled_size[1]

    # How far (in original mesh units) is the centroid above the bounding-box floor?
    centroid_above_bottom = float(mesh.centroid[2] - bounds['min'][2])

    print(f"    Original STL size: {bounds['size'][0]:.3f} x {bounds['size'][1]:.3f} x {bounds['size'][2]:.3f}")
    print(f"    Scaled STL size:   {scaled_size[0]:.3f} x {scaled_size[1]:.3f} x {scaled_size[2]:.3f}")
    print(f"    Scaled STL footprint: {scaled_footprint:.4f} m2")
    print(f"    Centroid above bottom: {centroid_above_bottom:.4f} m (original units)")

    return MeshInfo(mesh, bounds, is_watertight, scale, rotation_matrix,
                    scaled_size, scaled_footprint, centroid_above_bottom)


# ============================================================================
# VISUALIZATION
# ============================================================================

def visualize_placements(point_clouds, meshes, window_name="STL Placement"):
    """
    Visualize point clouds and meshes using Open3D.

    Args:
        point_clouds: list of (name_or_path, xyz, bounds_or_rgb) or (xyz, rgb) tuples
        meshes: list of trimesh.Trimesh
        window_name: window title
    """
    geometries = []

    mesh_colors = [
        [0.3, 0.5, 1.0],
        [0.8, 0.2, 0.2],
        [0.2, 0.8, 0.2],
        [0.8, 0.8, 0.2],
        [0.8, 0.2, 0.8],
        [0.2, 0.8, 0.8],
    ]

    for item in point_clouds:
        # Support both (name, xyz, bounds) and (xyz, rgb) tuples
        if len(item) == 3:
            _, xyz, _ = item
        else:
            xyz, _ = item
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(xyz)
        pcd.paint_uniform_color([1.0, 0.3, 0.3])
        geometries.append(pcd)

    for i, mesh in enumerate(meshes):
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(mesh.vertices)
        o3d_mesh.triangles = o3d.utility.Vector3iVector(mesh.faces)
        o3d_mesh.compute_vertex_normals()
        o3d_mesh.paint_uniform_color(mesh_colors[i % len(mesh_colors)])
        geometries.append(o3d_mesh)

    geometries.append(o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.2))
    o3d.visualization.draw_geometries(geometries, window_name=window_name, width=1280, height=720)


# ============================================================================
# RESULT SAVING
# ============================================================================

class NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

def save_results(output_dir: Path,
                 category: str,
                 meshes: List[trimesh.Trimesh],
                 candidates: List[PlacedObject],
                 stats: dict,
                 merged: List[dict],
                 save_individual: bool = True):
    """Save combined STL, individual STLs, and placement log."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if not meshes:
        print(f"    No meshes to save for {category}")
        return

    # Save individual meshes
    if save_individual:
        individual_dir = output_dir / "individual"
        individual_dir.mkdir(parents=True, exist_ok=True)
        for i, (mesh, cand) in enumerate(zip(meshes, candidates)):
            individual_path = individual_dir / f"{category}_{i+1:03d}.stl"
            mesh.export(str(individual_path))
        print(f"  Saved: {len(meshes)} individual STLs in {individual_dir}")

    # Save combined mesh
    combined_mesh = trimesh.util.concatenate(meshes)
    combined_path = output_dir / f"{category}_combined.stl"
    combined_mesh.export(str(combined_path))
    print(f"  Saved: {combined_path}")

    # Save log
    log = {
        'category': category,
        'mode': candidates[0].mode if candidates else 'unknown',
        'stats': stats,
        'placements': [
            {
                'source': c.source,
                'position': [float(v) for v in c.position],
                'rotation_degrees': float(c.rotation_angle),
                'icp_fitness': float(c.icp_fitness),
                'classification': c.classification,
                'point_count': int(c.point_count),
                'footprint': float(c.footprint),
                'mode': c.mode,
                **(({'dimensions': {k: float(v) for k, v in c.dimensions.items()}} if c.dimensions else {}))
            }
            for c in candidates
        ],
        'merged': merged,
    }
    log_path = output_dir / "scene_log.json"
    with open(log_path, 'w') as f:
        json.dump(log, f, indent=2, cls=NumpyEncoder)
    print(f"  Saved: {log_path}")


# ============================================================================
# INPUT DIRECTORY RESOLUTION
# ============================================================================

def find_input_dir(resolver, category: str) -> Optional[Path]:
    """Find the best available input directory for a category (healed > filtered > raw)."""
    for subdir in ['merged_groups', 'filtered', 'raw']:
        if subdir == 'merged_groups':
            test_dir = resolver.get_healed_dir(category, subdir)
        else:
            test_dir = resolver.get_extracted_dir(category, subdir)
        if test_dir.exists() and list(test_dir.glob("*.txt")) + list(test_dir.glob("*.ply")):
            return test_dir
    return None