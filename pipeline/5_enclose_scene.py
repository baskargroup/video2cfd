#!/usr/bin/env python3
"""
Step 5: Enclose Scene in Room

Creates a room enclosure around the furniture from Step 4:
  1. Single box covering the furniture scene (flat or auditorium floor)
  2. Rectangular vent openings cut through the ceiling
  3. Exports: room.stl, furniture_and_room.stl

Usage:
    python 5_enclose_scene.py --config ../configs/default.yaml

Auditorium floor mode (room.floor_type: "auditorium"):
  Builds a room with:
    - A raised stage at the front (full room width)
    - A flat floor section behind the stage
    - An inclined floor rising toward the back (matches chair tier angle)
  Config params:
    flat_depth:      depth of flat floor section behind stage (m)
    stage_depth:     front-to-back depth of the stage (m)
    stage_height:    height of the stage surface above base floor (m)
    floor_tilt_deg:  inclination of back floor in degrees (null = auto from chairs)
"""

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import trimesh

from path_resolver import PathResolver
from stl_utils import manifold_to_trimesh, trimesh_to_manifold, check_mesh_watertight


# ============================================================================
# ROOM CREATION
# ============================================================================

def _rotation_matrix_z(angle_deg: float) -> np.ndarray:
    """4x4 rotation matrix around Z axis."""
    rad = np.radians(angle_deg)
    c, s = np.cos(rad), np.sin(rad)
    m = np.eye(4)
    m[0, 0] = c;  m[0, 1] = -s
    m[1, 0] = s;  m[1, 1] = c
    return m


def create_room(room_min: np.ndarray, room_max: np.ndarray) -> trimesh.Trimesh:
    """Create a single box room enclosure (axis-aligned)."""
    size = room_max - room_min
    mesh = trimesh.creation.box(extents=size, transform=trimesh.transformations.translation_matrix(
        (room_min + room_max) / 2
    ))
    # Flip normals inward so the interior faces point into the room
    mesh.invert()
    return mesh


def cut_vent(room: trimesh.Trimesh, position: np.ndarray,
             size: np.ndarray, ceiling_z: float) -> trimesh.Trimesh:
    """Cut a rectangular hole through the ceiling of a room mesh."""
    import manifold3d

    # We need outward normals for boolean ops, then flip back after
    room.invert()
    room_m = trimesh_to_manifold(room)

    # Cutout box that punches through the ceiling
    cutout_h = 0.02
    cutout = manifold3d.Manifold.cube([size[0], size[1], cutout_h])
    cutout = cutout.translate([
        position[0] - size[0] / 2,
        position[1] - size[1] / 2,
        ceiling_z - cutout_h / 2
    ])

    result = manifold_to_trimesh(room_m - cutout)
    result.invert()  # Flip normals back inward
    return result


# ============================================================================
# AUDITORIUM FLOOR GEOMETRY
# ============================================================================

def _collect_chair_positions_aligned(resolver: 'PathResolver',
                                      rotation_deg: float,
                                      pivot: np.ndarray) -> np.ndarray:
    """Load all chair placement positions and rotate into the aligned frame.

    Returns an (N, 3) array of [x, y, z] positions, or an empty array if
    no placement logs are found.
    """
    positions = []
    for cat_name in resolver.get_enabled_categories():
        log_path = resolver.get_stl_dir(cat_name) / "scene_log_edited.json"
        if not log_path.exists():
            log_path = resolver.get_stl_dir(cat_name) / "scene_log.json"
        if not log_path.exists():
            continue
        with open(log_path) as f:
            log = json.load(f)
        for p in log.get("placements", []):
            pos = p.get("position", [0, 0, 0])
            positions.append([float(pos[0]), float(pos[1]), float(pos[2])])

    if not positions:
        return np.empty((0, 3))

    pts = np.array(positions)

    if abs(rotation_deg) > 1e-6:
        rad = np.radians(-rotation_deg)
        c, s = np.cos(rad), np.sin(rad)
        xy = pts[:, :2] - pivot[:2]
        pts = np.column_stack([
            xy[:, 0] * c - xy[:, 1] * s + pivot[0],
            xy[:, 0] * s + xy[:, 1] * c + pivot[1],
            pts[:, 2],
        ])

    return pts


