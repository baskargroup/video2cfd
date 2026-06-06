#!/usr/bin/env python3
"""
Step 4b: Interactive Scene Editor

Launches a web-based Three.js editor for human-in-the-loop editing of
placed STL objects. Allows deleting false positives, adjusting positions,
and correcting orientations.

Usage:
    python 4b_edit_scene.py --config ../configs/default.yaml
    python 4b_edit_scene.py --config ../configs/default.yaml --port 8080
    python 4b_edit_scene.py --config ../configs/default.yaml --rebuild  # rebuild only (no UI)

Protocol:
    1. Run steps 1-4 first to generate placements.
    2. Launch: python 4b_edit_scene.py --config ...
       - This CLEARS any previous edits and starts fresh from the Step 4 output.
    3. Edit the scene in the browser.
    4. Click "Save & Rebuild" to write scene_log_edited.json and regenerate STLs.
    5. Re-run step 5 if needed for room enclosure.

    Each launch of the editor is a clean session. Previous edit artifacts
    (scene_log_edited.json, *_edited.stl, etc.) are removed on startup.
    Use --rebuild to re-run the rebuild from an existing scene_log_edited.json
    without launching the UI (this does NOT clear previous edits).
"""

import argparse
import io
import json
import http.server
import shutil
import threading
import webbrowser
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import trimesh

from path_resolver import PathResolver
from stl_utils import NumpyEncoder, load_stl_mesh, get_bounds
from stl_procedural_placer import create_procedural_table


# ============================================================================
# PROCEDURAL MESH GENERATION
# ============================================================================

def make_procedural_table_bytes(dims: dict) -> bytes:
    """Generate a procedural table mesh from dimension params and return STL bytes."""
    mesh = create_procedural_table(
        width=float(dims.get("width", 1.0)),
        depth=float(dims.get("depth", 0.6)),
        height=float(dims.get("height", 0.75)),
        tabletop_thickness=float(dims.get("tabletop_thickness", 0.03)),
        leg_width=float(dims.get("leg_width", 0.05)),
        leg_inset=float(dims.get("leg_inset", 0.02)),
    )
    buf = io.BytesIO()
    mesh.export(buf, file_type="stl")
    return buf.getvalue()


# ============================================================================
# CLEANUP
# ============================================================================

def find_previous_edits(resolver: PathResolver) -> list:
    """Find all existing edit artifacts. Returns list of paths to remove."""
    stl_dir = resolver.get_stl_dir()
    found = []

    for cat_name in resolver.get_enabled_categories():
        cat_dir = resolver.get_stl_dir(cat_name)

        for f in [
            cat_dir / "scene_log_edited.json",
            cat_dir / f"{cat_name}_combined_edited.stl",
            cat_dir / f"{cat_name}_mannequins_edited.stl",
        ]:
            if f.exists():
                found.append(f)

        edited_individual = cat_dir / "individual_edited"
        if edited_individual.exists():
            found.append(edited_individual)

        mann_edited = cat_dir / "mannequins_edited"
        if mann_edited.exists():
            found.append(mann_edited)

    for f in [
        stl_dir / "furniture_edited.stl",
        stl_dir / "furniture_edited_with_mannequins.stl",
        stl_dir / "mannequin_adjustments.json",
        stl_dir / "added_objects.json",
        stl_dir / "added_procedural_objects.json",
        stl_dir / "room_config.json",
    ]:
        if f.exists():
            found.append(f)

    furniture_edited_dir = stl_dir / "furniture_individual_edited"
    if furniture_edited_dir.exists():
        found.append(furniture_edited_dir)

    added_dir = stl_dir / "added"
    if added_dir.exists():
        found.append(added_dir)

    added_proc_dir = stl_dir / "added_procedural"
    if added_proc_dir.exists():
        found.append(added_proc_dir)

    axis_aligned_dir = stl_dir / "axis_aligned"
    if axis_aligned_dir.exists():
        found.append(axis_aligned_dir)

    return found


def clean_previous_edits(resolver: PathResolver, artifacts: list):
    """Remove the given edit artifacts."""
    stl_dir = resolver.get_stl_dir()

    for path in artifacts:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

        try:
            label = str(path.relative_to(stl_dir))
        except ValueError:
            label = path.name
        print(f"    - {label}")

    print(f"  Cleaned {len(artifacts)} previous edit artifacts")


