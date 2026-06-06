#!/usr/bin/env python3
"""
Step 3: Heal and Merge Point Clouds

Detects connected point cloud fragments and merges them.
"""

import argparse
import gc
import os
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from path_resolver import PathResolver


def load_point_clouds(directory_path):
    """Load all point cloud files from directory."""
    directory = Path(directory_path)
    files = sorted(directory.glob("*.txt"))
    
    if not files:
        raise FileNotFoundError(f"No .txt files found in {directory}")
    
    print(f"[1/5] Loading {len(files)} files...")
    
    all_xyz, all_rgb, all_file_ids = [], [], []
    file_map = {}
    
    for file_id, file_path in enumerate(files):
        try:
            df = pd.read_csv(
                file_path, sep=r'\s+', comment='/', header=None,
                names=['x', 'y', 'z', 'r', 'g', 'b'], dtype='float32', engine='c'
            )
            
            if isinstance(df.iloc[0, 0], str):
                df = df.iloc[1:].astype('float32')
            
            xyz = df[['x', 'y', 'z']].values
            rgb = df[['r', 'g', 'b']].values
            
            # Normalize RGB to 0-255 range if needed
            if rgb.max() <= 1.0:
                rgb = rgb * 255.0
            
            all_xyz.append(xyz)
            all_rgb.append(rgb)
            all_file_ids.append(np.full(len(xyz), file_id, dtype=np.int32))
            file_map[file_id] = file_path.name
            
        except Exception as e:
            print(f"  Skipping {file_path.name}: {e}")
    
    global_xyz = np.vstack(all_xyz)
    global_rgb = np.vstack(all_rgb)
    global_ids = np.concatenate(all_file_ids)
    
    print(f"  Loaded {len(global_xyz)} points from {len(file_map)} files")
    
    del all_xyz, all_rgb, all_file_ids
    gc.collect()
    
    return global_xyz, global_rgb, global_ids, file_map


def detect_connections(xyz, rgb, ids, gap_threshold, resolution, num_slices):
    """Detect connected point cloud fragments using spatial slicing."""
    
    print(f"[2/5] Sorting spatially...")
    sort_idx = np.argsort(xyz[:, 0])
    xyz = xyz[sort_idx]
    rgb = rgb[sort_idx]
    ids = ids[sort_idx]
    
    x_min, x_max = xyz[:, 0].min(), xyz[:, 0].max()
    slice_width = (x_max - x_min) / num_slices
    
    G = nx.Graph()
    unique_ids = np.unique(ids)
    for uid in unique_ids:
        G.add_node(int(uid))
    
    edge_fillers = {}
    
    print(f"[3/5] Detecting connections in {num_slices} slabs...")
    
    for i in range(num_slices):
        current_x_start = x_min + (i * slice_width)
        current_x_end = current_x_start + slice_width + gap_threshold
        primary_boundary = current_x_start + slice_width
        
        idx_start = np.searchsorted(xyz[:, 0], current_x_start)
        idx_end = np.searchsorted(xyz[:, 0], current_x_end)
        
        if idx_end <= idx_start:
            continue
        
        slab_xyz = xyz[idx_start:idx_end]
        slab_rgb = rgb[idx_start:idx_end]
        slab_ids = ids[idx_start:idx_end]
        
        unique_in_slab = np.unique(slab_ids)
        if len(unique_in_slab) < 2:
            continue
        
        print(f"  Slab {i+1}/{num_slices}: {len(slab_xyz)} pts, {len(unique_in_slab)} files...", end="", flush=True)
        
        file_data = {}
        for fid in unique_in_slab:
            mask = (slab_ids == fid)
            file_data[fid] = {'xyz': slab_xyz[mask], 'rgb': slab_rgb[mask]}
        
        cross_file_pairs = 0
        file_list = list(unique_in_slab)
        
        for fi, fid_a in enumerate(file_list):
            for fid_b in file_list[fi+1:]:
                data_a = file_data[fid_a]
                data_b = file_data[fid_b]
                
                tree_b = cKDTree(data_b['xyz'])
                distances, indices = tree_b.query(data_a['xyz'], k=1, workers=-1)
                
                valid_mask = distances <= gap_threshold
                if not np.any(valid_mask):
                    continue
                
                valid_a = np.where(valid_mask)[0]
                valid_b = indices[valid_mask]
                
                pts_a = data_a['xyz'][valid_a]
                pts_b = data_b['xyz'][valid_b]
                
                in_primary = (pts_a[:, 0] <= primary_boundary) | (pts_b[:, 0] <= primary_boundary)
                if not np.any(in_primary):
                    continue
                
                valid_a = valid_a[in_primary]
                valid_b = valid_b[in_primary]
                pts_a = pts_a[in_primary]
                pts_b = pts_b[in_primary]
                
                if not G.has_edge(int(fid_a), int(fid_b)):
                    G.add_edge(int(fid_a), int(fid_b))
                
                # Generate filler points
                rgb_a = data_a['rgb'][valid_a]
                rgb_b = data_b['rgb'][valid_b]
                
                dists = np.linalg.norm(pts_b - pts_a, axis=1)
                steps = (dists / resolution).astype(np.int32)
                
                edge_key = (min(int(fid_a), int(fid_b)), max(int(fid_a), int(fid_b)))
                if edge_key not in edge_fillers:
                    edge_fillers[edge_key] = []
                
                need_fill_idx = np.where(steps > 0)[0]
                for j in need_fill_idx:
                    n_steps = steps[j]
                    ratios = np.linspace(0, 1, n_steps + 2)[1:-1].astype(np.float32)
                    new_xyz = np.outer(1 - ratios, pts_a[j]) + np.outer(ratios, pts_b[j])
                    new_rgb = np.outer(1 - ratios, rgb_a[j]) + np.outer(ratios, rgb_b[j])
                    edge_fillers[edge_key].append(np.hstack((new_xyz, new_rgb)).astype(np.float32))
                
                cross_file_pairs += len(valid_a)
        
        print(f" {cross_file_pairs} pairs")
        del file_data
        gc.collect()
    
    print(f"\n  Connections detected: {G.number_of_edges()} edges")
    return G, edge_fillers, xyz, rgb, ids