def _slope_from_points(pts: np.ndarray) -> float:
    """Least-squares slope of z = slope * y + intercept for a set of (y, z) points."""
    y_vals = pts[:, 1]
    z_vals = pts[:, 2]
    n = len(y_vals)
    denom = n * np.sum(y_vals ** 2) - np.sum(y_vals) ** 2
    if abs(denom) < 1e-12:
        return 0.0
    return float((n * np.sum(y_vals * z_vals) - np.sum(y_vals) * np.sum(z_vals)) / denom)


def _compute_floor_tilt_from_placements(resolver: 'PathResolver',
                                         rotation_deg: float,
                                         pivot: np.ndarray,
                                         y_min: float = None) -> float:
    """Estimate floor tilt slope (dz/dy) from chair placement positions.

    If *y_min* is given, only positions with y >= y_min are used (restricts
    the fit to the inclined section, excluding stage / flat chairs).

    Returns slope in m/m (positive = floor rises with increasing Y).
    """
    pts = _collect_chair_positions_aligned(resolver, rotation_deg, pivot)
    if len(pts) < 2:
        return 0.0

    if y_min is not None:
        pts = pts[pts[:, 1] >= y_min]
        if len(pts) < 2:
            return 0.0

    return _slope_from_points(pts)


def _make_auditorium_mesh(xL: float, xR: float,
                           yF: float, yS: float, yP: float, yB: float,
                           zF: float, zST: float, zB: float, zC: float) -> trimesh.Trimesh:
    """Build a closed auditorium room mesh with inward-facing normals.

    Coordinate layout (aligned frame, increasing Y = toward back):
        xL/xR : left / right walls
        yF    : front (stage face)
        yS    : stage back edge  (= yF + stage_depth)
        yP    : flat/incline junction (= yS + flat_depth)
        yB    : back wall
        zF    : base floor level
        zST   : stage top surface  (= zF + stage_height)
        zB    : back-floor Z at y=yB  (= zF + tilt * (yB - yP))
        zC    : ceiling

    Degenerate cases handled:
        stage_height == 0  → stage top / back-face are omitted, zST = zF
        flat_depth   == 0  → flat floor is omitted,  yP = yS
    """
    has_stage = (zST - zF) > 1e-6
    has_flat  = (yP  - yS) > 1e-6

    # ---- vertex pool ----
    # Left side  [0..6]
    # 0: front-left at stage/floor level
    v0  = [xL, yF, zST if has_stage else zF]
    # 1: stage-back-left top
    v1  = [xL, yS, zST if has_stage else zF]
    # 2: stage-back-left floor  (= flat floor start)
    v2  = [xL, yS, zF]
    # 3: flat/incline junction left
    v3  = [xL, yP, zF]
    # 4: back-left at inclined floor
    v4  = [xL, yB, zB]
    # 5: back-left ceiling
    v5  = [xL, yB, zC]
    # 6: front-left ceiling
    v6  = [xL, yF, zC]

    # Right side [7..13]
    v7  = [xR, yF, zST if has_stage else zF]
    v8  = [xR, yS, zST if has_stage else zF]
    v9  = [xR, yS, zF]
    v10 = [xR, yP, zF]
    v11 = [xR, yB, zB]
    v12 = [xR, yB, zC]
    v13 = [xR, yF, zC]

    verts = np.array([v0,v1,v2,v3,v4,v5,v6,
                      v7,v8,v9,v10,v11,v12,v13], dtype=np.float64)

    faces = []

    # ---- ceiling  (normal -Z) ----
    faces += [[6,5,12], [6,12,13]]

    # ---- back wall  (normal -Y) ----
    faces += [[4,11,12], [4,12,5]]

    # ---- front wall  (normal +Y) ----
    # Bottom of front wall is at stage level (or floor if no stage)
    faces += [[0,6,13], [0,13,7]]

    # ---- left wall  (normal +X, fan from vertex 6) ----
    if has_stage and has_flat:
        # Full 7-vertex polygon: 6,0,1,2,3,4,5
        faces += [[6,0,1],[6,1,2],[6,2,3],[6,3,4],[6,4,5]]
    elif has_stage and not has_flat:
        # yP == yS → vertices 2,3 coincide; skip flat section
        faces += [[6,0,1],[6,1,2],[6,2,4],[6,4,5]]
    elif not has_stage and has_flat:
        # zST == zF → vertices 0,1 at floor; skip stage top/back
        faces += [[6,0,3],[6,3,4],[6,4,5]]
    else:
        # No stage, no flat → simple 5-vertex wall
        faces += [[6,0,4],[6,4,5]]

    # ---- right wall  (normal -X, fan from vertex 12) ----
    if has_stage and has_flat:
        faces += [[12,11,10],[12,10,9],[12,9,8],[12,8,7],[12,7,13]]
    elif has_stage and not has_flat:
        faces += [[12,11,9],[12,9,8],[12,8,7],[12,7,13]]
    elif not has_stage and has_flat:
        faces += [[12,11,10],[12,10,7],[12,7,13]]
    else:
        faces += [[12,11,7],[12,7,13]]

    # ---- stage top  (normal +Z) — only when has_stage ----
    if has_stage:
        faces += [[0,7,8],[0,8,1]]

    # ---- stage back face  (normal +Y) — only when has_stage ----
    if has_stage:
        faces += [[9,2,1],[9,1,8]]

    # ---- flat floor  (normal +Z) — only when has_flat ----
    if has_flat:
        # Front edge: yS (or yF when no stage), back edge: yP
        f_y0 = yS
        iv_lf = 2   # xL, f_y0, zF
        iv_lb = 3   # xL, yP,   zF
        iv_rf = 9   # xR, f_y0, zF
        iv_rb = 10  # xR, yP,   zF
        faces += [[iv_lf, iv_rf, iv_rb], [iv_lf, iv_rb, iv_lb]]

    # ---- inclined floor  (normal up+forward) ----
    faces += [[3,10,11],[3,11,4]]

    faces_arr = np.array(faces, dtype=np.int32)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces_arr, process=False)
    return mesh


