#!/usr/bin/env python3
"""Prepare a complete OpenFOAM case from video2cfd STL outputs.

This script is self-contained: it generates the OpenFOAM dictionaries directly
from a YAML config and copies/scales the STL geometry into constant/triSurface;
it does not need the archived production case folders.

The generated case follows Section 3.5 of the paper (OpenFOAM v2412):

  * blockMesh background mesh (0.08 m cells by default) + snappyHexMesh, with
    distance refinement around the furniture and one searchableBox refinement
    region below every supply (inlet) patch;
  * inlet/outlet patches cut out of the ceiling with topoSet + createPatch;
  * simpleFoam (SIMPLEC, fixed iteration budget) followed by the passive-scalar
    decay run with scalarTransportFoamTurbulent, monitored with point probes.

All geometric checks (patches on the ceiling, monitors inside the room) are
done before anything is written, and the case is written to a temporary
sibling directory first, so a failed run never leaves a half-written case.
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required. Install with: pip install pyyaml")

try:
    import numpy as np
    import trimesh
except ImportError:
    sys.exit("ERROR: trimesh (and numpy) is required. Install with: pip install trimesh")

Vector = Tuple[float, float, float]

SCRIPT_DIR = Path(__file__).resolve().parent
CFD_DIR = SCRIPT_DIR.parent
REPO_ROOT = CFD_DIR.parent
ALLRUN_TEMPLATE = CFD_DIR / "templates" / "Allrun"

# A patch rectangle must lie at least this fraction inside the ceiling.
MIN_PATCH_CEILING_OVERLAP = 0.99

# The topoSet selection boxes are kept this far (m) inside the room footprint,
# so that a patch rectangle that touches a wall never selects faces of that wall.
SELECTION_WALL_CLEARANCE = 1.0e-3

# Position of locationInMesh inside its background cell, as fractions of the
# cell size. The values are irregular on purpose: they keep the point off the
# vertices and faces of the background cells and of their 2^n subdivisions.
LOCATION_CELL_FRACTIONS = (0.3467, 0.4717, 0.5907)

DEFAULT_FURNITURE_DISTANCE_REFINEMENT = [[0.02, 4], [0.05, 3]]
SUPPORTED_DECOMPOSITION_METHODS = ("scotch", "simple", "hierarchical")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def foam_header(cls: str, obj: str) -> str:
    return (
        "FoamFile\n"
        "{\n"
        "    version     2.0;\n"
        "    format      ascii;\n"
        f"    class       {cls};\n"
        f"    object      {obj};\n"
        "}\n"
    )


def fmt(value: Any) -> str:
    """Format a number for an OpenFOAM dictionary (integers stay integers)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    value = float(value)
    if value == 0.0:
        return "0"
    return f"{value:.9g}"


def vec(values: Iterable[Any]) -> str:
    return "(" + " ".join(fmt(v) for v in values) + ")"


def write(path: Path, text: str) -> None:
    """Write a text file with LF line endings on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> "SystemExit":
    return SystemExit(f"ERROR: {message}")


def _num(value: Any) -> Optional[float]:
    """Return value as a finite float, or None. Accepts numeric strings such as '1e-5'."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _num_list(value: Any, length: int) -> Optional[List[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    numbers = [_num(v) for v in value]
    if any(n is None for n in numbers):
        return None
    return [float(n) for n in numbers]


def _section(cfg: Dict[str, Any], name: str, errors: List[str]) -> Dict[str, Any]:
    value = cfg.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"'{name}' must be a mapping")
        return {}
    return value


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(cfg_path: Path) -> Dict[str, Any]:
    if not cfg_path.is_file():
        raise fail(f"config does not exist: {cfg_path}")
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise fail(f"cannot parse {cfg_path}: {exc}")
    if not isinstance(cfg, dict):
        raise fail(f"{cfg_path} does not contain a YAML mapping")
    return cfg


