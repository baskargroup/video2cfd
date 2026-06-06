#!/usr/bin/env python3
"""
Master pipeline runner for Video2STL.

Runs the complete pipeline for each enabled category, or specific steps.
"""

import argparse
import sys
from pathlib import Path

from path_resolver import PathResolver


def run_step_1_segment(resolver: PathResolver, category: str, **kwargs):
    """Run segmentation for a category."""
    import os
    import torch
    import numpy as np
    from PIL import Image
    
    # Import SAM3 components
    import sam3
    from sam3.model_builder import build_sam3_image_model
    from sam3.model.sam3_image_processor import Sam3Processor
    
    config = resolver.get_step_config(category, 'segmentation')
    frames_dir = resolver.get_frames_dir(category)
    output_dir = resolver.get_segmentation_dir(category)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    prompt = config.get('prompt', 'object')
    confidence = config.get('confidence_threshold', 0.5)
    
    print(f"\n{'='*60}")
    print(f"STEP 1: SEGMENTATION - {category}")
    print(f"  Prompt: '{prompt}'")
    print(f"  Confidence: {confidence}")
    print(f"  Frames: {frames_dir}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")
    
    if not frames_dir.exists():
        raise FileNotFoundError(f"Frames directory not found: {frames_dir}")
    
    # Setup device
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    
    # Build model
    print("\nLoading SAM3 model...")
    sam3_root = os.path.join(os.path.dirname(sam3.__file__), "..")
    bpe_path = os.path.join(sam3_root, "sam3", "assets", "bpe_simple_vocab_16e6.txt.gz")
    model = build_sam3_image_model(bpe_path=bpe_path)
    processor = Sam3Processor(model, confidence_threshold=confidence)
    
    # Import segment_image from 1_segment
    from importlib import import_module
    segment_module = import_module('1_segment')
    
    # Find images
    supported_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp'}
    image_files = [f for f in frames_dir.iterdir() 
                   if f.is_file() and f.suffix.lower() in supported_extensions]
    image_files = sorted(image_files)
    
    if not image_files:
        raise FileNotFoundError(f"No images found in {frames_dir}")
    
    print(f"\nFound {len(image_files)} images")
    print("-" * 60)
    
    # Process each image
    total_objects = 0
    for idx, image_path in enumerate(image_files, 1):
        print(f"\n[{idx}/{len(image_files)}]", end="")
        
        image_output_dir = output_dir / image_path.stem
        
        try:
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                results = segment_module.segment_image(
                    image_path=str(image_path),
                    text_prompt=prompt,
                    model=model,
                    processor=processor,
                    confidence_threshold=confidence,
                    output_dir=image_output_dir
                )
            total_objects += results['num_objects']
        except Exception as e:
            print(f"    ERROR: {e}")
    
    # Clean up GPU memory after segmentation
    del model, processor
    if device == "cuda":
        torch.cuda.empty_cache()
    
    print(f"\n{'='*60}")
    print(f"STEP 1 COMPLETE [{category}]: {total_objects} objects detected")
    print(f"{'='*60}")