def build_auditorium_room(scene_mesh: trimesh.Trimesh, room_config: dict,
                           resolver: 'PathResolver' = None,
                           verbose: bool = True) -> trimesh.Trimesh:
    """Build an auditorium-style room enclosure.

    Floor profile (front → back, increasing Y):
        [stage]  →  [flat floor]  →  [inclined floor rising to back]

    Walls and ceiling remain flat / axis-aligned.
    """
    rotation_deg = room_config.get('rotation_deg', 0.0)
    room_min, room_max = compute_room_bounds(scene_mesh, room_config)

    stage_depth  = float(room_config.get('stage_depth',  0.2))
    flat_depth   = float(room_config.get('flat_depth',   0.3))
    stage_height = float(room_config.get('stage_height', 0.05))

    xL, yF, zF = room_min[0], room_min[1], room_min[2]
    xR, yB, zC = room_max[0], room_max[1], room_max[2]

    yS = yF + stage_depth
    yP = yS + flat_depth

    # Clamp junction to room depth
    if yS > yB:
        yS = yB
    if yP > yB:
        yP = yB

    zST = zF + stage_height

    # --- Floor tilt ---
    floor_tilt_deg = room_config.get('floor_tilt_deg', None)
    if abs(rotation_deg) > 1e-6:
        pivot = np.mean(scene_mesh.vertices, axis=0)
    else:
        pivot = np.zeros(3)

    if floor_tilt_deg is not None:
        tilt_slope = float(np.tan(np.radians(float(floor_tilt_deg))))
    elif resolver is not None:
        tilt_slope = _compute_floor_tilt_from_placements(
            resolver, rotation_deg, pivot, y_min=yP)
    else:
        tilt_slope = 0.0

    incline_depth = yB - yP
    zB = zF + tilt_slope * incline_depth if incline_depth > 1e-6 else zF

    # --- Floor clearance: ensure no chair vertex is clipped by the inclined floor ---
    # The regression slope is a mean fit, so the floor surface can intersect chair
    # legs that sit slightly below the fit line.  Use the ACTUAL mesh vertices (not
    # placement centroids) to guarantee the floor lies below every furniture point
    # in the inclined section, with a small clearance buffer.
    _FLOOR_CLEARANCE = 0.0
    if incline_depth > 1e-6:
        # Rotate scene vertices into the aligned frame if needed
        verts = scene_mesh.vertices.copy()
        if abs(rotation_deg) > 1e-6:
            rad = np.radians(-rotation_deg)
            c, s = np.cos(rad), np.sin(rad)
            xy = verts[:, :2] - pivot[:2]
            verts = np.column_stack([
                xy[:, 0] * c - xy[:, 1] * s + pivot[0],
                xy[:, 0] * s + xy[:, 1] * c + pivot[1],
                verts[:, 2],
            ])
        incline_mask = verts[:, 1] >= yP
        if incline_mask.any():
            y_inc = verts[incline_mask, 1]
            z_inc = verts[incline_mask, 2]
            # For each vertex, max zF that keeps floor below it:
            # zF + slope * (y - yP) <= z_vertex  →  zF <= z_vertex - slope*(y-yP)
            zF_safe = float(np.min(z_inc - tilt_slope * (y_inc - yP))) - _FLOOR_CLEARANCE
            if zF_safe < zF:
                drop = zF - zF_safe
                if verbose:
                    print(f"    Floor clearance: lowering base by {drop:.4f} m "
                          f"to clear all chair vertices")
                zF = zF_safe
                zB = zF + tilt_slope * incline_depth

    # Ensure ceiling clears the back floor
    if zB >= zC:
        zC = zB + (room_max[2] - room_min[2])

    if verbose:
        print(f"\n    --- Auditorium Room Dimensions (aligned frame) ---")
        print(f"    Width  (X): {xR - xL:.3f} m")
        print(f"    Depth  (Y): {yB - yF:.3f} m")
        print(f"    Height (Z): {zC - zF:.3f} m")
        print(f"    Stage:      depth={stage_depth:.3f} m, height={stage_height:.3f} m")
        print(f"    Flat:       depth={flat_depth:.3f} m")
        print(f"    Tilt:       slope={tilt_slope:.4f} m/m  ({np.degrees(np.arctan(tilt_slope)):.1f}°)")
        print(f"    Back floor: Z={zB:.4f} m (rise={zB - zF:.4f} m)")

    room = _make_auditorium_mesh(xL, xR, yF, yS, yP, yB, zF, zST, zB, zC)

    # Rotate back to world frame if needed
    if abs(rotation_deg) > 1e-6:
        pivot = np.mean(scene_mesh.vertices, axis=0)
        room.invert()
        T_neg = trimesh.transformations.translation_matrix(-pivot)
        R     = _rotation_matrix_z(rotation_deg)
        T_pos = trimesh.transformations.translation_matrix(pivot)
        room.apply_transform(T_pos @ R @ T_neg)
        room.invert()
        if verbose:
            print(f"    Rotated room by {rotation_deg:.1f}° around scene centroid")

    if verbose:
        print(f"    Room mesh: {len(room.faces)} faces, watertight={room.is_watertight}")

    return room