def regenerate_mannequins(resolver: PathResolver):
    """Regenerate mannequin STLs from original individual chair STLs.

    This runs on every editor launch (fresh or continue) to ensure the
    ``mannequins/`` directory always contains clean Step 4 output — i.e.
    mannequins placed with **config values only**, no editor deltas baked
    in.  The editor's ``applyMannAdjustments()`` then applies editor
    deltas (scale, offsets, rotation) on top of these clean files.

    Why this is necessary:
        Prior to the mannequins_edited/ split, rebuild_from_edits() would
        overwrite mannequins/ with rebuilt meshes that had editor deltas
        baked into the vertex positions.  On the next editor launch those
        files were loaded as the "original" starting point, and the same
        deltas were applied again — doubling the offsets.

    Source-of-truth contract:
        mannequins/         ← canonical Step 4 output (regenerated here)
        mannequins_edited/  ← rebuild output (never loaded by the editor)
        individual/         ← canonical chair STLs (never modified)
        scene_log.json      ← canonical placement metadata (never modified)
    """
    import importlib
    stl_module = importlib.import_module("4_create_stl")

    for cat_name in resolver.get_enabled_categories():
        cat_dir = resolver.get_stl_dir(cat_name)
        mann_dir = cat_dir / "mannequins"
        individual_dir = cat_dir / "individual"
        scene_log_path = cat_dir / "scene_log.json"

        mann_cfg = stl_module.get_mannequin_config(resolver, cat_name)
        if not mann_cfg:
            continue

        if not individual_dir.exists() or not scene_log_path.exists():
            continue

        stl_files = sorted(individual_dir.glob("*.stl"))
        if not stl_files:
            continue

        with open(scene_log_path) as f:
            log = json.load(f)

        placements = log.get("placements", [])
        if len(stl_files) < len(placements):
            print(f"  [{cat_name}] Mannequin regen skipped: "
                  f"{len(stl_files)} STLs < {len(placements)} placements")
            continue
        if len(stl_files) > len(placements):
            # Stale files from a prior run remain after re-running step 4 with
            # fewer accepted objects.  STLs are written in placement order, so
            # trimming the tail is safe.
            print(f"  [{cat_name}] {len(stl_files) - len(placements)} stale STL(s) in individual/ — using first {len(placements)}")
            stl_files = stl_files[:len(placements)]

        chair_meshes = []
        rotation_angles = []
        labels = []
        for stl_file, placement in zip(stl_files, placements):
            mesh = load_stl_mesh(stl_file)
            if mesh is None:
                print(f"  [{cat_name}] Warning: failed to load {stl_file.name}")
                continue
            chair_meshes.append(mesh)
            rotation_angles.append(placement.get("rotation_degrees", 0))
            labels.append(stl_file.stem)

        # No overrides → pure config values, 100% occupancy.
        # Editor adjustments are applied later by applyMannAdjustments().
        mann_meshes = stl_module.place_mannequins_on_meshes(
            resolver, cat_name, chair_meshes, rotation_angles,
            labels=labels, verbose=False, overrides=None
        )

        if mann_meshes:
            # Wipe and recreate to avoid stale files from prior rebuilds
            # that may have written a different number of mannequins.
            if mann_dir.exists():
                shutil.rmtree(mann_dir)
            mann_dir.mkdir(parents=True, exist_ok=True)
            for i, m in enumerate(mann_meshes):
                m.export(str(mann_dir / f"mannequin_{i+1:03d}.stl"))
            combined = trimesh.util.concatenate(mann_meshes)
            combined.export(str(cat_dir / f"{cat_name}_mannequins.stl"))
            print(f"  [{cat_name}] Regenerated {len(mann_meshes)} mannequins")


# ============================================================================
# MANIFEST
# ============================================================================

def build_manifest(resolver: PathResolver) -> dict:
    """Build a manifest of all categories and their individual STL files."""
    categories = []

    for cat_name in resolver.get_enabled_categories():
        cat_dir = resolver.get_stl_dir(cat_name)
        individual_dir = cat_dir / "individual"
        scene_log_path = cat_dir / "scene_log.json"

        if not individual_dir.exists() or not scene_log_path.exists():
            continue

        stl_files = sorted([f.name for f in individual_dir.glob("*.stl")])
        if not stl_files:
            continue

        # Collect mannequin STLs if present
        mann_dir = cat_dir / "mannequins"
        mannequin_files = sorted([f.name for f in mann_dir.glob("*.stl")]) if mann_dir.exists() else []

        categories.append({
            "name": cat_name,
            "objects": stl_files,
            "mannequins": mannequin_files,
            "count": len(stl_files),
        })

    return {"categories": categories}


# ============================================================================
# SAVE
# ============================================================================

