#!/usr/bin/env python3
"""Create a small reproducibility manifest (JSON) for an OpenFOAM case.

The manifest lists size and SHA-256 of the files that define a run:

  * case root: Allrun, cfd_case_metadata.json, geometry_scaling.json,
    stl_quality_report.json, monitor_history.csv and every log.* file that is
    present;
  * system/: every dictionary, including controlDict, controlDict.flow and
    controlDict.scalar;
  * constant/: the property dictionaries, constant/triSurface/*.stl and
    constant/polyMesh/boundary (the patch list of the final mesh);
  * 0/: the initial and boundary conditions;
  * postProcessing/: the probe (monitor) output files.

Large solution data (time directories, processor directories, the mesh itself)
is not hashed. The manifest also records the time, the OpenFOAM version of the
current environment, the git commit of this repository, the container image
digest given with --image-digest, and the SHA-256 of the scalar solver binary
if it is on PATH (run the script inside the container to record it).

Example:
    python cfd/scripts/05_make_run_manifest.py runs/cfd/class_a_v2_chairs \\
        --image-digest sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT_FILES = (
    "Allrun",
    "cfd_case_metadata.json",
    "geometry_scaling.json",
    "stl_quality_report.json",
    "monitor_history.csv",
)
CONSTANT_FILES = (
    "transportProperties",
    "turbulenceProperties",
    "polyMesh/boundary",
)
MAX_POSTPROCESSING_BYTES = 200 * 1024 * 1024


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(case: Path) -> list[Path]:
    """Files of the case that go into the manifest (paths relative to the case)."""
    paths: list[Path] = []

    for name in ROOT_FILES:
        if (case / name).is_file():
            paths.append(Path(name))
    paths.extend(sorted(p.relative_to(case) for p in case.glob("log.*") if p.is_file()))

    for sub in ("system", "0"):
        folder = case / sub
        if folder.is_dir():
            paths.extend(sorted(p.relative_to(case) for p in folder.iterdir() if p.is_file()))

    for name in CONSTANT_FILES:
        if (case / "constant" / name).is_file():
            paths.append(Path("constant") / name)
    tri = case / "constant" / "triSurface"
    if tri.is_dir():
        paths.extend(sorted(p.relative_to(case) for p in tri.iterdir()
                            if p.is_file() and p.suffix.lower() == ".stl"))

    post = case / "postProcessing"
    if post.is_dir():
        paths.extend(sorted(p.relative_to(case) for p in post.rglob("*")
                            if p.is_file() and p.stat().st_size <= MAX_POSTPROCESSING_BYTES))
    return paths


def git_commit(repo_dir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_dir")
    parser.add_argument("--output", default="cfd_run_manifest.json",
                        help="manifest file; a relative path is placed inside the case directory")
    parser.add_argument("--image-digest", default=None,
                        help="digest of the container image the case was run in (e.g. sha256:...)")
    parser.add_argument("--solver", default="scalarTransportFoamTurbulent",
                        help="name of the scalar solver executable to look up on PATH")
    args = parser.parse_args()

    case = Path(args.case_dir)
    if not case.is_dir():
        raise SystemExit(f"ERROR: case directory not found: {case}")

    out = Path(args.output)
    if not out.is_absolute():
        out = case / out

    records = []
    for rel in collect(case):
        path = case / rel
        if path.resolve() == out.resolve():
            continue
        records.append({
            "path": rel.as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        })

    solver_path = shutil.which(args.solver)
    solver = {"name": args.solver, "path": None, "sha256": None}
    if solver_path:
        solver["path"] = str(solver_path)
        solver["sha256"] = sha256(Path(solver_path))

    manifest = {
        "case_dir": str(case.resolve()),
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "openfoam_version": os.environ.get("WM_PROJECT_VERSION"),
        "repository_commit": git_commit(Path(__file__).resolve().parent),
        "image_digest": args.image_digest,
        "scalar_solver": solver,
        "file_count": len(records),
        "files": records,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="\n") as f:
        f.write(json.dumps(manifest, indent=2) + "\n")

    print(f"Wrote manifest: {out} ({len(records)} files)")
    if solver_path:
        print(f"Scalar solver: {solver_path} sha256 {solver['sha256']}")
    else:
        print(f"Scalar solver '{args.solver}' not on PATH; its hash was not recorded "
              "(run this script inside the OpenFOAM environment or container to record it).")
    if not args.image_digest:
        print("No --image-digest given; the container image is not recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
