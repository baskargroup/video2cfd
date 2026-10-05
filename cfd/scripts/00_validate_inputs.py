#!/usr/bin/env python3
"""Validate a CFD YAML config and the STL hand-over before case generation.

This is a dry run of cfd/scripts/01_prepare_openfoam_case.py: it uses the same
checks (config keys, STL files, room height, ventilation patches on the ceiling,
monitors inside the room) but writes nothing.

Exit codes: 0 = passed, 1 = validation failed, 2 = config file not found.
"""
import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Optional, Sequence


def load_generator():
    """Import 01_prepare_openfoam_case.py (its name is not a valid module name)."""
    path = Path(__file__).resolve().with_name("01_prepare_openfoam_case.py")
    spec = importlib.util.spec_from_file_location("prepare_openfoam_case", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report_failure(messages) -> int:
    print("CFD input validation failed:")
    for message in messages:
        text = str(message)
        if text.startswith("ERROR: "):
            text = text[len("ERROR: "):]
        print(f"  - {text}")
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate CFD input config and STL handoff.")
    parser.add_argument("--config", required=True, help="Path to CFD YAML config.")
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="Check only the config keys and that the STL files exist; do not load the geometry.",
    )
    parser.add_argument("--no-repair-stl", action="store_true", help="Same meaning as in 01_prepare_openfoam_case.py.")
    parser.add_argument("--strict-watertight", action="store_true", help="Same meaning as in 01_prepare_openfoam_case.py.")
    parser.add_argument("--allow-partial-patches", action="store_true", help="Same meaning as in 01_prepare_openfoam_case.py.")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"ERROR: Config does not exist: {cfg_path}", file=sys.stderr)
        return 2

    generator = load_generator()

    # 1. Keys the generator uses: geometry (room, furniture file/list/null or
    #    furniture_dir, room height), ventilation patches, monitors, transport,
    #    turbulence, mesh and simulation settings.
    try:
        cfg = generator.load_config(cfg_path)
    except SystemExit as exc:
        return report_failure([str(exc)])
    settings, errors, warnings = generator.normalise_config(cfg)
    if errors:
        for warning in warnings:
            print(f"WARNING: {warning}")
        return report_failure(errors)

    # 2. STL hand-over: source folder, room STL, furniture STL(s).
    try:
        stl_dir, room_src, furniture_src = generator.resolve_stl_sources(settings, cfg_path)
    except SystemExit as exc:
        for warning in warnings:
            print(f"WARNING: {warning}")
        return report_failure([str(exc)])

    # 3. Geometry: scaling, watertightness, patches on the ceiling, monitors.
    summary = []
    if not args.config_only:
        try:
            plan = generator.build_case_plan(
                cfg,
                cfg_path,
                repair_stl=not args.no_repair_stl,
                strict_watertight=args.strict_watertight,
                allow_partial_patches=args.allow_partial_patches,
            )
        except SystemExit as exc:
            for warning in warnings:
                print(f"WARNING: {warning}")
            return report_failure([str(exc)])
        warnings = plan["warnings"]
        summary = generator.summary_lines(plan)

    for warning in warnings:
        print(f"WARNING: {warning}")
    print("CFD input validation passed.")
    print(f"Config: {cfg_path}")
    print(f"STL source directory: {stl_dir}")
    print(f"Room STL: {room_src.name}")
    if furniture_src:
        shown = ", ".join(path.name for path in furniture_src[:4]) + (" ..." if len(furniture_src) > 4 else "")
        print(f"Furniture STL(s): {len(furniture_src)} file(s): {shown}")
    else:
        print("Furniture STL(s): none (empty room)")
    for line in summary:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