def normalise_config(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Validate the YAML config and fill in defaults.

    Returns (settings, errors, warnings). Only the keys the generator uses are
    checked; nothing is read from disk here.
    """
    errors: List[str] = []
    warnings: List[str] = []
    s: Dict[str, Any] = {
        "case_name": cfg.get("case_name"),
        "description": cfg.get("description"),
        "paper_case": cfg.get("paper_case"),
    }

    # --- geometry ---------------------------------------------------------
    geom = _section(cfg, "geometry", errors)
    g: Dict[str, Any] = {}
    stl_dir = geom.get("stl_source_dir")
    if not isinstance(stl_dir, str) or not stl_dir.strip():
        errors.append("geometry.stl_source_dir is missing")
    g["stl_source_dir"] = stl_dir
    room_stl = geom.get("room_stl", "room.stl")
    if not isinstance(room_stl, str) or not room_stl.strip():
        errors.append("geometry.room_stl must be a file name")
    g["room_stl"] = room_stl

    furniture_stl = geom.get("furniture_stl")
    if furniture_stl is None:
        furniture_files: List[str] = []
    elif isinstance(furniture_stl, str) and furniture_stl.strip():
        furniture_files = [furniture_stl]
    elif isinstance(furniture_stl, (list, tuple)) and furniture_stl and all(
        isinstance(f, str) and f.strip() for f in furniture_stl
    ):
        furniture_files = list(furniture_stl)
    else:
        errors.append("geometry.furniture_stl must be a file name, a list of file names, or null (empty room)")
        furniture_files = []
    furniture_dir = geom.get("furniture_dir")
    if furniture_dir is not None and (not isinstance(furniture_dir, str) or not furniture_dir.strip()):
        errors.append("geometry.furniture_dir must be a folder name or null")
        furniture_dir = None
    if furniture_files and furniture_dir:
        errors.append("geometry.furniture_stl and geometry.furniture_dir are alternatives; give only one of them")
    g["furniture_files"] = furniture_files
    g["furniture_dir"] = furniture_dir

    height_raw = geom.get("room_height_m")
    if "measured_height_m" in geom:
        if height_raw is None:
            height_raw = geom.get("measured_height_m")
            warnings.append("geometry.measured_height_m is deprecated; use geometry.room_height_m")
        elif _num(geom.get("measured_height_m")) != _num(height_raw):
            errors.append("geometry.room_height_m and its old alias geometry.measured_height_m disagree")
    scale_flag = geom.get("scale_to_room_height")
    if "scale_to_measured_height" in geom:
        if scale_flag is None:
            scale_flag = geom.get("scale_to_measured_height")
            warnings.append("geometry.scale_to_measured_height is deprecated; use geometry.scale_to_room_height")
        elif bool(geom.get("scale_to_measured_height")) != bool(scale_flag):
            errors.append("geometry.scale_to_room_height and its old alias geometry.scale_to_measured_height disagree")
    if scale_flag is None:
        scale_flag = True
    if not isinstance(scale_flag, bool):
        errors.append("geometry.scale_to_room_height must be true or false")
        scale_flag = True
    room_height = None
    if height_raw is None:
        if scale_flag:
            errors.append("geometry.room_height_m is required when geometry.scale_to_room_height is true")
    else:
        room_height = _num(height_raw)
        if room_height is None or room_height <= 0.0:
            errors.append(f"geometry.room_height_m must be a positive number (got {height_raw!r})")
            room_height = None
    g["room_height_m"] = room_height
    g["scale_to_room_height"] = scale_flag
    s["geometry"] = g

    # --- ventilation ------------------------------------------------------
    vent = _section(cfg, "ventilation", errors)
    v: Dict[str, Any] = {}
    inlet_velocity = _num_list(vent.get("inlet_velocity", [0.0, 0.0, -0.5]), 3)
    if inlet_velocity is None:
        errors.append("ventilation.inlet_velocity must be a list of three numbers")
        inlet_velocity = [0.0, 0.0, -0.5]
    elif inlet_velocity[2] >= 0.0:
        warnings.append("ventilation.inlet_velocity has no downward (negative z) component; ceiling inlets will not supply air")
    v["inlet_velocity"] = inlet_velocity
    for key, default in (("scalar_initial_value", 1.0), ("scalar_inlet_value", 0.0)):
        value = _num(vent.get(key, default))
        if value is None:
            errors.append(f"ventilation.{key} must be a number")
            value = default
        v[key] = value
    half = _num(vent.get("ceiling_selection_half_thickness_m", 0.05))
    if half is None or half <= 0.0:
        errors.append("ventilation.ceiling_selection_half_thickness_m must be a positive number")
        half = 0.05
    v["ceiling_selection_half_thickness_m"] = half
    ref_height = vent.get("patch_reference_height_m")
    if ref_height is not None:
        ref_value = _num(ref_height)
        if ref_value is None or ref_value <= 0.0:
            errors.append(f"ventilation.patch_reference_height_m must be a positive number (got {ref_height!r})")
            ref_value = None
        ref_height = ref_value
    v["patch_reference_height_m"] = ref_height

    patches_raw = vent.get("patches")
    patches: List[Dict[str, Any]] = []
    if not isinstance(patches_raw, list) or not patches_raw:
        errors.append("ventilation.patches is missing or empty; define the inlet/outlet rectangles on the ceiling")
    else:
        seen = set()
        for index, patch in enumerate(patches_raw):
            if not isinstance(patch, dict):
                errors.append(f"ventilation.patches[{index}] must be a mapping")
                continue
            name = patch.get("name")
            label = f"ventilation.patches[{index}]" + (f" ('{name}')" if name else "")
            if not isinstance(name, str) or not name.replace("_", "").isalnum():
                errors.append(f"{label}: name must be a word (letters, digits, underscore)")
                continue
            if name in seen:
                errors.append(f"{label}: duplicate patch name")
            seen.add(name)
            role = patch.get("role")
            if role not in ("inlet", "outlet"):
                errors.append(f"{label}: role must be 'inlet' or 'outlet' (got {role!r})")
                continue
            shape = patch.get("shape", "rectangle")
            if shape != "rectangle":
                errors.append(f"{label}: unsupported patch shape '{shape}'; currently supported: rectangle")
                continue
            center = _num_list(patch.get("center_xy"), 2)
            size = _num_list(patch.get("size_xy"), 2)
            if center is None:
                errors.append(f"{label}: center_xy must be a list of two numbers")
            if size is None or min(size) <= 0.0:
                errors.append(f"{label}: size_xy must be a list of two positive numbers")
                size = None
            z_value = None
            if patch.get("z") is not None:
                z_value = _num(patch.get("z"))
                if z_value is None:
                    errors.append(f"{label}: z must be a number")
            if center is None or size is None:
                continue
            patches.append({"name": name, "role": role, "shape": shape, "center_xy": center, "size_xy": size, "z": z_value})
        if patches_raw and not errors:
            for role in ("inlet", "outlet"):
                if not any(p["role"] == role for p in patches):
                    errors.append(f"ventilation.patches defines no patch with role '{role}'")
    v["patches"] = patches
    s["ventilation"] = v

    # --- monitors ---------------------------------------------------------
    monitors_raw = cfg.get("monitors")
    monitors: List[Dict[str, Any]] = []
    if monitors_raw is None or monitors_raw == []:
        warnings.append("no monitors defined: the scalar run will write no probe history, so the clearance metrics cannot be extracted")
    elif not isinstance(monitors_raw, list):
        errors.append("monitors must be a list")
    else:
        seen = set()
        for index, monitor in enumerate(monitors_raw):
            if not isinstance(monitor, dict):
                errors.append(f"monitors[{index}] must be a mapping")
                continue
            name = monitor.get("name")
            label = f"monitors[{index}]" + (f" ('{name}')" if name else "")
            if not isinstance(name, str) or not name.replace("_", "").isalnum():
                errors.append(f"{label}: name must be a word (letters, digits, underscore)")
                continue
            if name in seen:
                errors.append(f"{label}: duplicate monitor name")
            seen.add(name)
            has_xyz = monitor.get("position_xyz") is not None
            has_rel = monitor.get("position_rel") is not None
            if has_xyz == has_rel:
                errors.append(f"{label}: give exactly one of position_xyz or position_rel")
                continue
            entry: Dict[str, Any] = {"name": name}
            if has_xyz:
                xyz = _num_list(monitor.get("position_xyz"), 3)
                if xyz is None:
                    errors.append(f"{label}: position_xyz must be a list of three numbers")
                    continue
                if monitor.get("height_above_floor_m") is not None:
                    errors.append(f"{label}: height_above_floor_m is only used with position_rel")
                    continue
                entry["position_xyz"] = xyz
            else:
                rel_raw = monitor.get("position_rel")
                rel = None
                if isinstance(rel_raw, (list, tuple)) and len(rel_raw) in (2, 3):
                    rel = _num_list(rel_raw, len(rel_raw))
                if rel is None or any(f < 0.0 or f > 1.0 for f in rel):
                    errors.append(f"{label}: position_rel must be [fx, fy] or [fx, fy, fz] with fractions between 0 and 1")
                    continue
                height = monitor.get("height_above_floor_m")
                if len(rel) == 2:
                    height_value = _num(height)
                    if height_value is None or height_value <= 0.0:
                        errors.append(f"{label}: position_rel [fx, fy] needs a positive height_above_floor_m")
                        continue
                    entry["height_above_floor_m"] = height_value
                elif height is not None:
                    errors.append(f"{label}: give either position_rel [fx, fy, fz] or position_rel [fx, fy] with height_above_floor_m, not both")
                    continue
                entry["position_rel"] = rel
            monitors.append(entry)
    s["monitors"] = monitors

    # --- transport / turbulence -------------------------------------------
    transport = _section(cfg, "transport", errors)
    t: Dict[str, Any] = {}
    field_name = transport.get("scalar_field_name", "T")
    if field_name != "T":
        errors.append("transport.scalar_field_name must be T (the field name read by scalarTransportFoamTurbulent)")
    t["scalar_field_name"] = "T"
    for key, default in (
        ("kinematic_viscosity_m2_s", 1.5e-5),
        ("molecular_diffusivity_m2_s", 1.5e-5),
        ("turbulent_schmidt_number", 0.7),
    ):
        value = _num(transport.get(key, default))
        if value is None or value <= 0.0:
            errors.append(f"transport.{key} must be a positive number")
            value = default
        t[key] = value
    s["transport"] = t

    turbulence = _section(cfg, "turbulence", errors)
    tb: Dict[str, Any] = {"model": turbulence.get("model", "kEpsilon")}
    if not isinstance(tb["model"], str):
        errors.append("turbulence.model must be a RAS model name")
        tb["model"] = "kEpsilon"
    for key, default in (("k", 0.015), ("epsilon", 0.004)):
        value = _num(turbulence.get(key, default))
        if value is None or value <= 0.0:
            errors.append(f"turbulence.{key} must be a positive number")
            value = default
        tb[key] = value
    s["turbulence"] = tb

    # --- mesh -------------------------------------------------------------
    mesh = _section(cfg, "mesh", errors)
    m: Dict[str, Any] = {}
    cell_size = _num(mesh.get("background_cell_size_m", 0.08))
    if cell_size is None or cell_size <= 0.0:
        errors.append("mesh.background_cell_size_m must be a positive number")
        cell_size = 0.08
    m["background_cell_size_m"] = cell_size
    cells = mesh.get("background_cells")
    if cells is not None:
        cell_numbers = _num_list(cells, 3)
        if cell_numbers is None or any(n < 1 or n != int(n) for n in cell_numbers):
            errors.append("mesh.background_cells must be a list of three positive integers")
            cells = None
        else:
            cells = [int(n) for n in cell_numbers]
    m["background_cells"] = cells
    padding = _num(mesh.get("background_padding_m", 0.01))
    if padding is None or padding < 0.0:
        errors.append("mesh.background_padding_m must be a non-negative number")
        padding = 0.01
    m["background_padding_m"] = padding
    for key, default in (("max_local_cells", 2_000_000), ("max_global_cells", 20_000_000)):
        value = _num(mesh.get(key, default))
        if value is None or value < 1 or value != int(value):
            errors.append(f"mesh.{key} must be a positive integer")
            value = default
        m[key] = int(value)
    for key, default in (("room_refinement_level", [2, 2]), ("furniture_refinement_level", [3, 4])):
        levels = _num_list(mesh.get(key, default), 2)
        if levels is None or any(n < 0 or n != int(n) for n in levels) or levels[0] > levels[1]:
            errors.append(f"mesh.{key} must be [min_level, max_level] with non-negative integers")
            levels = default
        m[key] = [int(n) for n in levels]
    distance_raw = mesh.get("furniture_distance_refinement", DEFAULT_FURNITURE_DISTANCE_REFINEMENT)
    distance_levels: List[List[float]] = []
    if not isinstance(distance_raw, list) or not distance_raw:
        errors.append("mesh.furniture_distance_refinement must be a list of [distance_m, level] pairs")
    else:
        for pair in distance_raw:
            numbers = _num_list(pair, 2)
            if numbers is None or numbers[0] <= 0.0 or numbers[1] < 0 or numbers[1] != int(numbers[1]):
                errors.append("mesh.furniture_distance_refinement must be a list of [distance_m, level] pairs")
                distance_levels = []
                break
            distance_levels.append([numbers[0], int(numbers[1])])
        distance_levels.sort(key=lambda pair: pair[0])
        if any(a[1] < b[1] for a, b in zip(distance_levels, distance_levels[1:])):
            errors.append("mesh.furniture_distance_refinement: the refinement level must not increase with distance")
    m["furniture_distance_refinement"] = distance_levels or [list(p) for p in DEFAULT_FURNITURE_DISTANCE_REFINEMENT]
    diffuser = mesh.get("diffuser_refinement", {})
    if diffuser is None:
        diffuser = {}
    d: Dict[str, Any] = {"enabled": True, "level": 3, "bottom_height_above_floor_m": 0.5, "margin_m": 0.0}
    if not isinstance(diffuser, dict):
        errors.append("mesh.diffuser_refinement must be a mapping")
    else:
        enabled = diffuser.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append("mesh.diffuser_refinement.enabled must be true or false")
            enabled = True
        d["enabled"] = enabled
        level = _num(diffuser.get("level", 3))
        if level is None or level < 0 or level != int(level):
            errors.append("mesh.diffuser_refinement.level must be a non-negative integer")
            level = 3
        d["level"] = int(level)
        for key in ("bottom_height_above_floor_m", "margin_m"):
            value = _num(diffuser.get(key, d[key]))
            if value is None or value < 0.0:
                errors.append(f"mesh.diffuser_refinement.{key} must be a non-negative number")
                value = d[key]
            d[key] = value
    m["diffuser_refinement"] = d
    location = mesh.get("location_in_mesh")
    if location is not None:
        location = _num_list(location, 3)
        if location is None:
            errors.append("mesh.location_in_mesh must be a list of three numbers")
    m["location_in_mesh"] = location
    s["mesh"] = m

    # --- simulation -------------------------------------------------------
    sim = _section(cfg, "simulation", errors)
    si: Dict[str, Any] = {
        "openfoam_version": str(sim.get("openfoam_version", "2412")),
        "airflow_solver": sim.get("airflow_solver", "simpleFoam"),
        "scalar_solver": sim.get("scalar_solver", "scalarTransportFoamTurbulent"),
    }
    for key in ("airflow_solver", "scalar_solver"):
        if not isinstance(si[key], str) or not si[key].strip():
            errors.append(f"simulation.{key} must be an application name")
    for key, default, integer in (
        ("airflow_end_iter", 1000, True),
        ("delta_t", 1, False),
        ("write_interval", 100, True),
        ("scalar_delta_t", 0.05, False),
        ("scalar_write_interval", 20, False),
        ("monitor_write_interval_steps", 1, True),
        ("default_tasks", 56, True),
    ):
        value = _num(sim.get(key, default))
        if value is None or value <= 0.0 or (integer and value != int(value)):
            errors.append(f"simulation.{key} must be a positive {'integer' if integer else 'number'}")
            value = default
        si[key] = int(value) if integer else value
    flow_end_time = si["airflow_end_iter"] * si["delta_t"]
    scalar_end = sim.get("scalar_end_time")
    if scalar_end is None:
        scalar_end_value = flow_end_time + 1200.0
        warnings.append(f"simulation.scalar_end_time is not set; using {fmt(scalar_end_value)} (1200 s after the flow solve)")
    else:
        scalar_end_value = _num(scalar_end)
        if scalar_end_value is None:
            errors.append("simulation.scalar_end_time must be a number")
            scalar_end_value = flow_end_time + 1200.0
    if scalar_end_value <= flow_end_time:
        errors.append(
            f"simulation.scalar_end_time ({fmt(scalar_end_value)}) must be larger than the time at which the flow solve ends "
            f"({fmt(flow_end_time)}): the scalar run starts from the last simpleFoam time directory"
        )
    si["scalar_end_time"] = scalar_end_value
    method = sim.get("decomposition_method", "scotch")
    if method not in SUPPORTED_DECOMPOSITION_METHODS:
        errors.append(
            f"simulation.decomposition_method must be one of {', '.join(SUPPORTED_DECOMPOSITION_METHODS)} (got {method!r})"
        )
        method = "scotch"
    si["decomposition_method"] = method
    split = sim.get("decomposition")
    if method in ("simple", "hierarchical"):
        numbers = _num_list(split, 3)
        if numbers is None or any(n < 1 or n != int(n) for n in numbers):
            errors.append(f"simulation.decomposition [nx, ny, nz] is required for decomposition_method {method}")
        else:
            split = [int(n) for n in numbers]
            if split[0] * split[1] * split[2] != si["default_tasks"]:
                errors.append("simulation.decomposition [nx, ny, nz] must multiply to simulation.default_tasks")
    else:
        split = None
    si["decomposition"] = split
    s["simulation"] = si

    runtime = cfg.get("runtime")
    s["runtime"] = runtime if isinstance(runtime, dict) else {}
    return s, errors, warnings


# ---------------------------------------------------------------------------
# STL handling
# ---------------------------------------------------------------------------
def resolve_stl_dir(raw: str, cfg_path: Path) -> Tuple[Path, List[Path]]:
    """Resolve geometry.stl_source_dir.

    A relative path is tried against the current directory, the repository
    root and the folder of the config file, in that order.
    """
    path = Path(os.path.expandvars(raw)).expanduser()
    if path.is_absolute():
        return path, [path]
    candidates = [Path.cwd() / path, REPO_ROOT / path, cfg_path.resolve().parent / path]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate, candidates
    return candidates[0], candidates


def resolve_stl_sources(settings: Dict[str, Any], cfg_path: Path) -> Tuple[Path, Path, List[Path]]:
    """Return (stl_dir, room STL, list of furniture STLs); exit if anything is missing."""
    geom = settings["geometry"]
    stl_dir, tried = resolve_stl_dir(geom["stl_source_dir"], cfg_path)
    if not stl_dir.is_dir():
        looked = "\n".join(f"         {c}" for c in dict.fromkeys(tried))
        raise fail(f"geometry.stl_source_dir not found: {geom['stl_source_dir']}\n       looked for:\n{looked}")
    room_src = stl_dir / geom["room_stl"]
    if not room_src.is_file():
        raise fail(f"missing room STL: {room_src}")
    furniture: List[Path] = []
    for name in geom["furniture_files"]:
        path = stl_dir / name
        if not path.is_file():
            raise fail(f"missing furniture STL: {path}")
        furniture.append(path)
    if geom["furniture_dir"]:
        folder = stl_dir / geom["furniture_dir"]
        if not folder.is_dir():
            raise fail(f"missing furniture folder (geometry.furniture_dir): {folder}")
        furniture = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".stl")
        if not furniture:
            raise fail(f"geometry.furniture_dir contains no STL files: {folder}")
    return stl_dir, room_src, furniture


def load_mesh(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(path, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty or len(mesh.faces) == 0:
        raise ValueError(f"Empty STL mesh: {path}")
    return mesh


def safe_call(obj: Any, name: str, *args: Any, **kwargs: Any) -> None:
    fn = getattr(obj, name, None)
    if callable(fn):
        fn(*args, **kwargs)


def conservative_repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Apply conservative trimesh cleanup without changing units or coordinates."""
    mesh = mesh.copy()
    safe_call(mesh, "process", validate=True)
    safe_call(mesh, "merge_vertices")
    safe_call(mesh, "remove_unreferenced_vertices")
    try:
        trimesh.repair.fix_winding(mesh)
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fix_inversion(mesh)
        trimesh.repair.fill_holes(mesh)
    except Exception:
        # The post-repair watertightness check decides whether to continue.
        pass
    safe_call(mesh, "remove_unreferenced_vertices")
    return mesh


def quality_report(mesh: trimesh.Trimesh, label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "is_watertight": bool(mesh.is_watertight),
        "is_winding_consistent": bool(getattr(mesh, "is_winding_consistent", False)),
        "euler_number": int(mesh.euler_number) if getattr(mesh, "euler_number", None) is not None else None,
        "bounds": mesh.bounds.tolist(),
        "extents": mesh.extents.tolist(),
    }


def body_report(mesh: trimesh.Trimesh, max_listed: int = 20) -> Tuple[Optional[Dict[str, Any]], Optional[np.ndarray]]:
    """Watertightness per connected body, and the bounding boxes of the bodies."""
    try:
        bodies = mesh.split(only_watertight=False)
    except Exception:
        return None, None
    if len(bodies) == 0:
        return None, None
    bounds = np.array([body.bounds for body in bodies], dtype=float)
    open_bodies = [body for body in bodies if not body.is_watertight]
    report = {
        "body_count": int(len(bodies)),
        "watertight_bodies": int(len(bodies) - len(open_bodies)),
        "non_watertight_bodies": int(len(open_bodies)),
        "faces_in_non_watertight_bodies": int(sum(len(body.faces) for body in open_bodies)),
        "largest_non_watertight_bodies": [
            {"faces": int(len(body.faces)), "bounds": body.bounds.tolist()}
            for body in sorted(open_bodies, key=lambda b: -len(b.faces))[:max_listed]
        ],
    }
    return report, bounds


def prepare_surface(
    sources: List[Path],
    scale: float,
    *,
    label: str,
    repair_stl: bool,
    require_watertight: bool,
    per_body: bool,
    preloaded: Optional[trimesh.Trimesh] = None,
) -> Tuple[trimesh.Trimesh, Dict[str, Any], Optional[np.ndarray]]:
    """Load (and merge), check, optionally repair and scale one surface.

    The repaired mesh is used only if the repair made the surface watertight;
    otherwise the surface is written exactly as it was delivered.
    """
    if preloaded is not None:
        mesh = preloaded.copy()
    else:
        try:
            meshes = [load_mesh(path) for path in sources]
        except Exception as exc:
            raise fail(f"cannot read {label} STL: {exc}")
        mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
    before = quality_report(mesh, label)

    repair_attempted = False
    repaired = False
    if repair_stl and not mesh.is_watertight:
        repair_attempted = True
        candidate = conservative_repair(mesh)
        if len(candidate.faces) > 0 and candidate.is_watertight:
            mesh = candidate
            repaired = True

    watertight = bool(mesh.is_watertight)
    if require_watertight and not watertight:
        where = ", ".join(str(p) for p in sources[:3]) + (" ..." if len(sources) > 3 else "")
        raise fail(
            f"{label} STL is not watertight"
            + (" after conservative trimesh repair" if repair_attempted else "")
            + f": {where}\n"
            + (
                "       The room surface must be closed. Fix it upstream (pipeline step 5)."
                if label == "room"
                else "       Fix the STL upstream, or drop --strict-watertight to continue with a warning."
            )
        )

    bounds_before_scale = mesh.bounds.tolist()
    if not math.isclose(scale, 1.0, rel_tol=0.0, abs_tol=1e-15):
        mesh.apply_scale(scale)
    bodies, body_bounds = body_report(mesh) if per_body else (None, None)
    report = {
        "label": label,
        "sources": [str(p) for p in sources],
        "scale": scale,
        "repair_attempted": repair_attempted,
        "repaired_by_trimesh": repaired,
        "is_watertight": watertight,
        "quality_before_repair": before,
        "bounds_before_scale": bounds_before_scale,
        "quality_final_written_stl": quality_report(mesh, label),
    }
    if bodies is not None:
        report["bodies"] = bodies
    return mesh, report, body_bounds


def bounds_tuple(bounds: Iterable[Iterable[float]]) -> Tuple[Vector, Vector]:
    b = list(bounds)
    return (float(b[0][0]), float(b[0][1]), float(b[0][2])), (float(b[1][0]), float(b[1][1]), float(b[1][2]))


# ---------------------------------------------------------------------------
# Geometry checks (plain numpy; no ray-tracing backend needed)
# ---------------------------------------------------------------------------
def _axis_crossings(triangles: np.ndarray, point: Sequence[float], axis: int, eps: float) -> np.ndarray:
    """Coordinates along `axis` where the axis-parallel line through `point` crosses the triangles."""
    if triangles is None or len(triangles) == 0:
        return np.empty(0)
    i, j = [k for k in range(3) if k != axis]
    a, b, c = triangles[:, 0, :], triangles[:, 1, :], triangles[:, 2, :]
    v0i, v0j = b[:, i] - a[:, i], b[:, j] - a[:, j]
    v1i, v1j = c[:, i] - a[:, i], c[:, j] - a[:, j]
    det = v0i * v1j - v0j * v1i
    scale = np.maximum(np.abs(v0i) + np.abs(v0j), np.abs(v1i) + np.abs(v1j))
    valid = np.abs(det) > 1e-12 * scale * scale
    det_safe = np.where(valid, det, 1.0)
    pi, pj = point[i] - a[:, i], point[j] - a[:, j]
    u = (pi * v1j - pj * v1i) / det_safe
    w = (v0i * pj - v0j * pi) / det_safe
    inside = valid & (u >= -eps) & (w >= -eps) & (u + w <= 1.0 + eps)
    if not np.any(inside):
        return np.empty(0)
    return a[inside, axis] + u[inside] * (b[inside, axis] - a[inside, axis]) + w[inside] * (c[inside, axis] - a[inside, axis])


def axis_hits(triangles: np.ndarray, point: Sequence[float], axis: int = 2) -> np.ndarray:
    """Sorted, distinct coordinates along `axis` where the line through `point` meets the surface.

    Used to find the floor and the ceiling above a point of the room footprint;
    crossings at the same coordinate (shared edges) are merged.
    """
    values = _axis_crossings(triangles, point, axis, 1e-9)
    if len(values) == 0:
        return values
    values = np.sort(values)
    span = float(np.ptp(triangles[:, :, axis])) or 1.0
    keep = np.concatenate(([True], np.diff(values) > 1e-7 * span))
    return values[keep]


def point_inside_surface(triangles: np.ndarray, point: Sequence[float], axis: int = 2) -> bool:
    """Parity test: is `point` inside the closed surface made of `triangles`?

    The ray is shifted sideways by a negligible, irregular amount so that it
    does not run exactly through triangle edges or vertices (for example the
    diagonal of a rectangular floor), which would be counted twice.
    """
    if triangles is None or len(triangles) == 0:
        return False
    shifted = [float(c) for c in point]
    for k, factor in zip([k for k in range(3) if k != axis], (7.310585786e-8, 2.689414214e-8)):
        shifted[k] += factor * (float(np.ptp(triangles[:, :, k])) or 1.0)
    values = _axis_crossings(triangles, shifted, axis, 0.0)
    return int(np.count_nonzero(values > point[axis])) % 2 == 1


def point_inside_furniture(triangles: Optional[np.ndarray], bounds: Optional[Tuple[Vector, Vector]], point: Sequence[float]) -> bool:
    """Estimate whether `point` is inside a furniture body (majority of three parity tests).

    Furniture may contain open bodies, for which a single parity test is not
    reliable; this is therefore only used for warnings and for locationInMesh.
    """
    if triangles is None or bounds is None:
        return False
    if any(point[k] < bounds[0][k] or point[k] > bounds[1][k] for k in range(3)):
        return False
    votes = sum(1 for axis in range(3) if point_inside_surface(triangles, point, axis))
    return votes >= 2


def _clip_polygon(poly: List[Tuple[float, float]], axis: int, bound: float, keep_greater: bool) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    count = len(poly)
    for index in range(count):
        p, q = poly[index], poly[(index + 1) % count]
        p_in = p[axis] >= bound if keep_greater else p[axis] <= bound
        q_in = q[axis] >= bound if keep_greater else q[axis] <= bound
        if p_in:
            out.append(p)
        if p_in != q_in:
            t = (bound - p[axis]) / (q[axis] - p[axis])
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def _polygon_area(poly: List[Tuple[float, float]]) -> float:
    if len(poly) < 3:
        return 0.0
    area = 0.0
    for index in range(len(poly)):
        x0, y0 = poly[index]
        x1, y1 = poly[(index + 1) % len(poly)]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def rectangle_overlap_with_triangles(triangles_xy: np.ndarray, xmin: float, xmax: float, ymin: float, ymax: float) -> float:
    """Area of the rectangle that is covered by the (non-overlapping) triangles."""
    total = 0.0
    for tri in triangles_xy:
        poly = [(float(x), float(y)) for x, y in tri]
        for axis, bound, keep_greater in ((0, xmin, True), (0, xmax, False), (1, ymin, True), (1, ymax, False)):
            poly = _clip_polygon(poly, axis, bound, keep_greater)
            if len(poly) < 3:
                break
        total += _polygon_area(poly)
    return total


def ceiling_triangles(room_triangles: np.ndarray, z_top: float, tol: float) -> np.ndarray:
    """Room triangles that lie in the horizontal ceiling plane z = z_top."""
    mask = np.all(np.abs(room_triangles[:, :, 2] - z_top) <= tol, axis=1)
    return room_triangles[mask]


# ---------------------------------------------------------------------------
# Case planning: everything is computed (and checked) here, nothing is written
# ---------------------------------------------------------------------------
def cells_for(extent: float, size: float) -> int:
    return max(1, int(math.floor(extent / size + 0.5)))


def plan_patches(
    settings: Dict[str, Any],
    room_bounds: Tuple[Vector, Vector],
    room_triangles: np.ndarray,
    patch_scale: float,
    allow_partial: bool,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """Rescale the patch rectangles and check that they lie on the ceiling."""
    rmin, rmax = room_bounds
    z_top = rmax[2]
    height = rmax[2] - rmin[2]
    dz = settings["ventilation"]["ceiling_selection_half_thickness_m"]
    ceiling = ceiling_triangles(room_triangles, z_top, 1e-6 * max(1.0, height))
    if len(ceiling) == 0:
        warnings.append(
            "the room STL has no flat ceiling triangles at its highest z; patch positions are checked "
            "against the bounding box of the room only"
        )
    problems: List[str] = []
    planned: List[Dict[str, Any]] = []
    for patch in settings["ventilation"]["patches"]:
        cx, cy = (patch["center_xy"][0] * patch_scale, patch["center_xy"][1] * patch_scale)
        sx, sy = patch["size_xy"]
        z = z_top if patch["z"] is None else patch["z"] * patch_scale
        xmin, xmax, ymin, ymax = cx - sx / 2.0, cx + sx / 2.0, cy - sy / 2.0, cy + sy / 2.0
        area = sx * sy
        ox = max(0.0, min(xmax, rmax[0]) - max(xmin, rmin[0]))
        oy = max(0.0, min(ymax, rmax[1]) - max(ymin, rmin[1]))
        bbox_fraction = min(1.0, ox * oy / area)
        if len(ceiling):
            overlap = min(1.0, rectangle_overlap_with_triangles(ceiling[:, :, :2], xmin, xmax, ymin, ymax) / area)
        else:
            overlap = bbox_fraction
        if 1.0 - overlap < 1e-9:
            overlap = 1.0
        if 1.0 - bbox_fraction < 1e-9:
            bbox_fraction = 1.0
        if overlap < MIN_PATCH_CEILING_OVERLAP:
            problems.append(
                f"patch '{patch['name']}': only {overlap * 100.0:.1f}% of the rectangle "
                f"x [{fmt(xmin)}, {fmt(xmax)}], y [{fmt(ymin)}, {fmt(ymax)}] lies on the ceiling "
                f"(room footprint x [{fmt(rmin[0])}, {fmt(rmax[0])}], y [{fmt(rmin[1])}, {fmt(rmax[1])}])"
            )
        # topoSet box: the rectangle, kept just inside the room footprint (see SELECTION_WALL_CLEARANCE)
        select_min = [max(xmin, rmin[0] + SELECTION_WALL_CLEARANCE), max(ymin, rmin[1] + SELECTION_WALL_CLEARANCE)]
        select_max = [min(xmax, rmax[0] - SELECTION_WALL_CLEARANCE), min(ymax, rmax[1] - SELECTION_WALL_CLEARANCE)]
        if select_min[0] >= select_max[0] or select_min[1] >= select_max[1]:
            select_min, select_max = [xmin, ymin], [xmax, ymax]
        if abs(z - z_top) > dz:
            problems.append(
                f"patch '{patch['name']}': the selection slab z = {fmt(z)} +/- {fmt(dz)} does not contain "
                f"the ceiling plane z = {fmt(z_top)}"
            )
        planned.append(
            {
                "name": patch["name"],
                "role": patch["role"],
                "shape": patch["shape"],
                "center_xy_config": list(patch["center_xy"]),
                "center_xy": [cx, cy],
                "size_xy": [sx, sy],
                "z": z,
                "rect_min_xy": [xmin, ymin],
                "rect_max_xy": [xmax, ymax],
                "box_min": [select_min[0], select_min[1], z - dz],
                "box_max": [select_max[0], select_max[1], z + dz],
                "nominal_area_m2": area,
                "ceiling_overlap_fraction": overlap,
                "bbox_overlap_fraction": bbox_fraction,
                "area_on_ceiling_m2": area * overlap,
            }
        )
    if problems:
        text = (
            "ventilation patches are not on the ceiling of the scaled room:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n  Check that geometry.stl_source_dir is the wall-aligned folder (4_stl/axis_aligned) and that"
            "\n  ventilation.patch_reference_height_m is the room height for which the patch centres were defined."
        )
        if allow_partial:
            warnings.append(text + "\n  (continuing because --allow-partial-patches was given)")
        else:
            raise fail(text + "\n  Use --allow-partial-patches to continue anyway.")
    return planned


def plan_block_mesh(settings: Dict[str, Any], room_bounds: Tuple[Vector, Vector]) -> Dict[str, Any]:
    rmin, rmax = room_bounds
    mesh = settings["mesh"]
    pad = mesh["background_padding_m"]
    bmin = [rmin[k] - pad for k in range(3)]
    bmax = [rmax[k] + pad for k in range(3)]
    extents = [bmax[k] - bmin[k] for k in range(3)]
    if mesh["background_cells"] is not None:
        cells = list(mesh["background_cells"])
    else:
        cells = [cells_for(extents[k], mesh["background_cell_size_m"]) for k in range(3)]
    sizes = [extents[k] / cells[k] for k in range(3)]
    return {
        "min": bmin,
        "max": bmax,
        "cells": cells,
        "cell_size_m": sizes,
        "target_cell_size_m": mesh["background_cell_size_m"],
        "explicit_cell_counts": mesh["background_cells"] is not None,
        "cell_count": cells[0] * cells[1] * cells[2],
    }


def plan_diffuser_boxes(
    settings: Dict[str, Any], room_bounds: Tuple[Vector, Vector], patches: List[Dict[str, Any]], block: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """One refinement box per inlet patch: patch footprint (+ margin) from the occupied zone up to the ceiling."""
    diffuser = settings["mesh"]["diffuser_refinement"]
    if not diffuser["enabled"]:
        return []
    rmin, rmax = room_bounds
    z_bottom = rmin[2] + diffuser["bottom_height_above_floor_m"]
    z_top = rmax[2]
    if z_bottom >= z_top:
        raise fail(
            f"mesh.diffuser_refinement.bottom_height_above_floor_m ({fmt(diffuser['bottom_height_above_floor_m'])} m) "
            f"is not below the ceiling (room height {fmt(z_top - rmin[2])} m)"
        )
    margin = diffuser["margin_m"]
    cell = max(block["cell_size_m"]) / (2 ** diffuser["level"])
    boxes = []
    for patch in patches:
        if patch["role"] != "inlet":
            continue
        bmin = [patch["rect_min_xy"][0] - margin, patch["rect_min_xy"][1] - margin, z_bottom]
        bmax = [patch["rect_max_xy"][0] + margin, patch["rect_max_xy"][1] + margin, z_top]
        volume = (bmax[0] - bmin[0]) * (bmax[1] - bmin[1]) * (bmax[2] - bmin[2])
        boxes.append(
            {
                "name": f"diffuserBox_{patch['name']}",
                "patch": patch["name"],
                "min": bmin,
                "max": bmax,
                "level": diffuser["level"],
                "cell_size_m": cell,
                "approx_cells": int(volume / cell ** 3),
            }
        )
    return boxes


def plan_location_in_mesh(
    settings: Dict[str, Any],
    room_bounds: Tuple[Vector, Vector],
    room_triangles: np.ndarray,
    block: Dict[str, Any],
    furniture_triangles: Optional[np.ndarray],
    furniture_bounds: Optional[Tuple[Vector, Vector]],
    furniture_body_bounds: Optional[np.ndarray],
    warnings: List[str],
) -> List[float]:
    """A point in the air just below the ceiling, near the room centre, off the background-mesh grid."""
    rmin, rmax = room_bounds
    explicit = settings["mesh"]["location_in_mesh"]
    if explicit is not None:
        if not point_inside_surface(room_triangles, explicit):
            warnings.append(f"mesh.location_in_mesh {vec(explicit)} is not inside the scaled room")
        return list(explicit)

    bmin, cells, size = block["min"], block["cells"], block["cell_size_m"]
    height = rmax[2] - rmin[2]

    def cell_index(coordinate: float, axis: int) -> int:
        return min(cells[axis] - 1, max(0, int(math.floor((coordinate - bmin[axis]) / size[axis]))))

    def cell_point(index: Sequence[int]) -> List[float]:
        return [bmin[k] + (index[k] + LOCATION_CELL_FRACTIONS[k]) * size[k] for k in range(3)]

    centre = [(rmin[0] + rmax[0]) / 2.0, (rmin[1] + rmax[1]) / 2.0]
    depth = max(1.5 * size[2], min(0.25, 0.1 * height))
    k0 = cell_index(rmax[2] - depth, 2)
    while k0 > 0 and bmin[2] + (k0 + LOCATION_CELL_FRACTIONS[2]) * size[2] > rmax[2] - 0.5 * size[2]:
        k0 -= 1
    i0, j0 = cell_index(centre[0], 0), cell_index(centre[1], 1)

    def in_body_boxes(point: Sequence[float]) -> bool:
        if furniture_body_bounds is None or len(furniture_body_bounds) == 0:
            return False
        margin = 0.5 * min(size)
        p = np.asarray(point)
        inside = np.all((p >= furniture_body_bounds[:, 0, :] - margin) & (p <= furniture_body_bounds[:, 1, :] + margin), axis=1)
        return bool(np.any(inside))

    fallback = None
    reach = 12
    offsets = sorted(
        ((di, dj) for di in range(-reach, reach + 1) for dj in range(-reach, reach + 1)),
        key=lambda o: (o[0] * o[0] + o[1] * o[1], o),
    )
    for di, dj in offsets:
        i, j = i0 + di, j0 + dj
        if not (0 <= i < cells[0] and 0 <= j < cells[1]):
            continue
        point = cell_point((i, j, k0))
        if not all(rmin[k] < point[k] < rmax[k] for k in range(3)):
            continue
        if not point_inside_surface(room_triangles, point):
            continue
        if point_inside_furniture(furniture_triangles, furniture_bounds, point):
            continue
        if fallback is None:
            fallback = point
        if not in_body_boxes(point):
            return point
    if fallback is not None:
        warnings.append(
            "locationInMesh lies inside the bounding box of a furniture body (but not inside the body itself); "
            "set mesh.location_in_mesh if snappyHexMesh keeps the wrong region"
        )
        return fallback
    raise fail(
        "could not find a locationInMesh point below the ceiling near the room centre; "
        "set mesh.location_in_mesh [x, y, z] in the config"
    )


def plan_monitors(
    settings: Dict[str, Any],
    room_bounds: Tuple[Vector, Vector],
    room_triangles: np.ndarray,
    furniture_triangles: Optional[np.ndarray],
    furniture_bounds: Optional[Tuple[Vector, Vector]],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """Absolute monitor (probe) coordinates in the scaled case, checked to be inside the room."""
    rmin, rmax = room_bounds
    extents = [rmax[k] - rmin[k] for k in range(3)]
    problems: List[str] = []
    planned: List[Dict[str, Any]] = []
    for monitor in settings["monitors"]:
        name = monitor["name"]
        entry: Dict[str, Any] = {"name": name}
        if "position_xyz" in monitor:
            point = list(monitor["position_xyz"])
            entry["defined_by"] = "position_xyz"
        else:
            rel = monitor["position_rel"]
            x = rmin[0] + rel[0] * extents[0]
            y = rmin[1] + rel[1] * extents[1]
            entry["position_rel"] = list(rel)
            if len(rel) == 3:
                point = [x, y, rmin[2] + rel[2] * extents[2]]
                entry["defined_by"] = "position_rel [fx, fy, fz]"
            else:
                height = monitor["height_above_floor_m"]
                entry["height_above_floor_m"] = height
                entry["defined_by"] = "position_rel [fx, fy] + height_above_floor_m (above the local floor)"
                hits = axis_hits(room_triangles, (x, y, 0.0), 2)
                if len(hits) < 2:
                    problems.append(
                        f"monitor '{name}': position_rel {rel} (x = {fmt(x)}, y = {fmt(y)}) is outside the room bounds "
                        "(no floor and ceiling found there)"
                    )
                    continue
                point = [x, y, float(hits[0]) + height]
                if point[2] >= float(hits[1]):
                    problems.append(
                        f"monitor '{name}': height_above_floor_m = {fmt(height)} m is above the room surface over the "
                        f"local floor (free height {fmt(float(hits[1]) - float(hits[0]))} m)"
                    )
                    continue
        if not all(rmin[k] < point[k] < rmax[k] for k in range(3)):
            problems.append(
                f"monitor '{name}': {vec(point)} is outside the room bounds "
                f"{vec(rmin)} - {vec(rmax)}"
            )
            continue
        if not point_inside_surface(room_triangles, point):
            problems.append(
                f"monitor '{name}': {vec(point)} is outside the room (for example below a raised or tiered floor)"
            )
            continue
        below = axis_hits(room_triangles, point, 2)
        below = below[below < point[2]]
        if len(below):
            entry["local_floor_z_m"] = float(below[-1])
            entry["height_above_local_floor_m"] = point[2] - float(below[-1])
        entry["position_xyz"] = [float(c) for c in point]
        entry["inside_furniture_estimate"] = point_inside_furniture(furniture_triangles, furniture_bounds, point)
        if entry["inside_furniture_estimate"]:
            warnings.append(
                f"monitor '{name}' at {vec(point)} appears to be inside a furniture or mannequin body; "
                "the probe would then record no valid data. Move the monitor."
            )
        planned.append(entry)
    if problems:
        raise fail("monitors are not inside the scaled room:\n" + "\n".join(f"  - {p}" for p in problems))
    return planned


def estimate_cells(
    settings: Dict[str, Any],
    block: Dict[str, Any],
    boxes: List[Dict[str, Any]],
    room_mesh: trimesh.Trimesh,
    furniture_mesh: Optional[trimesh.Trimesh],
) -> Dict[str, Any]:
    """Order-of-magnitude cell count of the refined mesh (snappyHexMesh adds buffer layers on top)."""
    base = max(block["cell_size_m"])
    mesh = settings["mesh"]
    room_cell = base / (2 ** mesh["room_refinement_level"][0])
    estimate = {
        "background": int(block["cell_count"]),
        "diffuser_boxes": int(sum(box["approx_cells"] for box in boxes)),
        "room_surface_layer": int(float(room_mesh.area) / room_cell ** 2),
        "furniture_distance_shells": 0,
    }
    if furniture_mesh is not None:
        area = float(furniture_mesh.area)
        cells = 0.0
        previous = 0.0
        for distance, level in mesh["furniture_distance_refinement"]:
            cell = base / (2 ** level)
            cells += area * (distance - previous) / cell ** 3
            previous = distance
        estimate["furniture_distance_shells"] = int(cells)
    estimate["total"] = int(sum(estimate.values()))
    estimate["note"] = (
        "rough estimate: background cells + refinement boxes + one layer of cells on the room surface + "
        "the distance shells on the outside of the furniture surface; not a snappyHexMesh result"
    )
    return estimate


def build_case_plan(
    cfg: Dict[str, Any],
    cfg_path: Path,
    *,
    repair_stl: bool = True,
    strict_watertight: bool = False,
    allow_non_watertight: bool = False,
    allow_partial_patches: bool = False,
) -> Dict[str, Any]:
    """Validate the config, load and scale the STLs and compute every case quantity.

    Raises SystemExit with a clear message on any problem. Nothing is written.
    """
    settings, errors, warnings = normalise_config(cfg)
    if errors:
        raise fail("invalid config " + str(cfg_path) + ":\n" + "\n".join(f"  - {e}" for e in errors))

    stl_dir, room_src, furniture_src = resolve_stl_sources(settings, cfg_path)
    geom = settings["geometry"]

    # --- scale factor from the room height --------------------------------
    try:
        room_raw = load_mesh(room_src)
    except Exception as exc:
        raise fail(f"cannot read room STL: {exc}")
    rmin0, rmax0 = bounds_tuple(room_raw.bounds)
    current_height = rmax0[2] - rmin0[2]
    if not current_height > 1e-9:
        raise fail(f"the room STL has zero height (z extent {current_height}): {room_src}")
    scale = 1.0
    if geom["scale_to_room_height"]:
        scale = float(geom["room_height_m"]) / current_height

    # --- surfaces ----------------------------------------------------------
    if allow_non_watertight:
        warnings.append("--allow-non-watertight is deprecated: the room STL is not required to be watertight in this run")
    room_mesh, room_report, _ = prepare_surface(
        [room_src],
        scale,
        label="room",
        repair_stl=repair_stl,
        require_watertight=not allow_non_watertight,
        per_body=False,
        preloaded=room_raw,
    )
    if not room_report["is_watertight"]:
        warnings.append(f"the room STL is not watertight: {room_src}")
    reports = [room_report]

    furniture_mesh = None
    furniture_body_bounds = None
    if furniture_src:
        furniture_mesh, furniture_report, furniture_body_bounds = prepare_surface(
            furniture_src,
            scale,
            label="furniture",
            repair_stl=repair_stl,
            require_watertight=strict_watertight,
            per_body=True,
        )
        reports.append(furniture_report)
        if not furniture_report["is_watertight"]:
            bodies = furniture_report.get("bodies")
            detail = ""
            if bodies:
                detail = (
                    f" ({bodies['non_watertight_bodies']} of {bodies['body_count']} connected bodies are open, "
                    f"{bodies['faces_in_non_watertight_bodies']} of {furniture_report['quality_final_written_stl']['faces']} faces)"
                )
            warnings.append(
                "the furniture STL is not watertight" + detail + ". snappyHexMesh tolerates open surfaces "
                "(the seated-mannequin template of the pipeline is known to be open); "
                "use --strict-watertight to make this an error"
            )

    room_bounds = bounds_tuple(room_mesh.bounds)
    rmin, rmax = room_bounds
    extents = [rmax[k] - rmin[k] for k in range(3)]
    room_height = extents[2]
    room_triangles = np.asarray(room_mesh.triangles, dtype=float)
    furniture_triangles = None
    furniture_bounds = None
    if furniture_mesh is not None:
        furniture_triangles = np.asarray(furniture_mesh.triangles, dtype=float)
        furniture_bounds = bounds_tuple(furniture_mesh.bounds)
        outside = [
            "xyz"[k]
            for k in range(3)
            if furniture_bounds[0][k] < rmin[k] - 1e-6 * room_height or furniture_bounds[1][k] > rmax[k] + 1e-6 * room_height
        ]
        if outside:
            warnings.append(
                "the furniture extends beyond the room bounds in " + ", ".join(outside)
                + "; the parts outside the room are not meshed"
            )

    # --- patches -----------------------------------------------------------
    reference = settings["ventilation"]["patch_reference_height_m"]
    patch_scale = room_height / reference if reference else 1.0
    patches = plan_patches(settings, room_bounds, room_triangles, patch_scale, allow_partial_patches, warnings)

    # --- mesh --------------------------------------------------------------
    block = plan_block_mesh(settings, room_bounds)
    boxes = plan_diffuser_boxes(settings, room_bounds, patches, block)
    location = plan_location_in_mesh(
        settings, room_bounds, room_triangles, block, furniture_triangles, furniture_bounds, furniture_body_bounds, warnings
    )
    estimate = estimate_cells(settings, block, boxes, room_mesh, furniture_mesh)
    if estimate["total"] > settings["mesh"]["max_global_cells"]:
        warnings.append(
            f"the rough cell estimate ({estimate['total'] / 1e6:.1f} million: background {estimate['background'] / 1e6:.1f}, "
            f"diffuser boxes {estimate['diffuser_boxes'] / 1e6:.1f}, room surface {estimate['room_surface_layer'] / 1e6:.1f}, "
            f"furniture shells {estimate['furniture_distance_shells'] / 1e6:.1f}) exceeds mesh.max_global_cells "
            f"({settings['mesh']['max_global_cells'] / 1e6:.1f} million); snappyHexMesh stops refining at that limit"
        )

    # --- monitors ------------------------------------------------------------
    monitors = plan_monitors(settings, room_bounds, room_triangles, furniture_triangles, furniture_bounds, warnings)

    # --- ventilation summary -------------------------------------------------
    inlet_area = sum(p["area_on_ceiling_m2"] for p in patches if p["role"] == "inlet")
    outlet_area = sum(p["area_on_ceiling_m2"] for p in patches if p["role"] == "outlet")
    normal_speed = abs(settings["ventilation"]["inlet_velocity"][2])
    flow_rate = inlet_area * normal_speed
    bbox_volume = extents[0] * extents[1] * extents[2]
    stl_volume = abs(float(room_mesh.volume)) if room_report["is_watertight"] else None
    ventilation_summary = {
        "inlet_count": sum(1 for p in patches if p["role"] == "inlet"),
        "outlet_count": sum(1 for p in patches if p["role"] == "outlet"),
        "inlet_area_m2": inlet_area,
        "outlet_area_m2": outlet_area,
        "inlet_normal_speed_m_s": normal_speed,
        "supply_flow_rate_m3_s": flow_rate,
        "room_bbox_volume_m3": bbox_volume,
        "room_stl_volume_m3": stl_volume,
        "nominal_air_changes_per_hour": 3600.0 * flow_rate / bbox_volume,
        "nominal_air_changes_per_hour_stl_volume": (3600.0 * flow_rate / stl_volume) if stl_volume else None,
        "nominal_time_constant_s": (bbox_volume / flow_rate) if flow_rate > 0.0 else None,
        "min_ceiling_overlap_fraction": min(p["ceiling_overlap_fraction"] for p in patches),
    }

    return {
        "settings": settings,
        "config_path": cfg_path,
        "stl_dir": stl_dir,
        "room_source": room_src,
        "furniture_sources": furniture_src,
        "room_mesh": room_mesh,
        "furniture_mesh": furniture_mesh,
        "geometry_reports": reports,
        "scale": scale,
        "room_height_before_scale": current_height,
        "room_bounds": room_bounds,
        "room_extents": extents,
        "patch_scale": patch_scale,
        "patches": patches,
        "block": block,
        "diffuser_boxes": boxes,
        "location_in_mesh": location,
        "cell_estimate": estimate,
        "monitors": monitors,
        "ventilation_summary": ventilation_summary,
        "policy": {
            "trimesh_repair_enabled": repair_stl,
            "require_watertight_room": not allow_non_watertight,
            "require_watertight_furniture": strict_watertight,
            "allow_partial_patches": allow_partial_patches,
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Dictionary writers
# ---------------------------------------------------------------------------
def render_field_files(case: Path, cfg: Dict[str, Any]) -> None:
    inlet_u_txt = vec(cfg["ventilation"]["inlet_velocity"])
    scalar_initial = fmt(cfg["ventilation"]["scalar_initial_value"])
    scalar_inlet = fmt(cfg["ventilation"]["scalar_inlet_value"])
    k0 = fmt(cfg["turbulence"]["k"])
    eps0 = fmt(cfg["turbulence"]["epsilon"])

    wall_regex = '"(room|furniture|defaultFaces).*"'
    inlet_regex = '"inlet.*"'
    outlet_regex = '"outlet.*"'

    write(case / "0/U", f"""{foam_header('volVectorField', 'U')}
dimensions      [0 1 -1 0 0 0 0];
internalField   uniform (0 0 0);
boundaryField
{{
    {inlet_regex}
    {{
        type        fixedValue;
        value       uniform {inlet_u_txt};
    }}
    {outlet_regex}
    {{
        type        zeroGradient;
    }}
    {wall_regex}
    {{
        type        noSlip;
    }}
}}
""")

    write(case / "0/p", f"""{foam_header('volScalarField', 'p')}
dimensions      [0 2 -2 0 0 0 0];
internalField   uniform 0;
boundaryField
{{
    {inlet_regex}
    {{
        type        zeroGradient;
    }}
    {outlet_regex}
    {{
        type        fixedValue;
        value       uniform 0;
    }}
    {wall_regex}
    {{
        type        zeroGradient;
    }}
}}
""")

    write(case / "0/k", f"""{foam_header('volScalarField', 'k')}
dimensions      [0 2 -2 0 0 0 0];
internalField   uniform {k0};
boundaryField
{{
    {inlet_regex}
    {{
        type        fixedValue;
        value       uniform {k0};
    }}
    {outlet_regex}
    {{
        type        zeroGradient;
    }}
    {wall_regex}
    {{
        type        kqRWallFunction;
        value       uniform {k0};
    }}
}}
""")

    write(case / "0/epsilon", f"""{foam_header('volScalarField', 'epsilon')}
dimensions      [0 2 -3 0 0 0 0];
internalField   uniform {eps0};
boundaryField
{{
    {inlet_regex}
    {{
        type        fixedValue;
        value       uniform {eps0};
    }}
    {outlet_regex}
    {{
        type        zeroGradient;
    }}
    {wall_regex}
    {{
        type        epsilonWallFunction;
        value       uniform {eps0};
    }}
}}
""")

    write(case / "0/nut", f"""{foam_header('volScalarField', 'nut')}
dimensions      [0 2 -1 0 0 0 0];
internalField   uniform 0;
boundaryField
{{
    "(inlet|outlet).*"
    {{
        type        calculated;
        value       uniform 0;
    }}
    {wall_regex}
    {{
        type        nutkWallFunction;
        value       uniform 0;
    }}
}}
""")

    # T is the passive scalar (concentration C of the paper).
    write(case / "0/T", f"""{foam_header('volScalarField', 'T')}
dimensions      [0 0 0 0 0 0 0];
internalField   uniform {scalar_initial};
boundaryField
{{
    {inlet_regex}
    {{
        type        fixedValue;
        value       uniform {scalar_inlet};
    }}
    {outlet_regex}
    {{
        type        zeroGradient;
    }}
    {wall_regex}
    {{
        type        zeroGradient;
    }}
}}
""")


def render_block_mesh(case: Path, plan: Dict[str, Any]) -> None:
    block = plan["block"]
    bmin, bmax, cells = block["min"], block["max"], block["cells"]
    x0, y0, z0 = (fmt(v) for v in bmin)
    x1, y1, z1 = (fmt(v) for v in bmax)
    write(case / "system/blockMeshDict", f"""{foam_header('dictionary', 'blockMeshDict')}
// Background mesh: bounding box of the scaled room plus padding.
// Cell size {fmt(block['cell_size_m'][0])} x {fmt(block['cell_size_m'][1])} x {fmt(block['cell_size_m'][2])} m.
scale   1;

vertices
(
    ({x0} {y0} {z0})
    ({x1} {y0} {z0})
    ({x1} {y1} {z0})
    ({x0} {y1} {z0})
    ({x0} {y0} {z1})
    ({x1} {y0} {z1})
    ({x1} {y1} {z1})
    ({x0} {y1} {z1})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({cells[0]} {cells[1]} {cells[2]}) simpleGrading (1 1 1)
);

edges ();

defaultPatch
{{
    name defaultFaces;
    type wall;
}}

boundary ();
mergePatchPairs ();
""")


def render_snappy(case: Path, plan: Dict[str, Any]) -> None:
    mesh = plan["settings"]["mesh"]
    has_furniture = plan["furniture_mesh"] is not None
    room_level = mesh["room_refinement_level"]
    furniture_level = mesh["furniture_refinement_level"]
    boxes = plan["diffuser_boxes"]

    geometry = """    room
    {
        type triSurfaceMesh;
        file "room.stl";
    }
"""
    features = f'        {{ file "room.eMesh"; level {room_level[1]}; }}\n'
    surfaces = f"""        room
        {{
            level ({room_level[0]} {room_level[1]});
            patchInfo {{ type wall; }}
        }}
"""
    regions = ""
    if has_furniture:
        geometry += """    furniture
    {
        type triSurfaceMesh;
        file "furniture.stl";
    }
"""
        features += f'        {{ file "furniture.eMesh"; level {furniture_level[1]}; }}\n'
        surfaces += f"""        furniture
        {{
            level ({furniture_level[0]} {furniture_level[1]});
            patchInfo {{ type wall; }}
        }}
"""
        levels = " ".join(f"({fmt(distance)} {level})" for distance, level in mesh["furniture_distance_refinement"])
        regions += f"""        furniture
        {{
            mode distance;
            levels ({levels});
        }}
"""
    for box in boxes:
        geometry += f"""    {box['name']}
    {{
        type searchableBox;
        min {vec(box['min'])};
        max {vec(box['max'])};
    }}
"""
        regions += f"""        {box['name']}
        {{
            mode inside;
            levels ((1E15 {box['level']}));
        }}
"""

    write(case / "system/snappyHexMeshDict", f"""{foam_header('dictionary', 'snappyHexMeshDict')}
castellatedMesh true;
snap            true;
addLayers       false;

geometry
{{
{geometry}}}

castellatedMeshControls
{{
    maxLocalCells       {mesh['max_local_cells']};
    maxGlobalCells      {mesh['max_global_cells']};
    minRefinementCells  10;
    maxLoadUnbalance    0.10;
    nCellsBetweenLevels 3;

    features
    (
{features}    );

    refinementSurfaces
    {{
{surfaces}    }}

    resolveFeatureAngle 45;

    refinementRegions
    {{
{regions}    }}

    locationInMesh {vec(plan['location_in_mesh'])};
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 30;
    nRelaxIter 5;
    nFeatureSnapIter 10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes true;
    layers {{}}
    expansionRatio 1.0;
    finalLayerThickness 0.3;
    minThickness 0.1;
    nGrow 0;
}}

meshQualityControls
{{
    maxNonOrtho 65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave 80;
    minVol 1e-13;
    minTetQuality 1e-9;
    minArea -1;
    minTwist -1;
    minDeterminant 0.001;
    minFaceWeight 0.02;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
}}

mergeTolerance 1e-6;
""")


def box_action(name: str, box_min: Sequence[float], box_max: Sequence[float], action: str = "new") -> str:
    return f"""    {{
        name {name};
        type faceSet;
        action {action};
        source boxToFace;
        sourceInfo
        {{
            box {vec(box_min)} {vec(box_max)};
        }}
    }}
"""


def boundary_subset_action(name: str) -> str:
    """Keep only boundary faces in the set: createPatch aborts on internal faces."""
    return f"""    {{
        name {name};
        type faceSet;
        action subset;
        source boundaryToFace;
        sourceInfo
        {{
        }}
    }}
"""


def render_toposet(case: Path, plan: Dict[str, Any]) -> None:
    actions: List[str] = []
    for role in ("inlet", "outlet"):
        set_name = f"{role}Faces"
        first = True
        for patch in plan["patches"]:
            if patch["role"] != role:
                continue
            actions.append(f"    // {patch['name']}\n")
            actions.append(box_action(set_name, patch["box_min"], patch["box_max"], "new" if first else "add"))
            first = False
        if not first:
            actions.append(f"    // {set_name}: drop the internal faces caught by the selection slabs\n")
            actions.append(boundary_subset_action(set_name))

    actions_text = "".join(actions)
    write(case / "system/topoSetDict", f"""{foam_header('dictionary', 'topoSetDict')}
actions
(
{actions_text});
""")


def render_create_patch(case: Path) -> None:
    write(case / "system/createPatchDict", f"""{foam_header('dictionary', 'createPatchDict')}
pointSync false;

patches
(
    {{
        name inlet;
        patchInfo {{ type patch; }}
        constructFrom set;
        set inletFaces;
    }}
    {{
        name outlet;
        patchInfo {{ type patch; }}
        constructFrom set;
        set outletFaces;
    }}
);
""")


def render_system_and_constants(case: Path, plan: Dict[str, Any]) -> None:
    cfg = plan["settings"]
    sim = cfg["simulation"]
    render_block_mesh(case, plan)
    render_snappy(case, plan)
    render_toposet(case, plan)
    render_create_patch(case)

    surface_features = """room.stl
{
    extractionMethod extractFromSurface;
    includedAngle 150;
}
"""
    if plan["furniture_mesh"] is not None:
        surface_features += """
furniture.stl
{
    extractionMethod extractFromSurface;
    includedAngle 150;
}
"""
    write(case / "system/surfaceFeatureExtractDict", f"""{foam_header('dictionary', 'surfaceFeatureExtractDict')}
{surface_features}""")

    coeffs = ""
    if sim["decomposition_method"] in ("simple", "hierarchical"):
        order = "\n    order xyz;" if sim["decomposition_method"] == "hierarchical" else ""
        coeffs = f"""coeffs
{{
    n {vec(sim['decomposition'])};{order}
}}
"""
    write(case / "system/decomposeParDict", f"""{foam_header('dictionary', 'decomposeParDict')}
numberOfSubdomains {sim['default_tasks']};
method {sim['decomposition_method']};
{coeffs}""")

    # Steady airflow: a fixed iteration budget is the stopping rule.
    flow_control = f"""{foam_header('dictionary', 'controlDict')}
application     {sim['airflow_solver']};
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {fmt(sim['airflow_end_iter'] * sim['delta_t'])};
deltaT          {fmt(sim['delta_t'])};
writeControl    timeStep;
writeInterval   {sim['write_interval']};
purgeWrite      2;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

functions
{{
}}
"""
    write(case / "system/controlDict", flow_control)
    write(case / "system/controlDict.flow", flow_control)

    # Transient passive scalar on the frozen flow field, started from the last
    # flow time directory. The monitors are written to postProcessing/monitors.
    if plan["monitors"]:
        locations = "".join(
            f"            {vec(m['position_xyz'])}    // {m['name']}\n" for m in plan["monitors"]
        )
        functions = f"""    monitors
    {{
        type            probes;
        libs            (sampling);
        writeControl    timeStep;
        writeInterval   {sim['monitor_write_interval_steps']};
        fields          (T);
        probeLocations
        (
{locations}        );
    }}
"""
    else:
        functions = ""
    write(case / "system/controlDict.scalar", f"""{foam_header('dictionary', 'controlDict')}
application     {sim['scalar_solver']};
startFrom       latestTime;
stopAt          endTime;
endTime         {fmt(sim['scalar_end_time'])};
deltaT          {fmt(sim['scalar_delta_t'])};
writeControl    adjustableRunTime;
writeInterval   {fmt(sim['scalar_write_interval'])};
purgeWrite      2;
writeFormat     ascii;
writePrecision  6;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

// The frozen nut field written by simpleFoam carries wall-function patch types
// (nutkWallFunction), which are registered by libturbulenceModels. Loading the
// library here makes the field readable whatever libraries the scalar solver
// binary was linked with.
libs            (turbulenceModels);

functions
{{
{functions}}}
""")

    write(case / "system/fvSchemes", f"""{foam_header('dictionary', 'fvSchemes')}
ddtSchemes
{{
    default         Euler;
}}

gradSchemes
{{
    default         Gauss linear;
}}

divSchemes
{{
    default         none;
    div(phi,U)      Gauss upwind;
    div(phi,k)      Gauss upwind;
    div(phi,epsilon) Gauss upwind;
    div(phi,T)      Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}}

laplacianSchemes
{{
    default         Gauss linear corrected;
}}

interpolationSchemes
{{
    default         linear;
}}

snGradSchemes
{{
    default         corrected;
}}

wallDist
{{
    method          meshWave;
}}
""")

    write(case / "system/fvSolution", f"""{foam_header('dictionary', 'fvSolution')}
solvers
{{
    p
    {{
        solver          PCG;
        preconditioner  DIC;
        tolerance       1e-06;
        relTol          0.01;
    }}

    "(U|k|epsilon)"
    {{
        solver          PBiCGStab;
        preconditioner  DILU;
        tolerance       1e-07;
        relTol          0.1;
    }}

    T
    {{
        solver          PBiCGStab;
        preconditioner  DILU;
        tolerance       1e-08;
        relTol          0;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors 0;
    consistent yes;
}}

relaxationFactors
{{
    fields
    {{
        p               1.0;
    }}
    equations
    {{
        U               0.7;
        k               0.7;
        epsilon         0.7;
        T               1.0;
    }}
}}
""")

    transport = cfg["transport"]
    write(case / "constant/transportProperties", f"""{foam_header('dictionary', 'transportProperties')}
transportModel  Newtonian;
nu              [0 2 -1 0 0 0 0] {fmt(transport['kinematic_viscosity_m2_s'])};
DT              DT [0 2 -1 0 0 0 0] {fmt(transport['molecular_diffusivity_m2_s'])};
Sct             Sct [0 0 0 0 0 0 0] {fmt(transport['turbulent_schmidt_number'])};
""")

    write(case / "constant/turbulenceProperties", f"""{foam_header('dictionary', 'turbulenceProperties')}
simulationType RAS;

RAS
{{
    RASModel        {cfg['turbulence']['model']};
    turbulence      on;
    printCoeffs     on;
}}
""")


# ---------------------------------------------------------------------------
# Case output
# ---------------------------------------------------------------------------
def build_metadata(plan: Dict[str, Any], sha256: Dict[str, str]) -> Dict[str, Any]:
    cfg = plan["settings"]
    rmin, rmax = plan["room_bounds"]
    block = plan["block"]
    return {
        "generator": "cfd/scripts/01_prepare_openfoam_case.py",
        "config": str(plan["config_path"]),
        "case_name": cfg.get("case_name"),
        "paper_case": cfg.get("paper_case"),
        "description": cfg.get("description"),
        "stl_source_dir": str(plan["stl_dir"]),
        "scale_factor": plan["scale"],
        "room_height_before_scale_m": plan["room_height_before_scale"],
        "room_height_m": plan["room_extents"][2],
        "scaled_room_bounds": [list(rmin), list(rmax)],
        "scaled_room_extents_m": list(plan["room_extents"]),
        "furniture_present": plan["furniture_mesh"] is not None,
        "source_stl_sha256": sha256,
        "patch_reference_height_m": cfg["ventilation"]["patch_reference_height_m"],
        "patch_centre_scale": plan["patch_scale"],
        "ceiling_z_m": rmax[2],
        "floor_z_m": rmin[2],
        "patches": plan["patches"],
        "ventilation_summary": plan["ventilation_summary"],
        "background_mesh": {
            "box_min": block["min"],
            "box_max": block["max"],
            "cells": block["cells"],
            "cell_count": block["cell_count"],
            "cell_size_m": block["cell_size_m"],
            "target_cell_size_m": block["target_cell_size_m"],
            "explicit_cell_counts": block["explicit_cell_counts"],
        },
        "diffuser_refinement_boxes": plan["diffuser_boxes"],
        "location_in_mesh": plan["location_in_mesh"],
        "rough_cell_estimate": plan["cell_estimate"],
        "monitors": plan["monitors"],
        "stl_quality_policy": plan["policy"],
        "copied_geometry": plan["geometry_reports"],
        "mesh": cfg["mesh"],
        "simulation": cfg["simulation"],
        "transport": cfg["transport"],
        "turbulence": cfg["turbulence"],
        "ventilation": {k: v for k, v in cfg["ventilation"].items() if k != "patches"},
        "runtime": cfg["runtime"],
        "warnings": plan["warnings"],
    }


def install_allrun(case: Path) -> bool:
    """Copy cfd/templates/Allrun into the case (LF line endings, executable)."""
    if not ALLRUN_TEMPLATE.is_file():
        return False
    data = ALLRUN_TEMPLATE.read_bytes().replace(b"\r\n", b"\n")
    target = case / "Allrun"
    target.write_bytes(data)
    try:
        target.chmod(0o755)
    except OSError:
        pass
    return True


def write_case(plan: Dict[str, Any], case: Path) -> List[str]:
    """Write all case files into `case` (an empty or new directory)."""
    notes: List[str] = []
    (case / "0").mkdir(parents=True, exist_ok=True)
    (case / "constant/triSurface").mkdir(parents=True, exist_ok=True)
    (case / "system").mkdir(parents=True, exist_ok=True)

    plan["room_mesh"].export(case / "constant/triSurface/room.stl")
    if plan["furniture_mesh"] is not None:
        plan["furniture_mesh"].export(case / "constant/triSurface/furniture.stl")

    render_field_files(case, plan["settings"])
    render_system_and_constants(case, plan)

    if not install_allrun(case):
        notes.append(f"Allrun template not found ({ALLRUN_TEMPLATE}); the case has no Allrun script")

    sha256 = {str(path): sha256_file(path) for path in [plan["room_source"], *plan["furniture_sources"]]}
    metadata = build_metadata(plan, sha256)
    write(case / "cfd_case_metadata.json", json.dumps(metadata, indent=2) + "\n")
    write(
        case / "geometry_scaling.json",
        json.dumps(
            {
                "scale_factor": plan["scale"],
                "room_height_before_scale_m": plan["room_height_before_scale"],
                "room_height_m": plan["room_extents"][2],
                "scaled_room_bounds": metadata["scaled_room_bounds"],
                "copied_geometry": plan["geometry_reports"],
            },
            indent=2,
        )
        + "\n",
    )
    write(
        case / "stl_quality_report.json",
        json.dumps(
            {
                "repair_enabled": plan["policy"]["trimesh_repair_enabled"],
                "require_watertight_room": plan["policy"]["require_watertight_room"],
                "require_watertight_furniture": plan["policy"]["require_watertight_furniture"],
                "geometry": plan["geometry_reports"],
            },
            indent=2,
        )
        + "\n",
    )
    write(case / "case.foam", "")
    return notes


def summary_lines(plan: Dict[str, Any]) -> List[str]:
    extents = plan["room_extents"]
    block = plan["block"]
    summary = plan["ventilation_summary"]
    return [
        f"  scale factor     : {plan['scale']:.6g} (room height {plan['room_height_before_scale']:.6g} -> {extents[2]:.6g} m)",
        f"  room (scaled)    : {extents[0]:.3f} x {extents[1]:.3f} x {extents[2]:.3f} m",
        f"  background mesh  : {block['cells'][0]} x {block['cells'][1]} x {block['cells'][2]} cells "
        f"({block['cell_count']:,}), cell size {block['cell_size_m'][0]:.4f} x {block['cell_size_m'][1]:.4f} x {block['cell_size_m'][2]:.4f} m",
        f"  patches          : {summary['inlet_count']} inlet ({summary['inlet_area_m2']:.4g} m2), "
        f"{summary['outlet_count']} outlet ({summary['outlet_area_m2']:.4g} m2); "
        f"smallest ceiling overlap {summary['min_ceiling_overlap_fraction']:.4f}",
        f"  supply flow rate : {summary['supply_flow_rate_m3_s']:.4g} m3/s, "
        f"{summary['nominal_air_changes_per_hour']:.3g} air changes per hour "
        f"(bounding-box volume {summary['room_bbox_volume_m3']:.1f} m3)",
        f"  diffuser boxes   : {len(plan['diffuser_boxes'])}",
        f"  monitors         : {len(plan['monitors'])}",
        f"  furniture        : {'yes' if plan['furniture_mesh'] is not None else 'none (empty room)'}",
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a complete OpenFOAM case from video2cfd STL outputs.")
    parser.add_argument("--config", required=True, help="Path to CFD YAML config.")
    parser.add_argument("--output", required=True, help="Output OpenFOAM case directory.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite output directory if it already exists.")
    parser.add_argument("--no-repair-stl", action="store_true", help="Do not apply conservative trimesh repair before watertightness checks.")
    parser.add_argument(
        "--strict-watertight",
        action="store_true",
        help="Fail if the furniture STL is not watertight (default: warn; the room STL must always be watertight).",
    )
    parser.add_argument(
        "--allow-non-watertight",
        action="store_true",
        help="Deprecated. Furniture is allowed to be non-watertight by default; this flag also relaxes the room check.",
    )
    parser.add_argument(
        "--allow-partial-patches",
        action="store_true",
        help="Only warn if an inlet/outlet rectangle is not on the ceiling of the scaled room (default: fail).",
    )
    args = parser.parse_args(argv)

    if args.strict_watertight and args.allow_non_watertight:
        raise fail("--strict-watertight and --allow-non-watertight are mutually exclusive")

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    out = Path(args.output)
    if out.exists():
        if not out.is_dir():
            raise fail(f"output exists and is not a directory: {out}")
        if not args.overwrite:
            raise fail(f"output exists: {out}. Use --overwrite to replace it.")

    plan = build_case_plan(
        cfg,
        cfg_path,
        repair_stl=not args.no_repair_stl,
        strict_watertight=args.strict_watertight,
        allow_non_watertight=args.allow_non_watertight,
        allow_partial_patches=args.allow_partial_patches,
    )

    # Write into a temporary sibling directory and rename at the end, so a
    # failure never leaves a half-written case (or destroys an existing one).
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f".{out.name}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        notes = write_case(plan, tmp)
        if out.exists():
            shutil.rmtree(out)
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    for warning in plan["warnings"]:
        print(f"WARNING: {warning}")
    for note in notes:
        print(f"WARNING: {note}")
    print(f"Prepared OpenFOAM case: {out}")
    for line in summary_lines(plan):
        print(line)
    print(f"  STLs copied to   : {out / 'constant/triSurface'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
