#!/usr/bin/env python3
"""
Step 2: Extract Point Clouds from Masks

Projects 3D point cloud onto 2D masks to extract object points.
Uses patch-based depth band filtering to remove background noise (e.g., floor
points that leak through the mask along lines of sight).

Run 2b_filter_pointcloud.py separately for connected component filtering.
"""

import argparse
import gc
import json
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import torch

from path_resolver import PathResolver


# GL-to-OpenCV coordinate flip (constant, never changes)
_GL_TO_CV = torch.tensor([
    [1, 0, 0, 0], [0, -1, 0, 0],
    [0, 0, -1, 0], [0, 0, 0, 1]
], dtype=torch.float32)


# ==========================================
# COORDINATE TRANSFORMS
# ==========================================

def apply_dataparser_transform(points, dataparser_data):
    """Apply dataparser inverse transform + rotation."""
    transform_matrix = np.array(dataparser_data['transform'])
    scale = dataparser_data['scale']
    
    R = transform_matrix[:, :3]
    t = transform_matrix[:, 3]
    
    points_unscaled = points / scale
    points_untrans = points_unscaled - t
    points_orig = (R.T @ points_untrans.T).T
    
    rot_x_neg90 = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    points_final = (rot_x_neg90 @ points_orig.T).T
    
    return points_final.astype(np.float32)


def build_frame_lookup(camera_data):
    """Build frame name -> transform_matrix lookup for O(1) camera pose retrieval.

    Indexes by both stem ("frame_00001") and full filename ("frame_00001.jpg")
    to handle different mask folder naming conventions.
    """
    lookup = {}
    for frame in camera_data['frames']:
        fp = frame['file_path']
        matrix = np.array(frame['transform_matrix'], dtype=np.float32)
        lookup[Path(fp).stem] = matrix
        lookup[Path(fp).name] = matrix
    return lookup


def get_intrinsics(json_data, device):
    """Extract camera intrinsics from transforms.json.

    Supports two formats:
      - Top-level intrinsics (e.g. instant-ngp style)
      - Per-frame intrinsics (e.g. nerfstudio style) — uses first frame
    """
    # Try top-level keys first, fall back to first frame
    source = json_data if 'fl_x' in json_data else json_data['frames'][0]
    fl_x, fl_y = source['fl_x'], source['fl_y']
    cx, cy = source['cx'], source['cy']
    w, h = source['w'], source['h']
    
    K = torch.tensor([
        [fl_x, 0, cx],
        [0, fl_y, cy],
        [0, 0, 1]
    ], device=device, dtype=torch.float32)
    return K, w, h


# ==========================================
# PROJECTION
# ==========================================

def _project_to_frame(points_gpu, w2c, K, width, height):
    """Project all 3D points to 2D for a single frame in one GPU pass.

    Optimizations vs the chunked approach:
    - Uses addmm (fused R@p + t) — avoids creating (N, 4) homogeneous tensor
      (~1.4GB saved for 70M points)
    - Inlines intrinsics — avoids (3, 3) @ (3, M) matmul intermediate
    - Single GPU→CPU transfer instead of per-chunk transfers
    - w2c is precomputed by caller (not recomputed per chunk)

    Args:
        points_gpu: (N, 3) float32 tensor on GPU (persistent across frames).
        w2c: (4, 4) world-to-camera matrix (gl_to_cv @ inv(c2w)), on GPU.
        K: (3, 3) intrinsics matrix on GPU.
        width, height: image dimensions.

    Returns:
        u, v: pixel coords (numpy int64), z: depths (numpy float32),
        indices: into original point array (numpy int64).
        All None if no points project into the frame.
    """
    # Extract R (3x3) and t (3,) from w2c to avoid homogeneous coordinates.
    # This saves ~1.4GB GPU memory vs creating an (N, 4) tensor for 70M points.
    R = w2c[:3, :3]
    t = w2c[:3, 3]

    # Fused cam_xyz = points @ R.T + t — single BLAS kernel, no large intermediates.
    # addmm(bias, mat1, mat2) = bias + mat1 @ mat2
    cam_xyz = torch.addmm(t.unsqueeze(0), points_gpu, R.T)  # (N, 3)

    # Filter points behind camera (~50% eliminated)
    z_vals = cam_xyz[:, 2]
    valid_mask = z_vals > 0.001
    valid_idx = torch.where(valid_mask)[0]

    if len(valid_idx) == 0:
        del cam_xyz
        return None, None, None, None

    cam_valid = cam_xyz[valid_idx]
    del cam_xyz  # free (N, 3) tensor early

    # Inline perspective projection (avoids K @ cam.T intermediate tensor)
    z = cam_valid[:, 2]
    inv_z = 1.0 / z
    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[0, 2], K[1, 2]
    u = (cam_valid[:, 0] * fx * inv_z + cx).round().long()
    v = (cam_valid[:, 1] * fy * inv_z + cy).round().long()

    # Bounds check
    in_bounds = (u >= 0) & (u < width) & (v >= 0) & (v < height)

    # Single GPU→CPU transfer
    u_np = u[in_bounds].cpu().numpy()
    v_np = v[in_bounds].cpu().numpy()
    z_np = z[in_bounds].cpu().numpy().astype(np.float32)
    indices_np = valid_idx[in_bounds].cpu().numpy()

    if len(u_np) == 0:
        return None, None, None, None

    return u_np, v_np, z_np, indices_np