# ============================================================================
# MAIN LOGIC
# ============================================================================

def compute_room_bounds(scene_mesh: trimesh.Trimesh,
                        room_config: dict) -> tuple:
    """Compute room min/max from scene bounds and config.

    When *rotation_deg* is set, the scene vertices are rotated into the
    axis-aligned frame first so that the bounding box aligns with the
    real room walls.  The returned min/max are in that rotated frame;
    ``create_room`` will rotate the box back.
    """
    padding = room_config.get('padding', 0.1)
    room_height = room_config.get('height', None)
    headroom = room_config.get('headroom', 0.5)
    rotation_deg = room_config.get('rotation_deg', 0.0)

    # Rotate scene vertices into axis-aligned frame for bbox computation
    if abs(rotation_deg) > 1e-6:
        verts = scene_mesh.vertices.copy()
        pivot = np.mean(verts, axis=0)
        R_inv = _rotation_matrix_z(-rotation_deg)[:3, :3]
        verts_rot = (verts - pivot) @ R_inv.T + pivot
        scene_min = verts_rot.min(axis=0)
        scene_max = verts_rot.max(axis=0)
    else:
        scene_min = scene_mesh.bounds[0].copy()
        scene_max = scene_mesh.bounds[1].copy()

    room_min = scene_min - padding
    room_min[2] = scene_min[2]  # floor meets furniture bottom
    room_max = scene_max + padding

    if room_height is not None:
        room_max[2] = room_min[2] + room_height
    else:
        room_max[2] = scene_max[2] + headroom

    # Optional explicit size override
    size_override = room_config.get('size', None)
    if size_override is not None:
        center_xy = (room_min[:2] + room_max[:2]) / 2
        room_min[0] = center_xy[0] - size_override[0] / 2
        room_max[0] = center_xy[0] + size_override[0] / 2
        room_min[1] = center_xy[1] - size_override[1] / 2
        room_max[1] = center_xy[1] + size_override[1] / 2
        if len(size_override) >= 3:
            room_max[2] = room_min[2] + size_override[2]

    return room_min, room_max