def save_edits(resolver: PathResolver, edits: dict) -> dict:
    """
    Save edited placements to scene_log_edited.json per category.

    Each placement from the editor includes:
      - All original fields (source, position, rotation_degrees, etc.)
      - _deleted: bool
      - _original_position / _original_rotation: for computing deltas during rebuild

    Also saves mannequin_adjustments if present in the payload.
    """
    saved_categories = []

    # Save mannequin adjustments if present (global, not per-category)
    mann_adj = edits.pop("_mannequin_adjustments", None)
    if mann_adj is not None:
        adj_path = resolver.get_stl_dir() / "mannequin_adjustments.json"
        with open(adj_path, "w") as f:
            json.dump(mann_adj, f, indent=2)
        print(f"  Saved mannequin adjustments -> {adj_path}")

    # Save room config overrides if present
    room_cfg = edits.pop("_room_config", None)
    if room_cfg is not None:
        rc_path = resolver.get_stl_dir() / "room_config.json"
        with open(rc_path, "w") as f:
            json.dump(room_cfg, f, indent=2)
        print(f"  Saved room config -> {rc_path}")

    # Save manually added objects (from template library)
    added_objects = edits.pop("_added_objects", None)
    if added_objects:
        added_path = resolver.get_stl_dir() / "added_objects.json"
        with open(added_path, "w") as f:
            json.dump(added_objects, f, indent=2, cls=NumpyEncoder)
        print(f"  Saved {len(added_objects)} manually added objects -> {added_path}")

    # Save manually added procedural objects
    added_proc_objects = edits.pop("_added_procedural_objects", None)
    if added_proc_objects:
        added_proc_path = resolver.get_stl_dir() / "added_procedural_objects.json"
        with open(added_proc_path, "w") as f:
            json.dump(added_proc_objects, f, indent=2, cls=NumpyEncoder)
        print(f"  Saved {len(added_proc_objects)} added procedural objects -> {added_proc_path}")

    for cat_name, cat_data in edits.items():
        cat_dir = resolver.get_stl_dir(cat_name)
        cat_dir.mkdir(parents=True, exist_ok=True)

        placements = cat_data.get("placements", [])

        edited_log = {
            "category": cat_name,
            "mode": cat_data.get("scene_log", {}).get("mode", "template"),
            "placements": [],
            "deleted": [],
        }

        for p in placements:
            # Strip private underscore fields for the saved log
            entry = {k: v for k, v in p.items() if not k.startswith("_")}

            if p.get("_deleted", False):
                edited_log["deleted"].append(entry)
            else:
                edited_log["placements"].append(entry)

        edited_path = cat_dir / "scene_log_edited.json"
        with open(edited_path, "w") as f:
            json.dump(edited_log, f, indent=2, cls=NumpyEncoder)

        n_kept = len(edited_log["placements"])
        n_deleted = len(edited_log["deleted"])
        saved_categories.append(cat_name)
        print(f"  [{cat_name}] Saved: {n_kept} kept, {n_deleted} deleted -> {edited_path}")

    return {
        "ok": True,
        "message": f"Saved edits for: {', '.join(saved_categories)}",
    }


def load_mannequin_adjustments(resolver: PathResolver) -> Optional[dict]:
    """Load mannequin adjustments saved by the editor, if any."""
    adj_path = resolver.get_stl_dir() / "mannequin_adjustments.json"
    if adj_path.exists():
        with open(adj_path) as f:
            return json.load(f)
    return None


# ============================================================================
# REBUILD
# ============================================================================

