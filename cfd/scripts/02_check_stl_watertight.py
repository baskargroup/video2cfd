#!/usr/bin/env python3
"""Check and optionally repair STL watertightness before OpenFOAM case generation.

This script uses trimesh for deterministic geometry preflight checks. The repair
step applies conservative cleanup operations available in trimesh, but it
cannot repair every defective STL. If a mesh is still not watertight after
repair, the script exits non-zero unless --allow-non-watertight is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import trimesh
except ImportError:
    sys.exit("ERROR: trimesh is required. Install with: pip install trimesh")


def load_mesh(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(path, force="mesh")
    if getattr(mesh, "is_empty", False):
        raise ValueError(f"Empty mesh: {path}")
    return mesh


def safe_call(obj: Any, name: str, *args: Any, **kwargs: Any) -> None:
    fn = getattr(obj, name, None)
    if callable(fn):
        fn(*args, **kwargs)


def conservative_repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Apply conservative trimesh cleanup without changing scale or coordinates."""
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
        # The final watertight check below decides pass/fail.
        pass
    safe_call(mesh, "remove_unreferenced_vertices")
    return mesh


def mesh_report(path: Path, mesh: trimesh.Trimesh, repaired: bool = False) -> dict[str, Any]:
    body_count = None
    try:
        body_count = len(mesh.split(only_watertight=False))
    except Exception:
        body_count = None

    return {
        "path": str(path),
        "repaired_output": repaired,
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "is_watertight": bool(mesh.is_watertight),
        "is_winding_consistent": bool(getattr(mesh, "is_winding_consistent", False)),
        "euler_number": int(mesh.euler_number) if getattr(mesh, "euler_number", None) is not None else None,
        "body_count": body_count,
        "bounds": mesh.bounds.tolist(),
        "extents": mesh.extents.tolist(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl", nargs="+", help="STL file(s) to check")
    parser.add_argument("--repair-output-dir", help="Write repaired STL files to this directory")
    parser.add_argument("--report", default="stl_watertight_report.json", help="JSON report path")
    parser.add_argument("--allow-non-watertight", action="store_true", help="Return success even if a mesh is not watertight")
    args = parser.parse_args()

    repair_dir = Path(args.repair_output_dir) if args.repair_output_dir else None
    if repair_dir:
        repair_dir.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    failures: list[str] = []

    for raw in args.stl:
        src = Path(raw)
        if not src.is_file():
            failures.append(f"missing file: {src}")
            continue

        try:
            mesh = load_mesh(src)
            before = mesh_report(src, mesh, repaired=False)
            after = before
            repaired_path = None

            if repair_dir and not mesh.is_watertight:
                repaired = conservative_repair(mesh)
                repaired_path = repair_dir / src.name
                repaired.export(repaired_path)
                after = mesh_report(repaired_path, repaired, repaired=True)

            reports.append({"source": before, "after_repair": after, "repaired_path": str(repaired_path) if repaired_path else None})

            if not after["is_watertight"]:
                failures.append(f"non-watertight STL: {src}")
        except Exception as exc:
            failures.append(f"{src}: {exc}")

    Path(args.report).write_text(json.dumps({"reports": reports, "failures": failures}, indent=2), encoding="utf-8")

    if failures and not args.allow_non_watertight:
        print("STL watertightness check failed:")
        for failure in failures:
            print(f"  - {failure}")
        print(f"Report: {args.report}")
        return 1

    print(f"STL watertightness report written: {args.report}")
    if failures:
        print("WARNING: non-watertight or invalid STL files were found, but --allow-non-watertight was set.")
    else:
        print("All checked STL files are watertight.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