def project_points_chunk(points_chunk, c2w, K, width, height):
    """Project a chunk of 3D points to 2D."""
    device = points_chunk.device
    num_points = points_chunk.shape[0]
    
    gl_to_cv = torch.tensor([
        [1, 0, 0, 0], [0, -1, 0, 0],
        [0, 0, -1, 0], [0, 0, 0, 1]
    ], device=device, dtype=torch.float32)
    
    ones = torch.ones((num_points, 1), device=device)
    points_homo = torch.hstack((points_chunk, ones))
    
    w2c = gl_to_cv @ torch.linalg.inv(c2w)
    cam_points = (w2c @ points_homo.T).T
    
    z_vals = cam_points[:, 2]
    valid_z_mask = z_vals > 0.001
    valid_indices = torch.where(valid_z_mask)[0]
    
    if len(valid_indices) == 0:
        return None, None, None, None
    
    cam_xyz = cam_points[valid_indices, :3]
    pixel_homo = (K @ cam_xyz.T).T
    u = pixel_homo[:, 0] / pixel_homo[:, 2]
    v = pixel_homo[:, 1] / pixel_homo[:, 2]
    z = pixel_homo[:, 2]
    
    u_int = torch.round(u).long()
    v_int = torch.round(v).long()
    in_bounds = (u_int >= 0) & (u_int < width) & (v_int >= 0) & (v_int < height)
    
    return u_int[in_bounds], v_int[in_bounds], z[in_bounds], valid_indices[in_bounds]


# ==========================================
# VISIBILITY / DEPTH FILTERING
# ==========================================

def _compute_patch_depth_bounds(patch_id, z, in_mask, depth_band_sigma, min_points=5):
    """Vectorized per-patch median/MAD computation.
    
    Returns per-point (depth_lo, depth_hi) arrays based on each point's patch.
    Points in patches with too few in-mask samples get (-inf, +inf) bounds.
    
    Args:
        patch_id: flat patch index per point (int array, length N).
        z: depth per point (float array, length N).
        in_mask: boolean array (length N) — which points are inside the mask.
        depth_band_sigma: number of MADs for the band.
        min_points: minimum in-mask points per patch for reliable stats.
    
    Returns:
        depth_lo, depth_hi: float arrays (length N) with per-point bounds.
    """
    n = len(z)
    depth_lo = np.full(n, -np.inf, dtype=np.float32)
    depth_hi = np.full(n, np.inf, dtype=np.float32)
    
    # Work only with in-mask points to compute stats
    mask_indices = np.where(in_mask)[0]
    if len(mask_indices) == 0:
        return depth_lo, depth_hi
    
    mask_pids = patch_id[mask_indices]
    mask_z = z[mask_indices]
    
    # Sort by patch_id for group-based operations
    sort_order = np.argsort(mask_pids)
    sorted_pids = mask_pids[sort_order]
    sorted_z = mask_z[sort_order]
    
    # Find group boundaries
    unique_pids, group_starts, group_counts = np.unique(
        sorted_pids, return_index=True, return_counts=True
    )
    
    # Compute median and MAD per group (only groups with enough points)
    valid_groups = group_counts >= min_points
    
    # Pre-allocate lookup: patch_id -> (med, lo, hi)
    pid_to_lo = {}
    pid_to_hi = {}
    
    for i in range(len(unique_pids)):
        if not valid_groups[i]:
            continue
        start = group_starts[i]
        count = group_counts[i]
        group_z = sorted_z[start:start + count]
        
        med = np.median(group_z)
        mad = np.median(np.abs(group_z - med))
        mad = max(mad, 0.005)
        
        pid = unique_pids[i]
        pid_to_lo[pid] = med - depth_band_sigma * mad
        pid_to_hi[pid] = med + depth_band_sigma * mad
    
    if not pid_to_lo:
        return depth_lo, depth_hi
    
    # Build bounds arrays for all points whose patch has valid stats.
    # Use vectorized lookup via a dense array if patch IDs are reasonably bounded,
    # otherwise fall back to a mapped approach.
    valid_pid_arr = np.array(list(pid_to_lo.keys()))
    lo_arr = np.array([pid_to_lo[p] for p in valid_pid_arr], dtype=np.float32)
    hi_arr = np.array([pid_to_hi[p] for p in valid_pid_arr], dtype=np.float32)
    
    max_pid = patch_id.max()
    # Use dense LUT if memory is reasonable (< ~40MB for float32 pair)
    if max_pid < 5_000_000:
        lo_lut = np.full(max_pid + 1, -np.inf, dtype=np.float32)
        hi_lut = np.full(max_pid + 1, np.inf, dtype=np.float32)
        lo_lut[valid_pid_arr] = lo_arr
        hi_lut[valid_pid_arr] = hi_arr
        depth_lo = lo_lut[patch_id]
        depth_hi = hi_lut[patch_id]
    else:
        # Sparse fallback — only update points that belong to valid patches
        for pid, lo, hi in zip(valid_pid_arr, lo_arr, hi_arr):
            match = patch_id == pid
            depth_lo[match] = lo
            depth_hi[match] = hi
    
    return depth_lo, depth_hi