def run_step_2_extract(resolver: PathResolver, category: str, **kwargs):
    """Run point cloud extraction for a category (2-pass approach).

    Pass 1: mask-only view counting → multi-view consensus
    Pass 2: depth refinement on consensus points only (use_depth_band / use_zbuffer)
    """
    import gc
    import json
    import torch
    import numpy as np
    import open3d as o3d

    from importlib import import_module
    extract_module = import_module('2_extract_points')

    config = resolver.get_step_config(category, 'extraction')
    masks_dir = resolver.get_segmentation_dir(category)
    frames_dir = resolver.get_frames_dir(category)
    output_dir = resolver.get_extracted_dir(category, "raw")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"STEP 2: EXTRACTION - {category}")
    print(f"  Masks: {masks_dir}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Load point cloud
    print("\nLoading point cloud...")
    pc_path = resolver.get_point_cloud_path(category)
    pcd = o3d.io.read_point_cloud(str(pc_path))
    points_np = np.asarray(pcd.points).astype(np.float32)
    colors_np = np.asarray(pcd.colors).astype(np.float32) if pcd.has_colors() else None
    print(f"  Loaded {len(points_np):,} points")

    # Load transforms
    dataparser_path = resolver.get_dataparser_path(category)
    transforms_path = resolver.get_transforms_path(category)

    with open(dataparser_path) as f:
        dp_data = json.load(f)
    with open(transforms_path) as f:
        cam_data = json.load(f)

    # Transform coordinates
    points_transformed = extract_module.apply_dataparser_transform(points_np, dp_data)
    K, w, h = extract_module.get_intrinsics(cam_data, device)

    # Config params
    z_threshold = config.get('z_threshold', 0.05)
    depth_band_sigma = config.get('depth_band_sigma', 2.0)
    depth_patch_size = config.get('depth_patch_size', 32)
    min_views = config.get('min_views', 2)
    save_view_counts = config.get('save_view_counts', False)
    use_depth_band = config.get('use_depth_band', True)
    use_zbuffer = config.get('use_zbuffer', True)

    print(f"\n  Extraction settings:")
    print(f"    z_threshold:      {z_threshold}")
    print(f"    depth_band_sigma: {depth_band_sigma}")
    print(f"    depth_patch_size: {depth_patch_size}")
    print(f"    min_views:        {min_views}")
    print(f"    use_depth_band:   {use_depth_band}")
    print(f"    use_zbuffer:      {use_zbuffer}")

    # Precompute
    frame_lookup = extract_module.build_frame_lookup(cam_data)
    gl_to_cv = extract_module._GL_TO_CV.to(device)
    mask_folders = sorted([d for d in masks_dir.iterdir() if d.is_dir()])
    print(f"\nProcessing {len(mask_folders)} frames...")
    loose_z = max(z_threshold * 10, 0.5)

    # GPU fast path: keep full point cloud on device for pass 1
    try:
        points_gpu = torch.from_numpy(points_transformed).float().to(device)
        use_gpu_fast_path = True
        print(f"  GPU fast path: enabled ({points_gpu.nelement() * 4 / 1e6:.0f} MB on device)")
    except RuntimeError:
        points_gpu = None
        use_gpu_fast_path = False
        print("  GPU fast path: disabled (insufficient GPU memory)")

    # ===== PASS 1: Mask-only view counting (cached) =====
    print("\nPass 1: Mask-only view counting...")
    view_counts_cache = output_dir / "CACHE_view_counts.npy"
    n_skipped = 0

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
                    c2w_np, _ = extract_module._lookup_c2w(frame_lookup, mf.name, frames_dir)
                    if c2w_np is None:
                        n_skipped += 1
                        if n_skipped == 1:
                            sample_paths = [f['file_path'] for f in cam_data.get('frames', [])[:5]]
                            print(f"\n  WARNING: Could not match frame '{mf.name}' to camera pose.")
                            print(f"    Frames dir: {frames_dir}")
                            print(f"    Sample paths in transforms.json: {sample_paths}")
                        continue

                    c2w = torch.tensor(c2w_np, device=device, dtype=torch.float32)
                    w2c = gl_to_cv @ torch.linalg.inv(c2w)

                    print(f"  [P1] {mf.name}...", end=" ")

                    u_global, v_global, z_global, indices_global = extract_module._project_to_frame(
                        points_gpu, w2c, K, w, h
                    )

                    if u_global is None:
                        print("No points projected.")
                        continue

                    prefilter = extract_module.compute_zbuffer_only(
                        u_global, v_global, z_global, w, h, z_threshold=loose_z
                    )
                    u_pf = u_global[prefilter]
                    v_pf = v_global[prefilter]
                    indices_pf = indices_global[prefilter]

                    print(f"{len(indices_pf)} projected.", end=" ")

                    frame_mask = extract_module._extract_mask_only(
                        mf, indices_pf, u_pf, v_pf, len(points_np)
                    )
                    print(f"{int(np.sum(frame_mask))} in masks.")
                    view_counts += frame_mask.astype(np.int32)

                else:
                    # Chunked fallback — applies all filters but still accumulates view counts
                    _, frame_mask = extract_module.process_single_frame(
                        mf, frames_dir, points_transformed, points_np, colors_np,
                        cam_data, K, w, h, device, output_dir, False,
                        z_threshold=z_threshold,
                        depth_band_sigma=depth_band_sigma,
                        depth_patch_size=depth_patch_size
                    )
                    if frame_mask is None:
                        n_skipped += 1
                    else:
                        view_counts += frame_mask.astype(np.int32)

            except Exception as e:
                print(f"Error processing {mf.name}: {e}")

            if i % 10 == 0:
                gc.collect()

        if use_gpu_fast_path:
            del points_gpu

        # Save cache so subsequent runs with different min_views skip pass 1
        np.save(str(view_counts_cache), view_counts)
        print(f"  Saved view_counts cache: {view_counts_cache.name}")

    if n_skipped > 0:
        print(f"\n  WARNING: {n_skipped}/{len(mask_folders)} frames skipped")

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

    # Save diagnostic view count PLY
    if save_view_counts and total_seen > 0:
        diag_mask = view_counts > 0
        diag_points = points_np[diag_mask]
        diag_counts = view_counts[diag_mask].astype(np.float32)
        max_c = max(diag_counts.max(), 1)
        normalized = diag_counts / max_c
        colors_diag = np.zeros((len(diag_points), 3), dtype=np.float32)
        colors_diag[:, 0] = normalized
        colors_diag[:, 2] = 1.0 - normalized
        diag_pcd = o3d.geometry.PointCloud()
        diag_pcd.points = o3d.utility.Vector3dVector(diag_points)
        diag_pcd.colors = o3d.utility.Vector3dVector(colors_diag)
        diag_path = output_dir / "DEBUG_view_counts.ply"
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
                    c2w_np, _ = extract_module._lookup_c2w(frame_lookup, mf.name, frames_dir)
                    if c2w_np is None:
                        continue

                    c2w = torch.tensor(c2w_np, device=device, dtype=torch.float32)
                    w2c = gl_to_cv @ torch.linalg.inv(c2w)

                    print(f"  [P2] {mf.name}...", end=" ")

                    u, v, z, local_idx = extract_module._project_to_frame(
                        consensus_gpu, w2c, K, w, h
                    )
                    if u is None:
                        print("No points projected.")
                        continue

                    pf = extract_module.compute_zbuffer_only(
                        u, v, z, w, h, z_threshold=loose_z
                    )
                    u, v, z, local_idx = u[pf], v[pf], z[pf], local_idx[pf]

                    print(f"{len(local_idx)} projected.", end=" ")

                    frame_local = extract_module.refine_points_from_masks(
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
    raw_output = output_dir / "FINAL_combined_all_images.ply"
    final_count = int(np.sum(global_mask))
    if final_count > 0:
        print("\nSaving raw combined point cloud...")
        raw_pcd = o3d.geometry.PointCloud()
        raw_pcd.points = o3d.utility.Vector3dVector(points_np[global_mask])
        if colors_np is not None:
            raw_pcd.colors = o3d.utility.Vector3dVector(colors_np[global_mask])
        o3d.io.write_point_cloud(str(raw_output), raw_pcd)
        print(f"  Saved: {raw_output}")

    print(f"\n{'='*60}")
    print(f"STEP 2 COMPLETE [{category}]: {final_count:,} points extracted")
    print(f"{'='*60}")


def run_step_2b_filter(resolver: PathResolver, category: str, **kwargs):
    """Run connected component filtering for a category."""
    import numpy as np
    import open3d as o3d
    
    from importlib import import_module
    filter_module = import_module('2b_filter_pointcloud')
    
    config = resolver.get_step_config(category, 'filtering')
    
    if not config.get('enabled', True):
        print(f"\n[{category}] Filtering disabled, skipping...")
        return
    
    input_dir = resolver.get_extracted_dir(category, "raw")
    output_dir = resolver.get_extracted_dir(category, "filtered")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Get parameters
    octree_level = config.get('octree_level', 10)
    min_points = config.get('min_points_per_component', 100)
    method = config.get('method', 'octree')
    eps = config.get('eps_override')
    debug = kwargs.get('verbose', False)
    
    print(f"\n{'='*60}")
    print(f"STEP 2b: FILTERING - {category}")
    print(f"  Method: {method}")
    print(f"  Octree level: {octree_level}")
    print(f"  Min points: {min_points}")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")
    
    # Load input point cloud
    input_path = input_dir / "FINAL_combined_all_images.ply"
    
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        print("Run step 2 (extraction) first.")
        return
    
    print(f"\nLoading: {input_path}")
    pcd = o3d.io.read_point_cloud(str(input_path))
    points = np.asarray(pcd.points).astype(np.float64)
    colors = np.asarray(pcd.colors).astype(np.float32) if pcd.has_colors() else None
    
    print(f"  Loaded {len(points):,} points")
    
    # Run connected components
    print(f"\nFinding connected components ({method} method)...")
    
    if method == 'dbscan':
        labels, used_eps = filter_module.find_connected_components_dbscan(
            points, eps=eps, octree_level=octree_level
        )
    else:  # octree
        labels, voxel_size = filter_module.find_connected_components_octree(points, octree_level)
    
    # Filter by size
    print(f"\nFiltering components with < {min_points} points...")
    (filtered_points, filtered_colors, filtered_labels, 
     kept, removed, counts) = filter_module.filter_by_component_size(
        points, colors, labels, min_points
    )
    
    print(f"  Kept: {kept:,} components ({len(filtered_points):,} points)")
    print(f"  Removed: {removed:,} components")
    
    # Save debug visualization
    if debug:
        debug_path = output_dir / "DEBUG_all_components_colored.ply"
        filter_module.save_debug_colored_ply(points, labels, debug_path)
    
    # Save individual components as TXT
    unique_labels = np.unique(filtered_labels)
    print(f"\nSaving {len(unique_labels)} components as TXT files...")
    
    for label in unique_labels:
        mask = filtered_labels == label
        comp_points = filtered_points[mask]
        comp_colors = filtered_colors[mask] if filtered_colors is not None else None
        
        output_path = output_dir / f"component_{label:04d}.txt"
        
        with open(output_path, 'w') as f:
            f.write("//X Y Z R G B\n")
            for i in range(len(comp_points)):
                x, y, z = comp_points[i]
                if comp_colors is not None:
                    r, g, b = (comp_colors[i] * 255).astype(int)
                else:
                    r, g, b = 128, 128, 128
                f.write(f"{x:.6f} {y:.6f} {z:.6f} {r} {g} {b}\n")
    
    # Save filtered combined PLY
    filtered_ply = output_dir / "FINAL_filtered.ply"
    out_pcd = o3d.geometry.PointCloud()
    out_pcd.points = o3d.utility.Vector3dVector(filtered_points)
    if filtered_colors is not None:
        out_pcd.colors = o3d.utility.Vector3dVector(filtered_colors)
    o3d.io.write_point_cloud(str(filtered_ply), out_pcd)
    
    print(f"\n{'='*60}")
    print(f"STEP 2b COMPLETE [{category}]: {kept} components, {len(filtered_points):,} points")
    print(f"{'='*60}")


def run_step_3_heal(resolver: PathResolver, category: str, **kwargs):
    """Run point cloud healing for a category."""
    import gc
    
    from importlib import import_module
    heal_module = import_module('3_heal_pointclouds')
    
    config = resolver.get_step_config(category, 'healing')
    
    # Determine input directory (prefer filtered, fall back to raw)
    input_dir = resolver.get_extracted_dir(category, "filtered")
    if not input_dir.exists() or not list(input_dir.glob("*.txt")):
        input_dir = resolver.get_extracted_dir(category, "raw")
        print(f"  Using raw directory (no filtered found)")
    
    output_dir = resolver.get_healed_dir(category, "merged_groups")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    gap_threshold = config.get('gap_threshold', 0.01)
    resolution = config.get('point_resolution', 0.002)
    num_slices = config.get('num_slices', 20)
    
    print(f"\n{'='*60}")
    print(f"STEP 3: HEALING - {category}")
    print(f"  Gap threshold: {gap_threshold}")
    print(f"  Resolution: {resolution}")
    print(f"  Slices: {num_slices}")
    print(f"  Input: {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")
    
    # Load and process
    g_xyz, g_rgb, g_ids, f_map = heal_module.load_point_clouds(input_dir)
    
    if len(g_xyz) == 0:
        print("  No point clouds found to heal.")
        return
    
    graph, filler_dict, g_xyz, g_rgb, g_ids = heal_module.detect_connections(
        g_xyz, g_rgb, g_ids, gap_threshold, resolution, num_slices
    )
    
    heal_module.save_merged_groups(graph, filler_dict, g_xyz, g_rgb, g_ids, str(output_dir), f_map)
    
    print(f"\n{'='*60}")
    print(f"STEP 3 COMPLETE [{category}]")
    print(f"{'='*60}")


def run_step_4_stl(resolver: PathResolver, category: str, **kwargs):
    """Run STL placement for a category."""
    import json
    import numpy as np
    import trimesh

    from importlib import import_module
    stl_module = import_module('4_create_stl')
    from stl_utils import save_results, check_mesh_watertight, load_trimesh

    cat_config = resolver.get_category_config(category)
    placement = cat_config.get('placement', {})
    mode = placement.get('mode', 'template')

    output_dir = resolver.get_stl_dir(category)
    output_dir.mkdir(parents=True, exist_ok=True)

    verbose = kwargs.get('verbose', True)

    print(f"\n{'='*60}")
    print(f"STEP 4: STL PLACEMENT - {category}")
    print(f"  Mode: {mode}")
    print(f"  ICP: {placement.get('use_icp', False)}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")

    # Check input template watertightness
    if mode == 'template':
        stl_path = resolver.get_stl_template_path(category)
        template = load_trimesh(stl_path)
        if template is not None:
            print(f"\n    --- Watertight Check: Input STL Template ---")
            check_mesh_watertight(template, f"Input template: {stl_path.name}")

    # Estimate vertical axis
    vertical_axis = stl_module.estimate_scene_vertical_axis(resolver)
    print(f"  Vertical axis: {vertical_axis}")

    # Process category
    try:
        candidates, meshes, stats = stl_module.process_category(
            resolver, category, vertical_axis, verbose=verbose
        )
    except NotImplementedError as e:
        print(f"\n  ERROR: {e}")
        return

    if meshes:
        # Check individual placed meshes
        print(f"\n    --- Watertight Check: Individual Placed Meshes ({category}) ---")
        n_wt = 0
        for mesh, cand in zip(meshes, candidates):
            chk = check_mesh_watertight(mesh, f"{cand.source} ({cand.mode})", indent=6)
            if chk['is_watertight']:
                n_wt += 1
        print(f"    Summary: {n_wt}/{len(meshes)} individual meshes are watertight")

        # Save per-category results
        merged = stats.get('merged', []) if isinstance(stats.get('merged'), list) else []
        save_results(
            output_dir, category, meshes, candidates, stats, merged,
            save_individual=True
        )

        # Check category combined
        cat_combined = load_trimesh(output_dir / f"{category}_combined.stl")
        if cat_combined is not None:
            print(f"\n    --- Watertight Check: Category Combined ({category}) ---")
            check_mesh_watertight(cat_combined, f"{category}_combined.stl")

        # Place mannequins (additive — does not affect placements)
        mannequin_meshes = stl_module.place_mannequins(
            resolver, category, candidates, meshes, verbose=verbose
        )
        if mannequin_meshes:
            mann_dir = output_dir / "mannequins"
            mann_dir.mkdir(parents=True, exist_ok=True)
            for i, m in enumerate(mannequin_meshes):
                m.export(str(mann_dir / f"mannequin_{i+1:03d}.stl"))
            print(f"  Saved: {len(mannequin_meshes)} mannequin STLs in {mann_dir}")

    print(f"\n{'='*60}")
    print(f"STEP 4 COMPLETE [{category}]: {len(meshes)} objects placed")
    print(f"{'='*60}")


def run_step_4_combine(resolver: PathResolver, **kwargs):
    """Combine all category STLs into furniture.stl and save individual furniture STLs."""
    import json
    import trimesh
    from stl_utils import check_mesh_watertight, print_watertight_report, load_trimesh

    output_dir = resolver.get_stl_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"COMBINING ALL CATEGORIES")
    print(f"{'='*60}")

    all_meshes = []
    all_names = []
    all_mannequin_meshes = []
    all_mannequin_names = []
    all_stats = {}
    wt_checks = []

    for category in resolver.get_enabled_categories():
        cat_dir = resolver.get_stl_dir(category)
        cat_individual_dir = cat_dir / "individual"
        cat_stl = cat_dir / f"{category}_combined.stl"
        cat_log = cat_dir / "scene_log.json"

        # Collect individual meshes from category (furniture only)
        if cat_individual_dir.exists():
            for stl_file in sorted(cat_individual_dir.glob("*.stl")):
                mesh = load_trimesh(stl_file)
                if mesh is not None:
                    all_meshes.append(mesh)
                    all_names.append(stl_file.stem)
        elif cat_stl.exists():
            mesh = load_trimesh(cat_stl)
            if mesh is not None:
                all_meshes.append(mesh)
                all_names.append(f"{category}_combined")

        # Collect mannequin meshes separately
        mann_dir = cat_dir / "mannequins"
        if mann_dir.exists():
            for stl_file in sorted(mann_dir.glob("*.stl")):
                mesh = load_trimesh(stl_file)
                if mesh is not None:
                    all_mannequin_meshes.append(mesh)
                    all_mannequin_names.append(stl_file.stem)

        if cat_stl.exists():
            print(f"  Loaded: {category} ({len([f for f in (cat_individual_dir.glob('*.stl') if cat_individual_dir.exists() else [])])} individual meshes)")
            mann_count = len(list(mann_dir.glob("*.stl"))) if mann_dir.exists() else 0
            if mann_count:
                print(f"          + {mann_count} mannequins")
            if cat_log.exists():
                with open(cat_log) as f:
                    all_stats[category] = json.load(f)
        else:
            print(f"  Not found: {cat_stl}")

    if not all_meshes:
        print("\n  No meshes to combine!")
        return

    # Save individual furniture STLs
    furniture_dir = output_dir / "furniture_individual"
    furniture_dir.mkdir(parents=True, exist_ok=True)
    for mesh, name in zip(all_meshes, all_names):
        path = furniture_dir / f"{name}.stl"
        mesh.export(str(path))
    # Include mannequin individual STLs alongside furniture
    for mesh, name in zip(all_mannequin_meshes, all_mannequin_names):
        path = furniture_dir / f"{name}.stl"
        mesh.export(str(path))
    total_individual = len(all_meshes) + len(all_mannequin_meshes)
    print(f"\n  Saved: {total_individual} individual STLs ({len(all_meshes)} furniture + {len(all_mannequin_meshes)} mannequins) in {furniture_dir}")

    # Save furniture.stl (no mannequins)
    combined = trimesh.util.concatenate(all_meshes)

    chk = check_mesh_watertight(combined, f"furniture.stl ({len(all_meshes)} meshes)")
    chk['stage'] = 'furniture_combined'
    wt_checks.append(chk)

    output_stl = output_dir / "furniture.stl"
    combined.export(str(output_stl))
    print(f"  Saved: {output_stl}")

    # Save furniture_with_mannequins.stl if mannequins exist
    if all_mannequin_meshes:
        combined_with_mann = trimesh.util.concatenate(all_meshes + all_mannequin_meshes)
        output_mann_stl = output_dir / "furniture_with_mannequins.stl"
        combined_with_mann.export(str(output_mann_stl))
        print(f"  Saved: {output_mann_stl} ({len(all_mannequin_meshes)} mannequins)")

    # Verify saved file round-trip
    reloaded = load_trimesh(output_stl)
    if reloaded is not None:
        chk = check_mesh_watertight(reloaded, "Reloaded furniture.stl (save/load round-trip)")
        chk['stage'] = 'saved_verification'
        wt_checks.append(chk)

    print_watertight_report(wt_checks)

    # Save watertight log
    wt_log_path = output_dir / "watertight_log.json"
    with open(wt_log_path, 'w') as f:
        json.dump(wt_checks, f, indent=2)
    print(f"\n  Saved: {wt_log_path}")

    # Scene log
    log_path = output_dir / "scene_log.json"
    with open(log_path, 'w') as f:
        json.dump({
            'categories': all_stats,
            'total_meshes': len(all_meshes),
        }, f, indent=2)
    print(f"  Saved: {log_path}")


def run_step_4b_edit(resolver: PathResolver, **kwargs):
    """Launch interactive scene editor for human-in-the-loop adjustments."""
    from importlib import import_module
    edit_module = import_module('4b_edit_scene')

    port = kwargs.get('port', 8051)
    edit_module.run_server(resolver, port=port)


def run_step_5_enclose(resolver: PathResolver, **kwargs):
    """Enclose scene in room with vents."""
    from importlib import import_module
    enclose_module = import_module('5_enclose_scene')

    verbose = kwargs.get('verbose', True)
    enclose_module.run_enclose_scene(resolver, verbose=verbose)


STEP_FUNCTIONS = {
    '1': run_step_1_segment,
    '2': run_step_2_extract,
    '2b': run_step_2b_filter,
    '3': run_step_3_heal,
    '4': run_step_4_stl,
}

# Steps that run once for the whole scene (not per-category)
SCENE_STEPS = {
    '4b': run_step_4b_edit,
    '5': run_step_5_enclose,
}


def main():
    parser = argparse.ArgumentParser(description="Video2STL Pipeline Runner")
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    parser.add_argument("--steps", type=str, default="1,2,2b,3,4,5",
                        help="Comma-separated steps to run (1,2,2b,3,4,5) or 'all'")
    parser.add_argument("--categories", type=str, default=None,
                        help="Comma-separated categories to process (default: all enabled)")
    parser.add_argument("--combine-only", action="store_true",
                        help="Only run the final STL combination step")
    parser.add_argument("--verbose", "-v", action="store_true")
    
    args = parser.parse_args()

    resolver = PathResolver(args.config)
    resolver.print_summary()

    # Copy config to run directory for reproducibility (with versioning)
    config_copy = resolver.copy_config_to_run_dir()
    print(f"\nConfig saved to: {config_copy}")

    if args.combine_only:
        run_step_4_combine(resolver)
        return
    
    # Parse steps
    if args.steps.lower() == 'all':
        steps = ['1', '2', '2b', '3', '4', '5']  # 4b is interactive, must be requested explicitly
    else:
        steps = [s.strip() for s in args.steps.split(',')]
    
    # Split into per-category steps and scene-level steps
    category_steps = [s for s in steps if s in STEP_FUNCTIONS]
    scene_steps = [s for s in steps if s in SCENE_STEPS]
    
    # Parse categories
    if args.categories:
        categories = [c.strip() for c in args.categories.split(',')]
    else:
        categories = resolver.get_enabled_categories()
    
    if not categories and category_steps:
        print("\nERROR: No enabled categories found in config!")
        sys.exit(1)
    
    print(f"\nSteps to run: {steps}")
    if category_steps:
        print(f"Categories: {categories}")
    
    # Run per-category steps
    if category_steps:
        for category in categories:
            print(f"\n{'#'*60}")
            print(f"# PROCESSING CATEGORY: {category}")
            print(f"{'#'*60}")
            
            resolver.ensure_dirs(category)
            
            for step in category_steps:
                try:
                    STEP_FUNCTIONS[step](resolver, category, verbose=args.verbose)
                except Exception as e:
                    print(f"\nERROR in step {step} for {category}: {e}")
                    import traceback
                    traceback.print_exc()
                    if input("\nContinue with next step? [y/N]: ").lower() != 'y':
                        sys.exit(1)
    
    # Combine all categories (runs if step 4 is requested)
    if '4' in steps and len(categories) > 0:
        run_step_4_combine(resolver)
    
    # Run scene-level steps
    for step in scene_steps:
        try:
            SCENE_STEPS[step](resolver, verbose=args.verbose)
        except Exception as e:
            print(f"\nERROR in step {step}: {e}")
            import traceback
            traceback.print_exc()
            if input("\nContinue? [y/N]: ").lower() != 'y':
                sys.exit(1)
    
    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()