def build_room(scene_mesh: trimesh.Trimesh, room_config: dict,
               verbose: bool = True,
               resolver: 'PathResolver' = None) -> trimesh.Trimesh:
    """Build room enclosure with optional vent cutouts.

    Dispatches to auditorium geometry when ``floor_type: "auditorium"``
    is set in room_config; otherwise builds a plain box.

    When *rotation_deg* is set, the bounding box is computed in a
    rotated frame and the final room mesh is rotated back into world
    coordinates.
    """
    floor_type = room_config.get('floor_type', 'flat')

    if floor_type == 'auditorium':
        if verbose:
            scene_size_v = scene_mesh.bounds[1] - scene_mesh.bounds[0]
            print(f"    Scene size: {scene_size_v[0]:.3f} x {scene_size_v[1]:.3f} x {scene_size_v[2]:.3f} m")
            rotation_deg = room_config.get('rotation_deg', 0.0)
            if abs(rotation_deg) > 1e-6:
                print(f"    Room rotation: {rotation_deg:.1f}°")
        return build_auditorium_room(scene_mesh, room_config,
                                     resolver=resolver, verbose=verbose)

    rotation_deg = room_config.get('rotation_deg', 0.0)
    room_min, room_max = compute_room_bounds(scene_mesh, room_config)
    room_size = room_max - room_min
    ceiling_z = room_max[2]

    # Pivot: centre of the furniture in the rotated frame, mapped back
    # to world.  For rotation_deg == 0 this is just the scene centroid.
    if abs(rotation_deg) > 1e-6:
        verts = scene_mesh.vertices.copy()
        pivot = np.mean(verts, axis=0)
    else:
        pivot = None

    if verbose:
        scene_size_v = scene_mesh.bounds[1] - scene_mesh.bounds[0]
        print(f"    Scene size: {scene_size_v[0]:.3f} x {scene_size_v[1]:.3f} x {scene_size_v[2]:.3f} m")
        if abs(rotation_deg) > 1e-6:
            print(f"    Room rotation: {rotation_deg:.1f}°")
        print(f"\n    --- Room Dimensions (aligned frame) ---")
        print(f"    Length (X'): {room_size[0]:.3f} m")
        print(f"    Width  (Y'): {room_size[1]:.3f} m")
        print(f"    Height (Z):  {room_size[2]:.3f} m")

    # Create room box axis-aligned first (rotation applied after vents)
    room = create_room(room_min, room_max)
    if verbose:
        print(f"    Room box: {len(room.faces)} faces, watertight={room.is_watertight}")

    # Cut vents (only if enabled)
    vents_enabled = room_config.get('vents_enabled', False)
    if not vents_enabled:
        if verbose:
            print(f"    Vents: disabled (set vents_enabled: true to enable)")
    else:
        for i, vent in enumerate(room_config.get('vents', [])):
            vent_size = np.array(vent['size'], dtype=np.float64)

            if 'position' in vent:
                vent_pos = np.array(vent['position'], dtype=np.float64)
            else:
                center = (room_min[:2] + room_max[:2]) / 2
                frac = 0.25 if vent.get('type') == 'inlet' else 0.75
                vent_pos = np.array([room_min[0] + room_size[0] * frac, center[1]])

            if verbose:
                print(f"    Vent {i+1} ({vent.get('type', '?')}): "
                      f"{vent_size[0]:.3f}x{vent_size[1]:.3f} m at ({vent_pos[0]:.3f}, {vent_pos[1]:.3f})")

            try:
                room = cut_vent(room, vent_pos, vent_size, ceiling_z)
                if verbose:
                    print(f"      Applied, watertight={room.is_watertight}")
            except Exception as e:
                print(f"      ERROR: {e}")

    # Rotate room into world frame (after vents are cut in aligned frame)
    if abs(rotation_deg) > 1e-6 and pivot is not None:
        # Room was built with inward normals → flip out for transform, flip back
        room.invert()
        T_neg = trimesh.transformations.translation_matrix(-pivot)
        R = _rotation_matrix_z(rotation_deg)
        T_pos = trimesh.transformations.translation_matrix(pivot)
        room.apply_transform(T_pos @ R @ T_neg)
        room.invert()
        if verbose:
            print(f"    Rotated room by {rotation_deg:.1f}° around scene centroid")

    return room