def save_merged_groups(G, edge_fillers, global_xyz, global_rgb, global_ids, output_dir, file_map):
    """Save merged point cloud groups."""
    
    print(f"[4/5] Grouping and saving...")
    
    components = list(nx.connected_components(G))
    merged_groups = [c for c in components if len(c) > 1]
    isolated_files = [c for c in components if len(c) == 1]
    
    print(f"  Found {len(merged_groups)} merged groups, {len(isolated_files)} isolated files")
    
    os.makedirs(output_dir, exist_ok=True)
    
    for group_idx, file_group in enumerate(components):
        file_group_list = sorted(list(file_group))
        
        if len(file_group_list) == 1:
            f_id = file_group_list[0]
            original_name = file_map.get(f_id, f"file_{f_id}")
            output_filename = f"isolated_{original_name}"
        else:
            output_filename = f"merged_group_{group_idx}.txt"
        
        output_path = os.path.join(output_dir, output_filename)
        
        with open(output_path, 'w') as f_out:
            f_out.write("//X Y Z R G B\n")
            
            for f_id in file_group_list:
                mask = (global_ids == f_id)
                pts = global_xyz[mask]
                cols = global_rgb[mask]
                
                # Ensure RGB is in 0-255 range and clipped
                cols = np.clip(cols, 0, 255)
                
                if len(pts) > 0:
                    data = np.hstack((pts, cols))
                    # Use integer format for RGB values
                    for row in data:
                        f_out.write(f"{row[0]:.6f} {row[1]:.6f} {row[2]:.6f} {int(row[3])} {int(row[4])} {int(row[5])}\n")
            
            if len(file_group_list) > 1:
                subgraph = G.subgraph(file_group)
                for (u, v) in subgraph.edges():
                    key = tuple(sorted((u, v)))
                    if key in edge_fillers and edge_fillers[key]:
                        fillers = np.vstack(edge_fillers[key])
                        # Clip filler RGB values too
                        fillers[:, 3:6] = np.clip(fillers[:, 3:6], 0, 255)
                        for row in fillers:
                            f_out.write(f"{row[0]:.6f} {row[1]:.6f} {row[2]:.6f} {int(row[3])} {int(row[4])} {int(row[5])}\n")
        
        gc.collect()
    
    print(f"  Saved {len(components)} files to {output_dir}")


def run_healing(resolver: PathResolver):
    """Run point cloud healing."""
    
    print("=" * 60)
    print("STEP 3: POINT CLOUD HEALING")
    print("=" * 60)
    
    # Get config
    gap_threshold = resolver.get('healing', 'gap_threshold', default=0.01)
    resolution = resolver.get('healing', 'point_resolution', default=0.002)
    num_slices = resolver.get('healing', 'num_slices', default=20)
    
    input_dir = resolver.pointclouds_filtered_dir
    output_dir = resolver.healed_dir / "merged_groups"
    
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Gap threshold: {gap_threshold}")
    print(f"Resolution: {resolution}")
    print(f"Slices: {num_slices}")
    
    # Check input
    if not input_dir.exists():
        # Try raw directory if filtered doesn't exist
        input_dir = resolver.pointclouds_raw_dir
        print(f"  Using raw directory: {input_dir}")
    
    # Load and process
    g_xyz, g_rgb, g_ids, f_map = load_point_clouds(input_dir)
    
    graph, filler_dict, g_xyz, g_rgb, g_ids = detect_connections(
        g_xyz, g_rgb, g_ids, gap_threshold, resolution, num_slices
    )
    
    save_merged_groups(graph, filler_dict, g_xyz, g_rgb, g_ids, str(output_dir), f_map)
    
    print("\n" + "=" * 60)
    print("STEP 3 COMPLETE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Step 3: Point Cloud Healing")
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    args = parser.parse_args()
    
    resolver = PathResolver(args.config)
    run_healing(resolver)


if __name__ == "__main__":
    main()