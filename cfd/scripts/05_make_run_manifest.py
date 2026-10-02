#!/usr/bin/env python3
"""Create a small reproducibility manifest for an OpenFOAM case."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

INCLUDE_NAMES = {
    "blockMeshDict", "snappyHexMeshDict", "surfaceFeatureExtractDict",
    "topoSetDict", "createPatchDict", "decomposeParDict", "controlDict",
    "fvSchemes", "fvSolution", "transportProperties", "turbulenceProperties",
    "U", "p", "k", "epsilon", "nut", "T",
    "log.blockMesh", "log.sFE", "log.sHM", "log.topoSet", "log.createPatch",
    "log.decomposePar", "log.simpleFoam", "log.scalarTransport",
    "log.scalarTransport_resume", "cfd_case_metadata.json",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir")
    parser.add_argument("--output", default="cfd_run_manifest.json")
    args = parser.parse_args()

    case = Path(args.case_dir)
    if not case.is_dir():
        raise SystemExit(f"ERROR: case directory not found: {case}")

    records = []
    for path in sorted(case.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith("processor") for part in path.parts):
            continue
        if path.name in INCLUDE_NAMES or path.suffix.lower() == ".stl":
            records.append({
                "path": str(path.relative_to(case)),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            })

    out = case / args.output
    out.write_text(json.dumps({
        "case_dir": str(case),
        "file_count": len(records),
        "files": records,
    }, indent=2))
    print(f"Wrote manifest: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