# ============================================================================
# AXIS-ALIGNED SCENE FOR CFD
# ============================================================================

def create_axis_aligned_scene(resolver: PathResolver,
                              furniture: trimesh.Trimesh,
                              room_config: dict,
                              verbose: bool = True):
    """Create axis-aligned copies of all scene STLs for CFD simulation.

    Rotates every mesh by ``-rotation_deg`` around the furniture centroid
    so that the room walls align with the coordinate axes.  Output goes
    to ``<stl_dir>/axis_aligned/``, mirroring the normal output structure.
    """
    rotation_deg = room_config.get('rotation_deg', 0.0)
    if abs(rotation_deg) < 1e-6:
        if verbose:
            print("\n  Scene already axis-aligned — skipping axis_aligned/ output")
        return

    stl_dir = resolver.get_stl_dir()
    aa_dir = stl_dir / "axis_aligned"
    if aa_dir.exists():
        shutil.rmtree(aa_dir)
    aa_dir.mkdir(parents=True)

    # Pivot = mean of furniture vertices (same pivot used by build_room)
    pivot = np.mean(furniture.vertices, axis=0)
    R_inv = _rotation_matrix_z(-rotation_deg)
    T_neg = trimesh.transformations.translation_matrix(-pivot)
    T_pos = trimesh.transformations.translation_matrix(pivot)
    transform = T_pos @ R_inv @ T_neg

    def _load(path):
        mesh = trimesh.load(str(path))
        if isinstance(mesh, trimesh.Scene):
            mesh = trimesh.util.concatenate(
                [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
            )
        return mesh

    def _rotate_and_save(src: Path, dst: Path, inward_normals: bool = False):
        """Load an STL, rotate into axis-aligned frame, and save."""
        mesh = _load(src)
        if inward_normals:
            mesh.invert()
        mesh.apply_transform(transform)
        if inward_normals:
            mesh.invert()
        dst.parent.mkdir(parents=True, exist_ok=True)
        mesh.export(str(dst))
        return mesh

    if verbose:
        print(f"\n  Creating axis-aligned scene (rotating by {-rotation_deg:.1f}°)...")

    # --- Top-level furniture files ---
    furniture_aa = None
    for fname in [
        "furniture_edited_with_mannequins.stl",
        "furniture_with_mannequins.stl",
        "furniture_edited.stl",
        "furniture.stl",
        "scene_combined.stl",
    ]:
        src = stl_dir / fname
        if src.exists():
            m = _rotate_and_save(src, aa_dir / fname)
            if furniture_aa is None:
                furniture_aa = m

    # --- Individual directories at top level ---
    for subdir_name in ["furniture_individual_edited", "added", "added_procedural"]:
        subdir = stl_dir / subdir_name
        if subdir.exists():
            for stl_file in sorted(subdir.glob("*.stl")):
                _rotate_and_save(stl_file, aa_dir / subdir_name / stl_file.name)

    # --- Per-category files ---
    for cat_name in resolver.get_enabled_categories():
        cat_dir = resolver.get_stl_dir(cat_name)
        aa_cat_dir = aa_dir / cat_name

        for fname in [
            f"{cat_name}_combined_edited.stl",
            f"{cat_name}_mannequins_edited.stl",
            f"{cat_name}_mannequins.stl",
        ]:
            src = cat_dir / fname
            if src.exists():
                _rotate_and_save(src, aa_cat_dir / fname)

        for subdir_name in ["individual_edited", "mannequins_edited"]:
            subdir = cat_dir / subdir_name
            if subdir.exists():
                for stl_file in sorted(subdir.glob("*.stl")):
                    _rotate_and_save(stl_file, aa_cat_dir / subdir_name / stl_file.name)

    # --- Room (inward-facing normals) ---
    room_src = stl_dir / "room.stl"
    room_aa = None
    if room_src.exists():
        room_aa = _rotate_and_save(room_src, aa_dir / "room.stl", inward_normals=True)

    # --- Combined furniture + room ---
    if furniture_aa is not None and room_aa is not None:
        combined = trimesh.util.concatenate([furniture_aa, room_aa])
        combined.export(str(aa_dir / "furniture_and_room.stl"))

    n_files = len(list(aa_dir.rglob("*.stl")))
    if verbose:
        print(f"  Axis-aligned: {n_files} STL files -> {aa_dir}")


# ============================================================================
# ENTRY POINT
# ============================================================================

def run_enclose_scene(resolver: PathResolver, verbose: bool = True,
                      room_overrides: dict = None):
    """Run Step 5: build room around furniture, save outputs.

    *room_overrides* (optional) — dict of room config keys to override,
    e.g. ``{"rotation_deg": 30}`` from the editor.
    """
    print("=" * 60)
    print("STEP 5: ENCLOSE SCENE IN ROOM")
    print("=" * 60)

    output_dir = resolver.get_stl_dir()

    # Load furniture — prefer versions with mannequins, then edited, then base
    furniture_path = None
    for candidate in [
        "furniture_edited_with_mannequins.stl",
        "furniture_with_mannequins.stl",
        "furniture_edited.stl",
        "furniture.stl",
        "scene_combined.stl",
    ]:
        p = output_dir / candidate
        if p.exists():
            furniture_path = p
            break
    if furniture_path is None:
        print(f"  ERROR: No furniture STL found. Run Step 4 first.")
        return

    print(f"\n  Loading: {furniture_path}")
    furniture = trimesh.load(str(furniture_path))
    if isinstance(furniture, trimesh.Scene):
        furniture = trimesh.util.concatenate(
            [g for g in furniture.geometry.values() if isinstance(g, trimesh.Trimesh)]
        )
    print(f"  Furniture: {len(furniture.vertices):,} verts, {len(furniture.faces):,} faces")

    # Build room — merge YAML config with editor overrides (room_config.json)
    room_config = resolver.config.get('room', {})
    editor_rc_path = output_dir / "room_config.json"
    if editor_rc_path.exists():
        with open(editor_rc_path) as f:
            editor_rc = json.load(f)
        room_config = {**room_config, **editor_rc}
        print(f"\n  Applied editor room config: {editor_rc_path}")
    if room_overrides:
        room_config = {**room_config, **room_overrides}
    print(f"\n  Building room...")

    # Compute room bounds (needed for log)
    room_min, room_max = compute_room_bounds(furniture, room_config)
    room_size = room_max - room_min
    scene_size = furniture.bounds[1] - furniture.bounds[0]

    room = build_room(furniture, room_config, verbose=verbose, resolver=resolver)

    # Combine for visualization
    combined = trimesh.util.concatenate([room, furniture])

    # --- Save ---
    paths = {
        'room.stl': room,
        'furniture_and_room.stl': combined,
    }
    print(f"\n  Saving outputs:")
    for name, mesh in paths.items():
        path = output_dir / name
        mesh.export(str(path))
        print(f"    {name}")

    # --- Watertight checks ---
    print(f"\n  --- Watertight Checks ---")
    checks = {
        'room.stl': room,
        'furniture.stl': furniture,
        'furniture_and_room.stl': combined,
    }
    log_checks = {}
    for name, mesh in checks.items():
        chk = check_mesh_watertight(mesh, name)
        log_checks[name] = chk

    # Save comprehensive log
    log = {
        'project': resolver.config.get('project', {}).get('name', ''),
        'run_id': resolver.run_id,
        'furniture_source': furniture_path.name,
        'scene': {
            'size': {
                'length_x': round(float(scene_size[0]), 4),
                'width_y': round(float(scene_size[1]), 4),
                'height_z': round(float(scene_size[2]), 4),
            },
            'bounds_min': [round(float(v), 4) for v in furniture.bounds[0]],
            'bounds_max': [round(float(v), 4) for v in furniture.bounds[1]],
            'vertices': len(furniture.vertices),
            'faces': len(furniture.faces),
        },
        'room': {
            'size': {
                'length_x': round(float(room_size[0]), 4),
                'width_y': round(float(room_size[1]), 4),
                'height_z': round(float(room_size[2]), 4),
            },
            'bounds_min': [round(float(v), 4) for v in room_min],
            'bounds_max': [round(float(v), 4) for v in room_max],
            'vertices': len(room.vertices),
            'faces': len(room.faces),
            'vents_enabled': room_config.get('vents_enabled', False),
            'num_vents': len(room_config.get('vents', [])) if room_config.get('vents_enabled', False) else 0,
        },
        'room_config': {k: v for k, v in room_config.items() if not callable(v)},
        'watertight_checks': log_checks,
    }
    log_path = output_dir / "room_log.json"
    with open(log_path, 'w') as f:
        json.dump(log, f, indent=2)

    # --- Axis-aligned output for CFD ---
    create_axis_aligned_scene(resolver, furniture, room_config, verbose=verbose)

    print(f"\n  Output files:")
    print(f"    furniture.stl           -- placed furniture (from Step 4)")
    print(f"    room.stl                -- room box with vent openings")
    print(f"    furniture_and_room.stl  -- both combined (visualization)")
    print(f"    room_log.json           -- scene info, room dimensions, watertight checks")
    if abs(room_config.get('rotation_deg', 0.0)) > 1e-6:
        print(f"    axis_aligned/           -- all meshes rotated to axis-aligned frame (CFD)")

    print(f"\n{'='*60}")
    print(f"STEP 5 COMPLETE")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Step 5: Enclose Scene in Room")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--verbose", "-v", action="store_true", default=True)
    args = parser.parse_args()

    resolver = PathResolver(args.config)
    run_enclose_scene(resolver, verbose=args.verbose)


if __name__ == "__main__":
    main()