def compute_visibility(u, v, z, width, height, z_threshold=0.05,
                       mask=None, patch_size=32, depth_band_sigma=2.0):
    """Z-buffer visibility with optional patch-based depth band filtering.
    
    Two-stage filtering:
      1. Patch-based depth band: For each NxN pixel patch, compute the median
         depth of in-mask points and reject points whose depth deviates by more
         than depth_band_sigma * MAD. This removes far-away background points
         (e.g., floor behind a chair) that share the same 2D mask region.
      2. Per-pixel Z-buffer: Among surviving points, keep only those within
         z_threshold of the closest point at each pixel.
    
    Stage 1 is skipped when mask is None (falls back to Z-buffer only).
    
    Args:
        u, v: pixel coordinates of projected points (int arrays).
        z: depth values of projected points (float array).
        width, height: image dimensions in pixels.
        z_threshold: per-pixel Z-buffer tolerance in meters.
        mask: optional 2D binary mask (H, W). Enables depth band filtering.
        patch_size: NxN patch size in pixels for local depth estimation.
        depth_band_sigma: number of MADs around median depth to keep per patch.
    
    Returns:
        Boolean array (len == len(z)) — True for points that pass both filters.
    """
    # Stage 1: Patch-based depth band filtering
    if mask is not None:
        patch_u = u // patch_size
        patch_v = v // patch_size
        n_patches_x = (width + patch_size - 1) // patch_size
        patch_id = patch_v * n_patches_x + patch_u
        
        in_mask = mask[v, u] > 0
        
        depth_lo, depth_hi = _compute_patch_depth_bounds(
            patch_id, z, in_mask, depth_band_sigma
        )
        depth_band_mask = (z >= depth_lo) & (z <= depth_hi)
    else:
        depth_band_mask = np.ones(len(z), dtype=bool)
    
    # Stage 2: Per-pixel Z-buffer on surviving points
    filtered_idx = np.where(depth_band_mask)[0]
    
    depth_buffer = np.full((height, width), np.inf, dtype=np.float32)
    np.minimum.at(depth_buffer, (v[filtered_idx], u[filtered_idx]), z[filtered_idx])
    
    min_depths = depth_buffer[v, u]
    zbuffer_mask = z <= (min_depths + z_threshold)
    
    return depth_band_mask & zbuffer_mask


def _build_depth_buffer(u, v, z, width, height):
    """Build per-pixel minimum depth buffer.

    Uses np.minimum.at which is O(n) — faster than sort-based approaches
    for large point counts (10M+) because the depth buffer (~4MB for 1K×1K)
    fits in L3 cache, making the random-access scatter pattern efficient.

    Returns:
        (height, width) float32 array with min depth per pixel (inf where empty).
    """
    depth_buffer = np.full((height, width), np.inf, dtype=np.float32)
    if len(u) > 0:
        np.minimum.at(depth_buffer, (v, u), z.astype(np.float32))
    return depth_buffer


def compute_zbuffer_only(u, v, z, width, height, z_threshold=0.05):
    """Fast Z-buffer-only visibility (no depth band filtering).

    Used as a pre-filter before per-mask processing to eliminate clearly
    occluded points early, reducing the number of points each mask processes.
    """
    depth_buffer = _build_depth_buffer(u, v, z, width, height)
    min_depths = depth_buffer[v, u]
    return z <= (min_depths + z_threshold)