def rebuild_from_edits(resolver: PathResolver) -> dict:
    """
    Rebuild combined STLs from scene_log_edited.json.

    For each kept placement:
    1. Load the original individual STL (from individual/)
    2. Compute position/rotation delta vs the original scene_log.json
    3. Apply rotation around bbox center, then translation (matching Three.js)
    4. Write edited individual STLs and combined STLs
    """
    stl_dir = resolver.get_stl_dir()
    all_meshes = []
    all_names = []
    results = []

    for cat_name in resolver.get_enabled_categories():
        cat_dir = resolver.get_stl_dir(cat_name)
        edited_path = cat_dir / "scene_log_edited.json"
        original_path = cat_dir / "scene_log.json"

        if not edited_path.exists():
            # No edits for this category — use original as-is
            individual_dir = cat_dir / "individual"
            if individual_dir.exists():
                for stl_file in sorted(individual_dir.glob("*.stl")):
                    mesh = load_stl_mesh(stl_file)
                    if mesh is not None:
                        all_meshes.append(mesh)
                        all_names.append(stl_file.stem)
                results.append(f"{cat_name}: {len(list(individual_dir.glob('*.stl')))} (unedited)")
            continue

        if not original_path.exists():
            continue

        with open(edited_path) as f:
            edited_log = json.load(f)
        with open(original_path) as f:
            original_log = json.load(f)

        # Build lookup: source -> (original_index, original_placement)
        original_by_source = {}
        for i, p in enumerate(original_log.get("placements", [])):
            original_by_source[p["source"]] = (i, p)

        individual_dir = cat_dir / "individual"
        if not individual_dir.exists():
            continue

        cat_meshes = []
        cat_names = []

        for placement in edited_log.get("placements", []):
            source = placement.get("source", "")

            if source not in original_by_source:
                print(f"    Warning: source '{source}' not found in original log, skipping")
                continue

            orig_idx, orig_p = original_by_source[source]
            stl_name = f"{cat_name}_{orig_idx + 1:03d}.stl"
            stl_path = individual_dir / stl_name

            if not stl_path.exists():
                print(f"    Warning: {stl_path} not found, skipping")
                continue

            # For procedural objects: regenerate mesh if dimensions changed
            if placement.get("mode") == "procedural" and placement.get("dimensions"):
                saved_dims = placement["dimensions"]
                orig_dims = orig_p.get("dimensions", {})
                dims_changed = any(
                    abs(float(saved_dims.get(k, 0)) - float(orig_dims.get(k, 0))) > 1e-6
                    for k in ["width", "depth", "height", "tabletop_thickness", "leg_width", "leg_inset"]
                )
                if dims_changed:
                    # Regenerate mesh from new dimensions, then apply original placement,
                    # so the subsequent delta logic works identically to the template path.
                    regen_mesh = create_procedural_table(
                        width=float(saved_dims.get("width", 1.0)),
                        depth=float(saved_dims.get("depth", 0.6)),
                        height=float(saved_dims.get("height", 0.75)),
                        tabletop_thickness=float(saved_dims.get("tabletop_thickness", 0.03)),
                        leg_width=float(saved_dims.get("leg_width", 0.05)),
                        leg_inset=float(saved_dims.get("leg_inset", 0.02)),
                    )
                    # Apply original rotation + position to match step 4 output.
                    # Step 4 uses `@ rot_z.T` (standard CCW rotation), so we must
                    # use the same convention — NOT the bare matrix (which is CW).
                    orig_rot_rad = np.radians(orig_p.get("rotation_degrees", 0))
                    rc, rs = np.cos(orig_rot_rad), np.sin(orig_rot_rad)
                    rot_z = np.array([[rc, -rs, 0], [rs, rc, 0], [0, 0, 1]])
                    regen_mesh.vertices = regen_mesh.vertices @ rot_z.T
                    regen_mesh.vertices += np.array(orig_p.get("position", [0, 0, 0]))
                    mesh = regen_mesh
                    print(f"    [{cat_name}] Regenerated {source} with new dims "
                          f"{saved_dims.get('width', 0):.2f}x{saved_dims.get('depth', 0):.2f}x{saved_dims.get('height', 0):.2f}m")
                else:
                    mesh = load_stl_mesh(stl_path)
            else:
                mesh = load_stl_mesh(stl_path)

            if mesh is None:
                continue

            # Delta from original placement
            orig_pos = np.array(orig_p.get("position", [0, 0, 0]))
            orig_rot = orig_p.get("rotation_degrees", 0)
            new_pos = np.array(placement.get("position", orig_pos.tolist()))
            new_rot = placement.get("rotation_degrees", orig_rot)

            delta_pos = new_pos - orig_pos
            delta_rot_rad = np.radians(new_rot - orig_rot)

            # Rotation around bounding box center (matches Three.js editor pivot)
            if abs(delta_rot_rad) > 1e-6:
                bbox_center = (mesh.vertices.min(axis=0) + mesh.vertices.max(axis=0)) / 2.0
                mesh.vertices -= bbox_center
                c, s = np.cos(delta_rot_rad), np.sin(delta_rot_rad)
                rot_matrix = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
                mesh.vertices = mesh.vertices @ rot_matrix.T
                mesh.vertices += bbox_center

            # Translation
            if np.linalg.norm(delta_pos) > 1e-6:
                mesh.vertices += delta_pos

            cat_meshes.append(mesh)
            cat_names.append(stl_path.stem)

        # Save edited individual meshes — clear first so deleted objects don't linger
        edited_individual_dir = cat_dir / "individual_edited"
        if edited_individual_dir.exists():
            for old_f in edited_individual_dir.glob("*.stl"):
                old_f.unlink()
        edited_individual_dir.mkdir(parents=True, exist_ok=True)
        for mesh, name in zip(cat_meshes, cat_names):
            mesh.export(str(edited_individual_dir / f"{name}.stl"))

        # Save category combined
        if cat_meshes:
            combined = trimesh.util.concatenate(cat_meshes)
            combined_path = cat_dir / f"{cat_name}_combined_edited.stl"
            combined.export(str(combined_path))
            print(f"  [{cat_name}] Rebuilt: {len(cat_meshes)} objects -> {combined_path}")

            all_meshes.extend(cat_meshes)
            all_names.extend(cat_names)
            results.append(f"{cat_name}: {len(cat_meshes)}")

    # Load and place manually added objects from the template library
    added_objects_path = stl_dir / "added_objects.json"
    if added_objects_path.exists():
        with open(added_objects_path) as f:
            added_objects = json.load(f)

        added_dir = stl_dir / "added"
        added_dir.mkdir(parents=True, exist_ok=True)

        for i, obj in enumerate(added_objects):
            if obj.get("deleted", False):
                continue

            # Support both template-library objects and copied individual STLs
            individual_stl = obj.get("individual_stl_path")
            if individual_stl:
                template_path = stl_dir / individual_stl
            else:
                template_file = obj.get("template_file", "")
                template_path = resolver.stl_templates_dir / template_file

            if not template_path.exists():
                print(f"    Warning: STL '{template_path.name}' not found, skipping")
                continue

            mesh = trimesh.load(str(template_path))
            if isinstance(mesh, trimesh.Scene):
                mesh = trimesh.util.concatenate(
                    [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
                )

            # Center mesh at origin, apply scale, rotation, then position
            mesh.vertices -= mesh.bounds.mean(axis=0)
            scale = obj.get("scale", 1.0)
            mesh.vertices *= scale

            rot_deg = obj.get("rotation_degrees", 0.0)
            if abs(rot_deg) > 1e-6:
                rot_rad = np.radians(rot_deg)
                c, s = np.cos(rot_rad), np.sin(rot_rad)
                rot_matrix = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
                mesh.vertices = mesh.vertices @ rot_matrix.T

            position = np.array(obj.get("position", [0, 0, 0]))
            mesh.vertices += position

            name = f"added_{i+1:03d}_{template_file.replace('.stl', '')}"
            mesh.export(str(added_dir / f"{name}.stl"))
            all_meshes.append(mesh)
            all_names.append(name)

        n_added = sum(1 for o in added_objects if not o.get("deleted", False))
        if n_added > 0:
            print(f"\n  Added objects: {n_added} from template library -> {added_dir}")
            results.append(f"added: {n_added}")

    # Load and place manually added procedural tables
    added_proc_path = stl_dir / "added_procedural_objects.json"
    if added_proc_path.exists():
        with open(added_proc_path) as f:
            added_proc_objects = json.load(f)

        added_proc_dir = stl_dir / "added_procedural"
        added_proc_dir.mkdir(parents=True, exist_ok=True)

        n_added_proc = 0
        for i, obj in enumerate(added_proc_objects):
            if obj.get("deleted", False):
                continue

            dims = obj.get("dimensions", {})
            mesh = create_procedural_table(
                width=float(dims.get("width", 1.0)),
                depth=float(dims.get("depth", 0.6)),
                height=float(dims.get("height", 0.75)),
                tabletop_thickness=float(dims.get("tabletop_thickness", 0.03)),
                leg_width=float(dims.get("leg_width", 0.05)),
                leg_inset=float(dims.get("leg_inset", 0.02)),
            )

            # Center at origin (table is already centered XY, Z=0 at bottom)
            rot_deg = obj.get("rotation_degrees", 0.0)
            if abs(rot_deg) > 1e-6:
                rot_rad = np.radians(rot_deg)
                c, s = np.cos(rot_rad), np.sin(rot_rad)
                rot_matrix = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
                mesh.vertices = mesh.vertices @ rot_matrix.T

            position = np.array(obj.get("position", [0, 0, 0]))
            mesh.vertices += position

            name = f"added_procedural_{i+1:03d}_table"
            mesh.export(str(added_proc_dir / f"{name}.stl"))
            all_meshes.append(mesh)
            all_names.append(name)
            n_added_proc += 1

        if n_added_proc > 0:
            print(f"\n  Added procedural tables: {n_added_proc} -> {added_proc_dir}")
            results.append(f"added_procedural: {n_added_proc}")

    # Combined furniture_edited.stl (furniture only, no mannequins)
    if all_meshes:
        furniture = trimesh.util.concatenate(all_meshes)
        furniture_path = stl_dir / "furniture_edited.stl"
        furniture.export(str(furniture_path))
        print(f"\n  Combined: {len(all_meshes)} objects -> {furniture_path}")

        furniture_dir = stl_dir / "furniture_individual_edited"
        if furniture_dir.exists():
            for old_f in furniture_dir.glob("*.stl"):
                old_f.unlink()
        furniture_dir.mkdir(parents=True, exist_ok=True)
        for mesh, name in zip(all_meshes, all_names):
            mesh.export(str(furniture_dir / f"{name}.stl"))
        print(f"  Individual: {len(all_meshes)} STLs in {furniture_dir}")

        # ---- Mannequin rebuild ----
        # Mannequins are placed from the TEMPLATE (not from mannequins/ files)
        # using edited chair positions + editor adjustments as deltas on top of
        # config values.  Output goes to mannequins_edited/ — NEVER mannequins/,
        # which is reserved for clean Step 4 originals (see regenerate_mannequins).
        from importlib import import_module
        stl_module = import_module('4_create_stl')

        mann_overrides = load_mannequin_adjustments(resolver)

        all_mannequin_meshes = []
        for cat_name in resolver.get_enabled_categories():
            cat_dir = resolver.get_stl_dir(cat_name)

            # Collect the edited meshes and rotation angles for this category
            cat_edited_meshes = []
            cat_rotation_angles = []
            cat_labels = []

            edited_path = cat_dir / "scene_log_edited.json"
            original_path = cat_dir / "scene_log.json"

            if edited_path.exists() and original_path.exists():
                with open(edited_path) as f:
                    edited_log = json.load(f)
                with open(original_path) as f:
                    original_log = json.load(f)

                original_by_source = {}
                for i, p in enumerate(original_log.get("placements", [])):
                    original_by_source[p["source"]] = (i, p)

                edited_individual_dir = cat_dir / "individual_edited"
                for placement in edited_log.get("placements", []):
                    source = placement.get("source", "")
                    if source not in original_by_source:
                        continue
                    orig_idx, _ = original_by_source[source]
                    stl_name = f"{cat_name}_{orig_idx + 1:03d}.stl"
                    stl_path = edited_individual_dir / stl_name
                    if stl_path.exists():
                        mesh = load_stl_mesh(stl_path)
                        if mesh is not None:
                            cat_edited_meshes.append(mesh)
                            cat_rotation_angles.append(placement.get("rotation_degrees", 0))
                            cat_labels.append(stl_path.stem)
            else:
                # No edits — use original individual meshes
                individual_dir = cat_dir / "individual"
                scene_log_path = cat_dir / "scene_log.json"
                if individual_dir.exists() and scene_log_path.exists():
                    with open(scene_log_path) as f:
                        log = json.load(f)
                    for i, stl_file in enumerate(sorted(individual_dir.glob("*.stl"))):
                        mesh = load_stl_mesh(stl_file)
                        if mesh is not None:
                            rot = log["placements"][i].get("rotation_degrees", 0) if i < len(log.get("placements", [])) else 0
                            cat_edited_meshes.append(mesh)
                            cat_rotation_angles.append(rot)
                            cat_labels.append(stl_file.stem)

            if cat_edited_meshes:
                mann_meshes = stl_module.place_mannequins_on_meshes(
                    resolver, cat_name, cat_edited_meshes,
                    cat_rotation_angles, cat_labels, verbose=False,
                    overrides=mann_overrides,
                )

                # IMPORTANT: save to mannequins_edited/, NEVER mannequins/.
                # mannequins/ is the canonical Step 4 output regenerated on
                # every editor launch.  Writing here would corrupt it.
                mann_edited_dir = cat_dir / "mannequins_edited"
                mann_edited_dir.mkdir(parents=True, exist_ok=True)
                for old_f in mann_edited_dir.glob("*.stl"):
                    old_f.unlink()

                if mann_meshes:
                    mann_combined = trimesh.util.concatenate(mann_meshes)
                    mann_combined.export(str(cat_dir / f"{cat_name}_mannequins_edited.stl"))
                    for mi, m in enumerate(mann_meshes):
                        m.export(str(mann_edited_dir / f"mannequin_{mi+1:03d}.stl"))
                    print(f"  [{cat_name}] Mannequins: {len(mann_meshes)} (occupancy-filtered) -> {mann_edited_dir}")

                all_mannequin_meshes.extend(mann_meshes)

        if all_mannequin_meshes:
            combined_with_mann = trimesh.util.concatenate(all_meshes + all_mannequin_meshes)
            mann_path = stl_dir / "furniture_edited_with_mannequins.stl"
            combined_with_mann.export(str(mann_path))
            print(f"  With mannequins: {len(all_mannequin_meshes)} mannequins -> {mann_path}")

            # Save individual mannequin STLs into furniture_individual_edited/
            for j, m in enumerate(all_mannequin_meshes):
                m.export(str(furniture_dir / f"mannequin_{j+1:03d}.stl"))
            print(f"  Mannequin individuals: {len(all_mannequin_meshes)} STLs in {furniture_dir}")

    return {
        "ok": True,
        "message": f"Rebuilt: {', '.join(results)}. Total: {len(all_meshes)} objects.",
    }


# ============================================================================
# HTTP SERVER
# ============================================================================

class EditorHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the scene editor."""

    def __init__(self, resolver: PathResolver, editor_dir: Path,
                 continue_editing: bool = False, *args, **kwargs):
        self.resolver = resolver
        self.editor_dir = editor_dir
        self.continue_editing = continue_editing
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        if '/stl/' not in str(args[0]):
            super().log_message(format, *args)

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/' or path == '/index.html':
            self._serve_file(self.editor_dir / 'index.html', 'text/html')

        elif path == '/api/manifest':
            manifest = build_manifest(self.resolver)
            self._send_json(manifest)

        elif path.startswith('/api/scene_log/'):
            cat_name = path.split('/')[-1]
            cat_dir = self.resolver.get_stl_dir(cat_name)
            original_path = cat_dir / "scene_log.json"

            if original_path.exists():
                self._serve_file(original_path, 'application/json')
            else:
                self._send_json({"error": f"No scene_log for {cat_name}"}, 404)

        elif path == '/api/mannequin_config':
            # Return current mannequin config for all categories
            mann_configs = {}
            for cat_name in self.resolver.get_enabled_categories():
                from importlib import import_module
                stl_module = import_module('4_create_stl')
                cfg = stl_module.get_mannequin_config(self.resolver, cat_name)
                if cfg:
                    mann_configs[cat_name] = {
                        'scale_factor': cfg.get('scale_factor', 1.0),
                        'position_offset': cfg.get('position_offset', [0, 0, 0]),
                    }
            self._send_json(mann_configs)

        elif path == '/api/room_config':
            import importlib
            step5 = importlib.import_module('5_enclose_scene')
            rc = self.resolver.config.get('room', {})
            floor_tilt_deg = rc.get('floor_tilt_deg', None)
            # Auto-compute tilt from chair positions so the editor preview
            # can display it even when floor_tilt_deg is null in the config.
            auto_tilt_deg = None
            if floor_tilt_deg is None:
                try:
                    import numpy as np
                    slope = step5._compute_floor_tilt_from_placements(
                        self.resolver, rc.get('rotation_deg', 0.0), np.zeros(3))
                    auto_tilt_deg = float(np.degrees(np.arctan(slope)))
                except Exception:
                    pass
            self._send_json({
                'rotation_deg':   rc.get('rotation_deg', 0),
                'padding':        rc.get('padding', 0.1),
                'height':         rc.get('height', 1.0),
                'floor_type':     rc.get('floor_type', 'flat'),
                'stage_depth':    rc.get('stage_depth', 0.2),
                'stage_height':   rc.get('stage_height', 0.05),
                'flat_depth':     rc.get('flat_depth', 0.3),
                'floor_tilt_deg': floor_tilt_deg,
                'auto_tilt_deg':  auto_tilt_deg,  # computed preview value when tilt is auto
            })

        elif path == '/api/previous_edits':
            # Return saved edit state for --continue mode
            if not self.continue_editing:
                self._send_json({"continue": False})
            else:
                stl_dir = self.resolver.get_stl_dir()
                result = {"continue": True, "categories": {}}

                # Per-category edited placements
                for cat_name in self.resolver.get_enabled_categories():
                    cat_dir = self.resolver.get_stl_dir(cat_name)
                    edited_path = cat_dir / "scene_log_edited.json"
                    if edited_path.exists():
                        with open(edited_path) as f:
                            result["categories"][cat_name] = json.load(f)

                # Mannequin adjustments
                mann_path = stl_dir / "mannequin_adjustments.json"
                if mann_path.exists():
                    with open(mann_path) as f:
                        result["mannequin_adjustments"] = json.load(f)

                # Room config
                rc_path = stl_dir / "room_config.json"
                if rc_path.exists():
                    with open(rc_path) as f:
                        result["room_config"] = json.load(f)

                # Added objects
                added_path = stl_dir / "added_objects.json"
                if added_path.exists():
                    with open(added_path) as f:
                        result["added_objects"] = json.load(f)

                # Added procedural objects
                added_proc_path = stl_dir / "added_procedural_objects.json"
                if added_proc_path.exists():
                    with open(added_proc_path) as f:
                        result["added_procedural_objects"] = json.load(f)

                self._send_json(result)

        elif path == '/api/templates':
            templates = self.resolver.list_stl_templates()
            self._send_json({"templates": templates})

        elif path.startswith('/api/template_info/'):
            filename = path.split('/')[-1]
            template_path = self.resolver.stl_templates_dir / filename
            if template_path.exists():
                try:
                    mesh = trimesh.load(str(template_path))
                    if isinstance(mesh, trimesh.Scene):
                        mesh = trimesh.util.concatenate(
                            [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
                        )
                    size = mesh.bounds[1] - mesh.bounds[0]
                    self._send_json({
                        "filename": filename,
                        "size": size.tolist(),
                        "vertices": len(mesh.vertices),
                        "faces": len(mesh.faces),
                    })
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
                self._send_json({"error": f"Template not found: {filename}"}, 404)

        elif path.startswith('/template_stl/'):
            filename = path[len('/template_stl/'):]
            template_path = self.resolver.stl_templates_dir / filename
            if template_path.exists():
                self._serve_file(template_path, 'application/octet-stream')
            else:
                self.send_error(404, f"Template not found: {filename}")

        elif path.startswith('/stl/'):
            rel = path[len('/stl/'):]
            stl_path = self.resolver.get_stl_dir() / rel
            if stl_path.exists():
                self._serve_file(stl_path, 'application/octet-stream')
            else:
                self.send_error(404, f"STL not found: {rel}")

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        if path == '/api/save':
            try:
                edits = json.loads(body)
                result = save_edits(self.resolver, edits)
                self._send_json(result)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

        elif path == '/api/save_and_rebuild':
            try:
                edits = json.loads(body)
                save_edits(self.resolver, edits)
                rebuild_result = rebuild_from_edits(self.resolver)
                self._send_json({
                    "ok": True,
                    "message": f"Saved and rebuilt. {rebuild_result['message']}",
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

        elif path == '/api/generate_procedural_table':
            try:
                params = json.loads(body) if body else {}
                dims = params.get("dimensions", params)
                stl_bytes = make_procedural_table_bytes(dims)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', len(stl_bytes))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(stl_bytes)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

        elif path == '/api/compute_floor_tilt':
            try:
                import importlib, numpy as np
                step5 = importlib.import_module('5_enclose_scene')
                params = json.loads(body) if body else {}
                rotation_deg = float(params.get('rotation_deg', 0.0))
                stage_depth  = float(params.get('stage_depth',  0.2))
                flat_depth   = float(params.get('flat_depth',   0.3))
                padding      = float(params.get('padding',      0.1))

                # Load all chair positions and rotate into aligned frame
                positions = []
                for cat_name in self.resolver.get_enabled_categories():
                    log_path = self.resolver.get_stl_dir(cat_name) / "scene_log_edited.json"
                    if not log_path.exists():
                        log_path = self.resolver.get_stl_dir(cat_name) / "scene_log.json"
                    if not log_path.exists():
                        continue
                    with open(log_path) as f:
                        log = json.load(f)
                    for p in log.get("placements", []):
                        pos = p.get("position", [0, 0, 0])
                        positions.append([float(pos[0]), float(pos[1]), float(pos[2])])

                if len(positions) < 2:
                    self._send_json({"ok": False, "error": "Not enough placement data"})
                else:
                    pts = np.array(positions)
                    if abs(rotation_deg) > 1e-6:
                        rad = np.radians(-rotation_deg)
                        c, s = np.cos(rad), np.sin(rad)
                        pivot = pts.mean(axis=0)
                        xy = pts[:, :2] - pivot[:2]
                        pts = np.column_stack([
                            xy[:, 0] * c - xy[:, 1] * s + pivot[0],
                            xy[:, 0] * s + xy[:, 1] * c + pivot[1],
                            pts[:, 2],
                        ])
                    # y_min = start of inclined section in aligned frame
                    y_min_chairs = float(pts[:, 1].min())
                    y_min = y_min_chairs - padding + stage_depth + flat_depth
                    incline_pts = pts[pts[:, 1] >= y_min]
                    if len(incline_pts) < 2:
                        # Fall back to all chairs if filter leaves too few
                        incline_pts = pts
                    y_vals, z_vals = incline_pts[:, 1], incline_pts[:, 2]
                    n = len(y_vals)
                    denom = n * np.sum(y_vals**2) - np.sum(y_vals)**2
                    slope = (n * np.sum(y_vals * z_vals) - np.sum(y_vals) * np.sum(z_vals)) / denom \
                            if abs(denom) > 1e-12 else 0.0
                    tilt_deg = float(np.degrees(np.arctan(slope)))
                    self._send_json({
                        "ok": True,
                        "tilt_deg": round(tilt_deg, 2),
                        "n_chairs": int(len(incline_pts)),
                    })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

        else:
            self.send_error(404)

    def _serve_file(self, filepath: Path, content_type: str):
        try:
            data = filepath.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(data))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def _send_json(self, data, status=200):
        body = json.dumps(data, cls=NumpyEncoder).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


def run_server(resolver: PathResolver, port: int = 8051,
               continue_editing: bool = False):
    """Start the editor HTTP server.

    *continue_editing* — if True, skip cleanup and serve the previously
    saved edits so the user can pick up where they left off.
    """
    editor_dir = Path(__file__).parent / "editor"

    if not editor_dir.exists():
        raise FileNotFoundError(f"Editor directory not found: {editor_dir}")

    artifacts = find_previous_edits(resolver)

    if artifacts:
        if continue_editing:
            # Explicit --continue: skip prompt
            print(f"\nContinuing from {len(artifacts)} previous edit artifacts (no cleanup).")
        else:
            # Interactive prompt: let the user choose
            stl_dir = resolver.get_stl_dir()
            print(f"\nFound {len(artifacts)} previous edit artifacts:")
            for p in artifacts:
                try:
                    label = str(p.relative_to(stl_dir))
                except ValueError:
                    label = p.name
                print(f"    - {label}")

            print("\nOptions:")
            print("  [c] Continue editing (keep previous edits)")
            print("  [f] Start fresh (delete previous edits)")
            print("  [q] Quit (preserve edits, don't launch editor)")
            answer = input("\nChoice [c/f/q]: ").strip().lower()

            if answer == 'c':
                continue_editing = True
                print("Continuing from previous edits.")
            elif answer == 'f':
                clean_previous_edits(resolver, artifacts)
                print("Starting fresh.")
            else:
                print("Aborted. Previous edits preserved.")
                return
    else:
        if continue_editing:
            print("\nNo previous edits found — starting fresh instead.")
        else:
            print("\nNo previous edits found. Starting fresh.")
        continue_editing = False

    # Always regenerate mannequins from original chair STLs.
    # Previous rebuilds may have overwritten mannequins/ with adjusted versions
    # (baked-in editor offsets + edited chair positions).  The editor applies
    # adjustments on top of the loaded STLs, so loading pre-adjusted files
    # causes double-application.  Regenerating ensures clean starting files.
    regenerate_mannequins(resolver)

    class ThreadedHTTPServer(
        http.server.ThreadingHTTPServer if hasattr(http.server, 'ThreadingHTTPServer')
        else http.server.HTTPServer
    ):
        pass

    handler = partial(EditorHandler, resolver, editor_dir, continue_editing)
    server = ThreadedHTTPServer(('0.0.0.0', port), handler)
    server.timeout = 0.5  # Don't block indefinitely on socket ops

    mode_label = "CONTINUE" if continue_editing else "FRESH"
    url = f"http://localhost:{port}"
    print(f"\n{'='*60}")
    print(f"  SCENE EDITOR ({mode_label})")
    print(f"  Open in browser: {url}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*60}\n")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nShutting down editor...")
        server.shutdown()
        server.server_close()
        print("Editor stopped.")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Step 4b: Interactive Scene Editor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  (default)     Start FRESH from Step 4 output. Previous edits are cleared.
  --continue    Continue editing from previous session. Loads saved edits
                (deletions, transforms, mannequins, room config) so you
                can pick up where you left off.
  --rebuild     Regenerate STLs from scene_log_edited.json (no UI).

Examples:
  python 4b_edit_scene.py --config ../configs/default.yaml
  python 4b_edit_scene.py --config ../configs/default.yaml --continue
  python 4b_edit_scene.py --config ../configs/default.yaml --port 8080
  python 4b_edit_scene.py --config ../configs/default.yaml --rebuild
        """
    )
    parser.add_argument("--config", type=str, required=True, help="Path to config YAML")
    parser.add_argument("--port", type=int, default=8051, help="HTTP server port (default: 8051)")
    parser.add_argument("--continue", dest="continue_editing", action="store_true",
                        help="Continue from previous edits instead of starting fresh")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild STLs from existing scene_log_edited.json (no UI, no cleanup)")

    args = parser.parse_args()
    resolver = PathResolver(args.config)

    print(f"Project: {resolver.config['project']['name']}")
    print(f"Run: {resolver.run_id}")
    print(f"STL dir: {resolver.get_stl_dir()}")

    # Verify Step 4 output exists
    manifest = build_manifest(resolver)
    if not manifest["categories"]:
        print("\nERROR: No Step 4 output found. Run steps 1-4 first.")
        print(f"  Expected: {resolver.get_stl_dir()}/<category>/individual/*.stl")
        return

    total = sum(c["count"] for c in manifest["categories"])
    cats = [f"{c['name']}({c['count']})" for c in manifest["categories"]]
    print(f"Found: {total} objects across {len(manifest['categories'])} categories: {', '.join(cats)}")

    if args.rebuild:
        print("\nRebuilding from edited placements...")
        result = rebuild_from_edits(resolver)
        print(f"\n{result['message']}")
    else:
        run_server(resolver, port=args.port,
                   continue_editing=args.continue_editing)


if __name__ == "__main__":
    main()
