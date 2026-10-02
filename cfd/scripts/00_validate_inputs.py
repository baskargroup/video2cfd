#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required. Install with: pip install pyyaml")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CFD input config and STL handoff.")
    parser.add_argument("--config", required=True, help="Path to CFD YAML config.")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"ERROR: Config does not exist: {cfg_path}", file=sys.stderr)
        return 2

    cfg = yaml.safe_load(cfg_path.read_text())
    errors = []
    warnings = []

    geom = cfg.get("geometry", {})
    stl_dir = Path(geom.get("stl_source_dir", ""))

    if not stl_dir.is_dir():
        errors.append(f"Missing geometry.stl_source_dir: {stl_dir}")

    for key in ("room_stl", "furniture_stl"):
        name = geom.get(key)
        if not name:
            errors.append(f"Missing geometry.{key} in config")
            continue
        path = stl_dir / name
        if not path.is_file():
            errors.append(f"Missing required STL: {path}")

    scene_json = stl_dir / "scene.json"
    if not scene_json.is_file():
        warnings.append(
            f"scene.json not found at {scene_json}. The paper workflow lists STLs & scene.json as CFD raw inputs."
        )

    if geom.get("measured_height_m") is None:
        errors.append("Missing geometry.measured_height_m")

    sim = cfg.get("simulation", {})
    for key in ("airflow_solver", "scalar_solver", "openfoam_version"):
        if key not in sim:
            errors.append(f"Missing simulation.{key}")

    transport = cfg.get("transport", {})
    for key in ("molecular_diffusivity_m2_s", "turbulent_schmidt_number"):
        if key not in transport:
            errors.append(f"Missing transport.{key}")

    ventilation = cfg.get("ventilation", {})
    if "inlet_velocity" not in ventilation:
        errors.append("Missing ventilation.inlet_velocity")

    turbulence = cfg.get("turbulence", {})
    for key in ("k", "epsilon"):
        if key not in turbulence:
            errors.append(f"Missing turbulence.{key}")

    for w in warnings:
        print(f"WARNING: {w}")

    if errors:
        print("CFD input validation failed:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("CFD input validation passed.")
    print(f"Config: {cfg_path}")
    print(f"STL source directory: {stl_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
