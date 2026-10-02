#!/usr/bin/env python3
"""Prepare a complete OpenFOAM case from video2cfd STL outputs.

This script is self-contained: it does not require the historical production zip
archive. It generates the OpenFOAM dictionaries directly from a YAML config and
copies/scales the STL geometry into constant/triSurface.
"""
import argparse
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required. Install with: pip install pyyaml")

try:
    import trimesh
except ImportError:
    sys.exit("ERROR: trimesh is required. Install with: pip install trimesh")

Vector = Tuple[float, float, float]


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


def load_mesh(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(path, force="mesh")
    if mesh.is_empty:
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


def quality_report(path: Path, mesh: trimesh.Trimesh, label: str) -> Dict[str, Any]:
    try:
        body_count = len(mesh.split(only_watertight=False))
    except Exception:
        body_count = None
    return {
        "label": label,
        "path": str(path),
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "is_watertight": bool(mesh.is_watertight),
        "is_winding_consistent": bool(getattr(mesh, "is_winding_consistent", False)),
        "euler_number": int(mesh.euler_number) if getattr(mesh, "euler_number", None) is not None else None,
        "body_count": body_count,
        "bounds": mesh.bounds.tolist(),
        "extents": mesh.extents.tolist(),
    }


def export_scaled_stl(
    src: Path,
    dst: Path,
    scale: float,
    *,
    label: str,
    repair_stl: bool,
    require_watertight: bool,
) -> Dict[str, Any]:
    mesh = load_mesh(src)
    before = quality_report(src, mesh, label)

    repaired = False
    if repair_stl and not mesh.is_watertight:
        mesh = conservative_repair(mesh)
        repaired = True

    after_repair = quality_report(src, mesh, label)
    if require_watertight and not mesh.is_watertight:
        raise SystemExit(
            f"ERROR: {label} STL is not watertight after conservative trimesh repair: {src}\n"
            "       Fix the STL upstream or rerun with --allow-non-watertight only for debugging."
        )

    bounds_before_scale = mesh.bounds.tolist()
    if not math.isclose(scale, 1.0):
        mesh.apply_scale(scale)
    mesh.export(dst)
    final = quality_report(dst, mesh, label)

    return {
        "source": str(src),
        "target": str(dst),
        "scale": scale,
        "repaired_by_trimesh": repaired,
        "quality_before_repair": before,
        "quality_after_repair": after_repair,
        "bounds_before_scale": bounds_before_scale,
        "quality_final_written_stl": final,
    }


def bounds_tuple(bounds: Iterable[Iterable[float]]) -> Tuple[Vector, Vector]:
    b = list(bounds)
    return (float(b[0][0]), float(b[0][1]), float(b[0][2])), (float(b[1][0]), float(b[1][1]), float(b[1][2]))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def render_field_files(case: Path, cfg: Dict[str, Any]) -> None:
    inlet_u = cfg.get("ventilation", {}).get("inlet_velocity", [0.0, 0.0, -0.5])
    inlet_u_txt = f"({inlet_u[0]} {inlet_u[1]} {inlet_u[2]})"
    scalar_initial = cfg.get("ventilation", {}).get("scalar_initial_value", 1.0)
    scalar_inlet = cfg.get("ventilation", {}).get("scalar_inlet_value", 0.0)
    k0 = cfg.get("turbulence", {}).get("k", 0.015)
    eps0 = cfg.get("turbulence", {}).get("epsilon", 0.004)

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


def render_block_mesh(case: Path, room_bounds: Tuple[Vector, Vector], cfg: Dict[str, Any]) -> None:
    rmin, rmax = room_bounds
    pad = float(cfg.get("mesh", {}).get("background_padding_m", 0.01))
    bmin = (rmin[0] - pad, rmin[1] - pad, rmin[2] - pad)
    bmax = (rmax[0] + pad, rmax[1] + pad, rmax[2] + pad)
    cells = cfg.get("mesh", {}).get("background_cells", [60, 60, 30])
    write(case / "system/blockMeshDict", f"""{foam_header('dictionary', 'blockMeshDict')}
scale   1;

vertices
(
    ({bmin[0]} {bmin[1]} {bmin[2]})
    ({bmax[0]} {bmin[1]} {bmin[2]})
    ({bmax[0]} {bmax[1]} {bmin[2]})
    ({bmin[0]} {bmax[1]} {bmin[2]})
    ({bmin[0]} {bmin[1]} {bmax[2]})
    ({bmax[0]} {bmin[1]} {bmax[2]})
    ({bmax[0]} {bmax[1]} {bmax[2]})
    ({bmin[0]} {bmax[1]} {bmax[2]})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({cells[0]} {cells[1]} {cells[2]}) simpleGrading (1 1 1)
);

edges ();
boundary ();
mergePatchPairs ();
""")


def render_snappy(case: Path, room_bounds: Tuple[Vector, Vector], cfg: Dict[str, Any]) -> None:
    rmin, rmax = room_bounds
    loc = cfg.get("mesh", {}).get("location_in_mesh")
    if loc is None:
        loc = [(rmin[0] + rmax[0]) / 2.0, (rmin[1] + rmax[1]) / 2.0, (rmin[2] + rmax[2]) / 2.0]
    mesh = cfg.get("mesh", {})
    max_local = mesh.get("max_local_cells", 2_000_000)
    max_global = mesh.get("max_global_cells", 15_000_000)
    room_level = mesh.get("room_refinement_level", [2, 2])
    furniture_level = mesh.get("furniture_refinement_level", [3, 4])
    write(case / "system/snappyHexMeshDict", f"""{foam_header('dictionary', 'snappyHexMeshDict')}
castellatedMesh true;
snap            true;
addLayers       false;

geometry
{{
    room
    {{
        type triSurfaceMesh;
        file "room.stl";
    }}
    furniture
    {{
        type triSurfaceMesh;
        file "furniture.stl";
    }}
}}

castellatedMeshControls
{{
    maxLocalCells       {max_local};
    maxGlobalCells      {max_global};
    minRefinementCells  10;
    maxLoadUnbalance    0.10;
    nCellsBetweenLevels 3;

    features
    (
        {{ file "room.eMesh"; level {room_level[1]}; }}
        {{ file "furniture.eMesh"; level {furniture_level[1]}; }}
    );

    refinementSurfaces
    {{
        room
        {{
            level ({room_level[0]} {room_level[1]});
            patchInfo {{ type wall; }}
        }}
        furniture
        {{
            level ({furniture_level[0]} {furniture_level[1]});
            patchInfo {{ type wall; }}
        }}
    }}

    resolveFeatureAngle 45;

    refinementRegions
    {{
        furniture
        {{
            mode distance;
            levels ((0.02 {furniture_level[1]}) (0.05 {furniture_level[0]}));
        }}
    }}

    locationInMesh ({loc[0]} {loc[1]} {loc[2]});
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 30;
    nRelaxIter 5;
    nFeatureSnapIter 10;
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
    errorReduction 100;
}}

mergeTolerance 1e-6;
""")


def box_action(name: str, box_min: Vector, box_max: Vector, action: str = "new") -> str:
    return f"""    {{
        name {name};
        type faceSet;
        action {action};
        source boxToFace;
        sourceInfo
        {{
            box ({box_min[0]} {box_min[1]} {box_min[2]}) ({box_max[0]} {box_max[1]} {box_max[2]});
        }}
    }}
"""


def render_toposet(case: Path, room_bounds: Tuple[Vector, Vector], cfg: Dict[str, Any]) -> None:
    rmin, rmax = room_bounds
    z_top = rmax[2]
    dz = float(cfg.get("ventilation", {}).get("ceiling_selection_half_thickness_m", 0.05))
    patches = cfg.get("ventilation", {}).get("patches", [])
    if not patches:
        raise SystemExit("ERROR: config must define ventilation.patches for inlet/outlet face selection")

    actions: List[str] = []
    for patch in patches:
        name = patch["name"]
        role = patch["role"]
        set_name = f"{role}Faces"
        shape = patch.get("shape", "rectangle")
        if shape != "rectangle":
            raise SystemExit(f"ERROR: unsupported patch shape '{shape}' for {name}; currently supported: rectangle")
        cx, cy = patch["center_xy"]
        sx, sy = patch["size_xy"]
        z = patch.get("z", z_top)
        bmin = (cx - sx / 2.0, cy - sy / 2.0, z - dz)
        bmax = (cx + sx / 2.0, cy + sy / 2.0, z + dz)
        actions.append(box_action(set_name, bmin, bmax, "new" if not any(f"name {set_name};" in a for a in actions) else "add"))

    actions_text = "".join(actions)
    write(case / "system/topoSetDict", f"""{foam_header('dictionary', 'topoSetDict')}
actions
(
{actions_text});
""")


def render_create_patch(case: Path) -> None:
    write(case / "system/createPatchDict", f"""{foam_header('dictionary', 'createPatchDict')}
pointSync false;
purgeEmptyPatches true;

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


def render_system_and_constants(case: Path, cfg: Dict[str, Any], room_bounds: Tuple[Vector, Vector]) -> None:
    render_block_mesh(case, room_bounds, cfg)
    render_snappy(case, room_bounds, cfg)
    render_toposet(case, room_bounds, cfg)
    render_create_patch(case)

    nprocs = int(cfg.get("simulation", {}).get("default_tasks", 56))
    decomp = cfg.get("simulation", {}).get("decomposition", [8, 7, 1])
    airflow_end = cfg.get("simulation", {}).get("airflow_end_iter", 1000)
    scalar_end = cfg.get("simulation", {}).get("scalar_end_time", 5000)
    write_interval = cfg.get("simulation", {}).get("write_interval", 100)
    delta_t = cfg.get("simulation", {}).get("delta_t", 1)
    dt = cfg.get("transport", {}).get("molecular_diffusivity_m2_s", 1.5e-5)
    sct = cfg.get("transport", {}).get("turbulent_schmidt_number", 0.7)

    write(case / "system/surfaceFeatureExtractDict", f"""{foam_header('dictionary', 'surfaceFeatureExtractDict')}
room.stl
{{
    extractionMethod extractFromSurface;
    includedAngle 150;
}}

furniture.stl
{{
    extractionMethod extractFromSurface;
    includedAngle 150;
}}
""")

    write(case / "system/decomposeParDict", f"""{foam_header('dictionary', 'decomposeParDict')}
numberOfSubdomains {nprocs};
method          simple;
coeffs
{{
    n ({decomp[0]} {decomp[1]} {decomp[2]});
}}
""")

    write(case / "system/controlDict", f"""{foam_header('dictionary', 'controlDict')}
application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {airflow_end};
deltaT          {delta_t};
writeControl    timeStep;
writeInterval   {write_interval};
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
""")

    write(case / "system/controlDict.scalar", f"""{foam_header('dictionary', 'controlDict')}
application     scalarTransportFoamTurbulent;
startFrom       latestTime;
stopAt          endTime;
endTime         {scalar_end};
deltaT          {cfg.get('simulation', {}).get('scalar_delta_t', 0.05)};
writeControl    adjustableRunTime;
writeInterval   {cfg.get('simulation', {}).get('scalar_write_interval', 20)};
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
""")

    rmin, rmax = room_bounds
    loc = [(rmin[0] + rmax[0]) / 2.0, (rmin[1] + rmax[1]) / 2.0, (rmin[2] + rmax[2]) / 2.0]
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

    "(U|k|epsilon|T)"
    {{
        solver          PBiCGStab;
        preconditioner  DILU;
        tolerance       1e-07;
        relTol          0.1;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors 2;
    consistent yes;
    pRefPoint ({loc[0]} {loc[1]} {loc[2]});
    pRefValue 0;
    residualControl
    {{
        p               1e-4;
        U               1e-4;
    }}
}}

PIMPLE
{{
    nCorrectors 1;
    nNonOrthogonalCorrectors 1;
}}

relaxationFactors
{{
    fields
    {{
        p               0.3;
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

    write(case / "constant/transportProperties", f"""{foam_header('dictionary', 'transportProperties')}
transportModel  Newtonian;
nu              [0 2 -1 0 0 0 0] 1.5e-05;
DT              DT [0 2 -1 0 0 0 0] {dt};
Sct             Sct [0 0 0 0 0 0 0] {sct};
""")

    write(case / "constant/turbulenceProperties", f"""{foam_header('dictionary', 'turbulenceProperties')}
simulationType RAS;

RAS
{{
    RASModel        kEpsilon;
    turbulence      on;
    printCoeffs     on;
}}
""")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a complete OpenFOAM case from video2cfd STL outputs.")
    parser.add_argument("--config", required=True, help="Path to CFD YAML config.")
    parser.add_argument("--output", required=True, help="Output OpenFOAM case directory.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite output directory if it already exists.")
    parser.add_argument("--no-repair-stl", action="store_true", help="Do not apply conservative trimesh repair before watertightness checks.")
    parser.add_argument("--allow-non-watertight", action="store_true", help="Allow non-watertight STL files. Use only for debugging; default is to fail.")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text())
    geom = cfg["geometry"]
    stl_dir = Path(geom["stl_source_dir"])
    room_src = stl_dir / geom.get("room_stl", "room.stl")
    furn_src = stl_dir / geom.get("furniture_stl", "furniture.stl")
    out = Path(args.output)

    if not room_src.is_file():
        raise SystemExit(f"ERROR: missing room STL: {room_src}")
    if not furn_src.is_file():
        raise SystemExit(f"ERROR: missing furniture STL: {furn_src}")
    if out.exists():
        if not args.overwrite:
            raise SystemExit(f"ERROR: output exists: {out}. Use --overwrite to replace it.")
        shutil.rmtree(out)

    (out / "0").mkdir(parents=True)
    (out / "constant/triSurface").mkdir(parents=True)
    (out / "system").mkdir(parents=True)

    room_mesh = load_mesh(room_src)
    rmin0, rmax0 = bounds_tuple(room_mesh.bounds)
    current_height = rmax0[2] - rmin0[2]
    measured_height = geom.get("measured_height_m")
    scale = 1.0
    if geom.get("scale_to_measured_height", True):
        if measured_height is None:
            raise SystemExit("ERROR: measured_height_m is required when scale_to_measured_height is true")
        scale = float(measured_height) / float(current_height)

    repair_stl = not args.no_repair_stl
    require_watertight = not args.allow_non_watertight

    copied = []
    copied.append(export_scaled_stl(room_src, out / "constant/triSurface/room.stl", scale, label="room", repair_stl=repair_stl, require_watertight=require_watertight))
    copied.append(export_scaled_stl(furn_src, out / "constant/triSurface/furniture.stl", scale, label="furniture", repair_stl=repair_stl, require_watertight=require_watertight))

    combined_src = stl_dir / geom.get("combined_stl", "furniture_and_room.stl")
    if combined_src.is_file():
        copied.append(export_scaled_stl(combined_src, out / "constant/triSurface/furniture_and_room.stl", scale, label="combined", repair_stl=repair_stl, require_watertight=require_watertight))

    scaled_room = load_mesh(out / "constant/triSurface/room.stl")
    room_bounds = bounds_tuple(scaled_room.bounds)

    render_field_files(out, cfg)
    render_system_and_constants(out, cfg, room_bounds)

    metadata = {
        "config": str(cfg_path),
        "case_name": cfg.get("case_name"),
        "stl_source_dir": str(stl_dir),
        "scale_factor": scale,
        "room_height_before_scale_m": current_height,
        "measured_height_m": measured_height,
        "scaled_room_bounds": scaled_room.bounds.tolist(),
        "stl_quality_policy": {"trimesh_repair_enabled": repair_stl, "require_watertight": require_watertight},
        "copied_geometry": copied,
        "simulation": cfg.get("simulation", {}),
        "transport": cfg.get("transport", {}),
        "ventilation": cfg.get("ventilation", {}),
    }
    write(out / "cfd_case_metadata.json", json.dumps(metadata, indent=2))
    write(out / "geometry_scaling.json", json.dumps({"scale_factor": scale, "copied_geometry": copied}, indent=2))
    write(out / "stl_quality_report.json", json.dumps({"repair_enabled": repair_stl, "require_watertight": require_watertight, "geometry": copied}, indent=2))
    write(out / "case.foam", "")

    print(f"Prepared OpenFOAM case: {out}")
    print(f"Scale factor: {scale}")
    print(f"STLs copied to: {out / 'constant/triSurface'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