def _extract_mask_only(mask_folder, indices_global, u_global, v_global, n_points):
    """Pass 1: mask-membership check only. No depth band or z-buffer.

    For each mask in mask_folder, marks any projected point that falls inside
    the mask. Returns a boolean array over the full point cloud (size n_points).
    Used to accumulate per-frame view counts for multi-view consensus.

    Args:
        mask_folder: Path to folder with mask_*.png files.
        indices_global: indices into the full point cloud for projected points.
        u_global, v_global: pixel coords of projected points (after prefilter).
        n_points: total number of points in the full point cloud.

    Returns:
        frame_mask: bool array of length n_points (True = seen in any mask).
    """
    frame_mask = np.zeros(n_points, dtype=bool)
    for mask_file in sorted(Path(mask_folder).glob("mask_*.png")):
        mask = cv2.imread(str(mask_file), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            continue
        in_mask = mask[v_global, u_global] > 0
        if np.any(in_mask):
            frame_mask[indices_global[in_mask]] = True
    return frame_mask


def refine_points_from_masks(mask_folder, local_indices, u_global, v_global, z_global,
                              width, height, n_consensus,
                              use_depth_band=True, use_zbuffer=True,
                              z_threshold=0.05, depth_band_sigma=2.0, depth_patch_size=32):
    """Pass 2: apply configurable depth filters on consensus points only.

    Operates on the projected consensus point subset (not the full cloud).
    Both depth band and z-buffer are independently togglable.

    Args:
        mask_folder: Path to folder with mask_*.png files.
        local_indices: indices into consensus array (0..n_consensus-1) for
                       projected+prefiltered points.
        u_global, v_global: pixel coords of projected consensus points.
        z_global: depths of projected consensus points.
        width, height: image dimensions.
        n_consensus: total number of consensus points.
        use_depth_band: apply patch-based depth band filter.
        use_zbuffer: apply per-pixel z-buffer filter.
        z_threshold, depth_band_sigma, depth_patch_size: filter params.

    Returns:
        frame_mask_local: bool array of length n_consensus.
    """
    frame_mask = np.zeros(n_consensus, dtype=bool)
    mask_files = sorted(Path(mask_folder).glob("mask_*.png"))
    if not mask_files:
        return frame_mask

    # Pre-compute patch IDs once (reused across all masks in this frame)
    if use_depth_band:
        patch_u = u_global // depth_patch_size
        patch_v = v_global // depth_patch_size
        n_patches_x = (width + depth_patch_size - 1) // depth_patch_size
        patch_id = patch_v * n_patches_x + patch_u

    for mask_file in mask_files:
        mask = cv2.imread(str(mask_file), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            continue

        in_mask = mask[v_global, u_global] > 0
        if not np.any(in_mask):
            continue

        selected = in_mask.copy()
        depth_ok = None

        if use_depth_band:
            depth_lo, depth_hi = _compute_patch_depth_bounds(
                patch_id, z_global, in_mask, depth_band_sigma
            )
            depth_ok = (z_global >= depth_lo) & (z_global <= depth_hi)
            selected = selected & depth_ok

        if use_zbuffer:
            # Build z-buffer from depth-band-filtered points (or all if band disabled)
            zbuf_src = (np.where(depth_ok)[0]
                        if (use_depth_band and depth_ok is not None)
                        else np.arange(len(u_global)))
            depth_buffer = _build_depth_buffer(
                u_global[zbuf_src], v_global[zbuf_src], z_global[zbuf_src], width, height
            )
            zbuffer_ok = z_global <= (depth_buffer[v_global, u_global] + z_threshold)
            selected = selected & zbuffer_ok

        frame_mask[local_indices[selected]] = True

    return frame_mask


# ==========================================
# MASK EXTRACTION
# ==========================================

def extract_points_from_masks(mask_folder, indices_global, u_global, v_global, z_global,
                              width, height, output_folder, points_np, colors_np=None,
                              save_individual=False, z_threshold=0.05,
                              depth_band_sigma=2.0, depth_patch_size=32):
    """Extract points from masks with per-mask patch-based depth band filtering.
    
    Optimization: pre-computes patch IDs once and reuses across masks.
    
    Args:
        mask_folder: Path to folder containing mask_*.png files.
        indices_global: indices into points_np for all projected points.
        u_global, v_global: pixel coordinates of all projected points.
        z_global: depth values of all projected points.
        width, height: image dimensions.
        output_folder: where to save individual mask point clouds.
        points_np: full point cloud (N, 3).
        colors_np: full point cloud colors (N, 3) or None.
        save_individual: whether to save per-mask .ply files.
        z_threshold: Z-buffer tolerance in meters.
        depth_band_sigma: MADs for patch-based depth band.
        depth_patch_size: patch size in pixels.
    
    Returns:
        results: list of dicts with mask name and point count.
        frame_mask: boolean array (len == len(points_np)) for combined selection.
    """
    mask_folder = Path(mask_folder)
    mask_files = sorted(mask_folder.glob("mask_*.png"))
    
    if not mask_files:
        return [], np.zeros(len(points_np), dtype=bool)
    
    frame_mask = np.zeros(len(points_np), dtype=bool)
    results = []
    
    # Pre-compute patch IDs once (shared across all masks in this frame)
    patch_u = u_global // depth_patch_size
    patch_v = v_global // depth_patch_size
    n_patches_x = (width + depth_patch_size - 1) // depth_patch_size
    patch_id = patch_v * n_patches_x + patch_u
    
    for mask_file in mask_files:
        mask = cv2.imread(str(mask_file), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            continue
        
        # Check which projected points fall inside this mask
        in_mask = mask[v_global, u_global] > 0
        n_in_mask = np.count_nonzero(in_mask)
        if n_in_mask == 0:
            continue
        
        # Compute depth bounds from in-mask points
        depth_lo, depth_hi = _compute_patch_depth_bounds(
            patch_id, z_global, in_mask, depth_band_sigma
        )
        
        # Apply depth band filter
        depth_ok = (z_global >= depth_lo) & (z_global <= depth_hi)
        
        # Build Z-buffer from depth-band-filtered points only
        filtered_idx = np.where(depth_ok)[0]
        depth_buffer = _build_depth_buffer(
            u_global[filtered_idx], v_global[filtered_idx],
            z_global[filtered_idx], width, height
        )
        
        min_depths = depth_buffer[v_global, u_global]
        zbuffer_ok = z_global <= (min_depths + z_threshold)
        
        # Final selection: in mask AND depth band AND z-buffer
        selected = in_mask & depth_ok & zbuffer_ok
        point_indices = indices_global[selected]
        
        if len(point_indices) == 0:
            continue
        
        frame_mask[point_indices] = True
        
        if save_individual:
            pcd = o3d.geometry.PointCloud()
            pcd.points = o3d.utility.Vector3dVector(points_np[point_indices])
            if colors_np is not None:
                pcd.colors = o3d.utility.Vector3dVector(colors_np[point_indices])
            o3d.io.write_point_cloud(str(output_folder / f"{mask_file.stem}.ply"), pcd)
            results.append({'mask': mask_file.stem, 'count': len(point_indices)})
    
    return results, frame_mask


# ==========================================
# SINGLE FRAME PROCESSING
# ==========================================

def process_single_frame(mask_folder, frames_dir, points_transformed, points_np, colors_np,
                         camera_data, K, w, h, device, output_folder, save_individual,
                         z_threshold=0.05, depth_band_sigma=2.0, depth_patch_size=32):
    """Process a single frame's masks with depth-band-aware extraction.
    
    Steps:
      1. Find the camera pose for this frame.
      2. Project all 3D points into the camera (chunked for memory).
      3. Pre-filter with a loose Z-buffer pass to reduce point count.
      4. For each mask, run patch-based depth filtering + Z-buffer.
    """
    mask_folder_name = mask_folder.name
    
    # Find corresponding image and camera pose
    img_path = None
    for ext in ['.jpg', '.png', '.jpeg', '.JPG']:
        p = Path(frames_dir) / f"{mask_folder_name}{ext}"
        if p.exists():
            img_path = p
            break
    
    if img_path is None:
        return None, None
    
    c2w = None
    for frame in camera_data['frames']:
        if mask_folder_name in frame['file_path'] or img_path.name in frame['file_path']:
            c2w = torch.tensor(frame['transform_matrix'], device=device, dtype=torch.float32)
            break
    
    if c2w is None:
        return None, None
    
    # Chunked projection
    CHUNK_SIZE = 500000
    all_u, all_v, all_z, all_indices = [], [], [], []
    
    print(f"  Projecting {mask_folder_name}...", end=" ")
    
    for start in range(0, len(points_transformed), CHUNK_SIZE):
        end = min(start + CHUNK_SIZE, len(points_transformed))
        chunk = torch.from_numpy(points_transformed[start:end]).float().to(device)
        
        u, v, z, valid_idx = project_points_chunk(chunk, c2w, K, w, h)
        
        if u is not None:
            all_u.append(u.cpu().numpy())
            all_v.append(v.cpu().numpy())
            all_z.append(z.cpu().numpy())
            all_indices.append(valid_idx.cpu().numpy() + start)
        
        del chunk
    
    if not all_u:
        print("No points projected.")
        return None, None
    
    u_global = np.concatenate(all_u)
    v_global = np.concatenate(all_v)
    z_global = np.concatenate(all_z)
    indices_global = np.concatenate(all_indices)
    
    # Pre-filter: loose Z-buffer pass to discard clearly occluded points.
    # Uses a generous threshold (10x configured) to avoid discarding points
    # that the per-mask depth band filter would keep.
    prefilter_mask = compute_zbuffer_only(
        u_global, v_global, z_global, w, h,
        z_threshold=max(z_threshold * 10, 0.5)
    )
    u_global = u_global[prefilter_mask]
    v_global = v_global[prefilter_mask]
    z_global = z_global[prefilter_mask]
    indices_global = indices_global[prefilter_mask]
    
    print(f"{len(indices_global)} after prefilter.", end=" ")
    
    # Extract points from masks (depth band + Z-buffer per mask)
    output_folder = Path(output_folder) / mask_folder_name
    output_folder.mkdir(parents=True, exist_ok=True)
    
    results, frame_mask = extract_points_from_masks(
        mask_folder, indices_global, u_global, v_global, z_global,
        w, h, output_folder, points_np, colors_np, save_individual,
        z_threshold=z_threshold,
        depth_band_sigma=depth_band_sigma,
        depth_patch_size=depth_patch_size
    )
    
    extracted_count = int(np.sum(frame_mask)) if frame_mask is not None else 0
    print(f"{extracted_count} extracted.")
    
    # Save combined for this frame
    if np.any(frame_mask):
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(points_np[frame_mask])
        if colors_np is not None:
            pcd.colors = o3d.utility.Vector3dVector(colors_np[frame_mask])
        o3d.io.write_point_cloud(str(output_folder / "combined.ply"), pcd)
    
    return results, frame_mask


# ==========================================
# MAIN EXTRACTION
# ==========================================

def _lookup_c2w(frame_lookup, mask_folder_name, frames_dir):
    """Find camera-to-world matrix for a mask folder using the prebuilt lookup.

    Returns (c2w_np, img_path) or (None, None) if not found.
    """
    # Direct stem match (most common case)
    c2w_np = frame_lookup.get(mask_folder_name)
    if c2w_np is not None:
        # Verify image file exists
        for ext in ['.jpg', '.png', '.jpeg', '.JPG']:
            p = Path(frames_dir) / f"{mask_folder_name}{ext}"
            if p.exists():
                return c2w_np, p
        return c2w_np, None

    # Try with extension (e.g., mask folder named "frame_00001.jpg")
    for ext in ['.jpg', '.png', '.jpeg', '.JPG']:
        c2w_np = frame_lookup.get(f"{mask_folder_name}{ext}")
        if c2w_np is not None:
            p = Path(frames_dir) / f"{mask_folder_name}{ext}"
            return c2w_np, p if p.exists() else None

    return None, None


def run_extraction(resolver: PathResolver):
    """Run point cloud extraction for the active category.

    Two-pass approach:
      Pass 1: mask-only view counting across all frames → consensus_indices
      Pass 2: depth refinement on consensus points only (configurable)

    Pass 2 is skipped when both use_depth_band and use_zbuffer are False,
    in which case the consensus result is used directly as the output.
    """

    print("=" * 60)
    print("STEP 2: POINT CLOUD EXTRACTION")
    print("=" * 60)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Load point cloud
    print("\nLoading point cloud...")
    pcd = o3d.io.read_point_cloud(str(resolver.point_cloud_path))
    points_np = np.asarray(pcd.points).astype(np.float32)
    colors_np = np.asarray(pcd.colors).astype(np.float32) if pcd.has_colors() else None
    print(f"  Loaded {len(points_np):,} points")

    # Load transforms
    with open(resolver.dataparser_path) as f:
        dp_data = json.load(f)
    with open(resolver.transforms_path) as f:
        cam_data = json.load(f)

    # Transform coordinates
    points_transformed = apply_dataparser_transform(points_np, dp_data)
    K, w, h = get_intrinsics(cam_data, device)

    # Get extraction settings from config
    z_threshold = resolver.get('extraction', 'z_threshold', default=0.05)
    depth_band_sigma = resolver.get('extraction', 'depth_band_sigma', default=2.0)
    depth_patch_size = resolver.get('extraction', 'depth_patch_size', default=32)
    min_views = resolver.get('extraction', 'min_views', default=2)
    save_view_counts = resolver.get('extraction', 'save_view_counts', default=False)
    use_depth_band = resolver.get('extraction', 'use_depth_band', default=True)
    use_zbuffer = resolver.get('extraction', 'use_zbuffer', default=True)

    print(f"\n  Extraction settings:")
    print(f"    z_threshold:      {z_threshold}")
    print(f"    depth_band_sigma: {depth_band_sigma}")
    print(f"    depth_patch_size: {depth_patch_size}")
    print(f"    min_views:        {min_views}")
    print(f"    use_depth_band:   {use_depth_band}")
    print(f"    use_zbuffer:      {use_zbuffer}")

    # Precompute once (O(1) frame lookup, constant GL→CV matrix)
    frame_lookup = build_frame_lookup(cam_data)
    gl_to_cv = _GL_TO_CV.to(device)

    # Keep full point cloud on GPU for pass 1
    try:
        points_gpu = torch.from_numpy(points_transformed).float().to(device)
        use_gpu_fast_path = True
        print(f"  GPU fast path: enabled ({points_gpu.nelement() * 4 / 1e6:.0f} MB on device)")
    except RuntimeError:
        points_gpu = None
        use_gpu_fast_path = False
        print("  GPU fast path: disabled (insufficient GPU memory, using chunked fallback)")

    mask_folders = sorted([d for d in resolver.masks_dir.iterdir() if d.is_dir()])
    print(f"\nProcessing {len(mask_folders)} frames...")
    loose_z = max(z_threshold * 10, 0.5)

    # ===== PASS 1: Mask-only view counting (cached) =====
    print("\nPass 1: Mask-only view counting...")
    view_counts_cache = resolver.pointclouds_raw_dir / "CACHE_view_counts.npy"

    if view_counts_cache.exists():
        view_counts = np.load(str(view_counts_cache))
        print(f"  Loaded cached view_counts ({len(view_counts):,} points).")
        print(f"  Delete '{view_counts_cache.name}' to force recomputation.")
        # Free GPU memory — pass 1 is skipped
        if use_gpu_fast_path:
            del points_gpu
    else:
        view_counts = np.zeros(len(points_np), dtype=np.int32)

        for i, mf in enumerate(mask_folders):
            try:
                if use_gpu_fast_path:
                    c2w_np, _ = _lookup_c2w(frame_lookup, mf.name, resolver.frames_dir)
                    if c2w_np is None:
                        continue

                    c2w = torch.tensor(c2w_np, device=device, dtype=torch.float32)
                    w2c = gl_to_cv @ torch.linalg.inv(c2w)

                    print(f"  [P1] {mf.name}...", end=" ")

                    u_global, v_global, z_global, indices_global = _project_to_frame(
                        points_gpu, w2c, K, w, h
                    )

                    if u_global is None:
                        print("No points projected.")
                        continue

                    prefilter = compute_zbuffer_only(
                        u_global, v_global, z_global, w, h, z_threshold=loose_z
                    )
                    u_pf = u_global[prefilter]
                    v_pf = v_global[prefilter]
                    indices_pf = indices_global[prefilter]

                    print(f"{len(indices_pf)} projected.", end=" ")

                    frame_mask = _extract_mask_only(mf, indices_pf, u_pf, v_pf, len(points_np))
                    print(f"{int(np.sum(frame_mask))} in masks.")
                    view_counts += frame_mask.astype(np.int32)

                else:
                    # Chunked fallback (low GPU memory) — applies all filters in pass 1
                    _, frame_mask = process_single_frame(
                        mf, resolver.frames_dir, points_transformed, points_np, colors_np,
                        cam_data, K, w, h, device, resolver.pointclouds_raw_dir, False,
                        z_threshold=z_threshold,
                        depth_band_sigma=depth_band_sigma,
                        depth_patch_size=depth_patch_size
                    )
                    if frame_mask is not None:
                        view_counts += frame_mask.astype(np.int32)

            except Exception as e:
                print(f"Error processing {mf.name}: {e}")

            if i % 10 == 0:
                gc.collect()

        # Free full-cloud GPU tensor before pass 2
        if use_gpu_fast_path:
            del points_gpu

        # Save cache so subsequent runs with different min_views skip pass 1
        np.save(str(view_counts_cache), view_counts)
        print(f"  Saved view_counts cache: {view_counts_cache.name}")

    # Consensus threshold
    consensus_indices = np.where(view_counts >= min_views)[0]

    # Log view count statistics
    total_seen = int(np.sum(view_counts > 0))
    total_kept = len(consensus_indices)
    max_views = int(view_counts.max()) if total_seen > 0 else 0

    print(f"\n  Multi-view consensus (min_views={min_views}):")
    print(f"    Points seen in >= 1 view:  {total_seen:,}")
    print(f"    Points kept (>= {min_views} views): {total_kept:,}")
    if total_seen > 0:
        print(f"    Max views for any point:   {max_views}")
        print(f"    View count distribution:")
        for threshold in [1, 2, 3, 5, 10, 20]:
            if threshold <= max_views:
                count = int(np.sum(view_counts >= threshold))
                print(f"      >= {threshold:>2} views: {count:>10,} points")

    # Save diagnostic view count PLY (color-coded by view count)
    if save_view_counts and total_seen > 0:
        diag_mask = view_counts > 0
        diag_points = points_np[diag_mask]
        diag_counts = view_counts[diag_mask].astype(np.float32)

        max_c = max(diag_counts.max(), 1)
        normalized = diag_counts / max_c
        colors = np.zeros((len(diag_points), 3), dtype=np.float32)
        colors[:, 0] = normalized        # R increases with count
        colors[:, 2] = 1.0 - normalized  # B decreases with count

        diag_pcd = o3d.geometry.PointCloud()
        diag_pcd.points = o3d.utility.Vector3dVector(diag_points)
        diag_pcd.colors = o3d.utility.Vector3dVector(colors)
        diag_path = resolver.pointclouds_raw_dir / "DEBUG_view_counts.ply"
        o3d.io.write_point_cloud(str(diag_path), diag_pcd)
        print(f"  Saved diagnostic view counts: {diag_path}")

    # ===== PASS 2: Depth refinement on consensus points =====
    if not use_depth_band and not use_zbuffer:
        print("\nPass 2: Skipped (both depth filters disabled).")
        global_mask = np.zeros(len(points_np), dtype=bool)
        global_mask[consensus_indices] = True
    else:
        print(f"\nPass 2: Depth refinement on {len(consensus_indices):,} consensus points...")
        print(f"  use_depth_band={use_depth_band}, use_zbuffer={use_zbuffer}")

        try:
            consensus_gpu = torch.from_numpy(
                points_transformed[consensus_indices]
            ).float().to(device)
            use_gpu_pass2 = True
            print(f"  Consensus GPU: {consensus_gpu.nelement() * 4 / 1e6:.0f} MB")
        except RuntimeError:
            consensus_gpu = None
            use_gpu_pass2 = False
            print("  GPU unavailable for pass 2, using consensus-only result.")

        if use_gpu_pass2:
            final_mask_local = np.zeros(len(consensus_indices), dtype=bool)

            for i, mf in enumerate(mask_folders):
                try:
                    c2w_np, _ = _lookup_c2w(frame_lookup, mf.name, resolver.frames_dir)
                    if c2w_np is None:
                        continue

                    c2w = torch.tensor(c2w_np, device=device, dtype=torch.float32)
                    w2c = gl_to_cv @ torch.linalg.inv(c2w)

                    print(f"  [P2] {mf.name}...", end=" ")

                    u, v, z, local_idx = _project_to_frame(consensus_gpu, w2c, K, w, h)
                    if u is None:
                        print("No points projected.")
                        continue

                    pf = compute_zbuffer_only(u, v, z, w, h, z_threshold=loose_z)
                    u, v, z, local_idx = u[pf], v[pf], z[pf], local_idx[pf]

                    print(f"{len(local_idx)} projected.", end=" ")

                    frame_local = refine_points_from_masks(
                        mf, local_idx, u, v, z, w, h, len(consensus_indices),
                        use_depth_band=use_depth_band, use_zbuffer=use_zbuffer,
                        z_threshold=z_threshold, depth_band_sigma=depth_band_sigma,
                        depth_patch_size=depth_patch_size
                    )
                    print(f"{int(np.sum(frame_local))} selected.")
                    final_mask_local |= frame_local

                except Exception as e:
                    print(f"Error processing {mf.name} (pass 2): {e}")

                if i % 10 == 0:
                    gc.collect()

            del consensus_gpu

            global_mask = np.zeros(len(points_np), dtype=bool)
            global_mask[consensus_indices[final_mask_local]] = True
        else:
            global_mask = np.zeros(len(points_np), dtype=bool)
            global_mask[consensus_indices] = True

    # Save raw combined cloud
    raw_output = resolver.pointclouds_raw_dir / "FINAL_combined_all_images.ply"
    if np.any(global_mask):
        print("\nSaving raw combined point cloud...")
        raw_pcd = o3d.geometry.PointCloud()
        raw_pcd.points = o3d.utility.Vector3dVector(points_np[global_mask])
        if colors_np is not None:
            raw_pcd.colors = o3d.utility.Vector3dVector(colors_np[global_mask])
        o3d.io.write_point_cloud(str(raw_output), raw_pcd)
        print(f"  Saved: {raw_output}")
        final_count = int(np.sum(global_mask))
    else:
        final_count = 0

    print("\n" + "=" * 60)
    print(f"STEP 2 COMPLETE: {final_count:,} points extracted")
    print("Next: Run 2b_filter_pointcloud.py to filter components")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Step 2: Point Cloud Extraction")
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    args = parser.parse_args()
    
    resolver = PathResolver(args.config)
    run_extraction(resolver)


if __name__ == "__main__":
    main()