#!/usr/bin/env python3
"""Find a ParaView executable (pvpython, pvbatch or paraview) for the optional
rendering step. Looks at the environment variables PVPYTHON/PVBATCH/PARAVIEW,
then PATH, then a few common install locations. Linux/macOS only.

With --batch-only the GUI executable 'paraview' is ignored, because it cannot
run a Python script without a display.
"""
import argparse
import os
import sys
from pathlib import Path


NAMES = ("pvpython", "pvbatch", "paraview")


def is_exe(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def candidates():
    # 1. Explicit env vars first
    for env in ("PVPYTHON", "PVBATCH", "PARAVIEW"):
        val = os.environ.get(env)
        if val:
            p = Path(val).expanduser()
            if is_exe(p):
                yield p

    # 2. PATH
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for name in NAMES:
            p = Path(d) / name
            if is_exe(p):
                yield p

    # 3. Common extracted binary locations
    roots = [
        Path.home() / "Downloads",
        Path.home() / "bin",
        Path.home() / "software",
        Path.home() / "apps",
        Path("/opt"),
        Path("/usr/local"),
    ]

    patterns = [
        "ParaView*/bin/pvpython",
        "ParaView*/bin/pvbatch",
        "ParaView*/bin/paraview",
        "paraview*/bin/pvpython",
        "paraview*/bin/pvbatch",
        "paraview*/bin/paraview",
    ]

    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            for p in root.glob(pat):
                if is_exe(p):
                    yield p


def main():
    ap = argparse.ArgumentParser(description="Find a ParaView executable usable for automated rendering.")
    ap.add_argument("--prefer", choices=["pvpython", "pvbatch", "paraview"], default="pvpython")
    ap.add_argument("--print-all", action="store_true")
    ap.add_argument("--batch-only", action="store_true",
                    help="only accept pvpython or pvbatch (the GUI 'paraview' cannot run scripts headless)")
    args = ap.parse_args()

    found = []
    seen = set()
    for p in candidates():
        rp = str(p.resolve())
        if rp not in seen:
            seen.add(rp)
            found.append(p.resolve())

    def rank(p: Path):
        name = p.name
        if name == args.prefer:
            return 0
        if name == "pvpython":
            return 1
        if name == "pvbatch":
            return 2
        if name == "paraview":
            return 3
        return 4

    if args.batch_only:
        found = [p for p in found if p.name in ("pvpython", "pvbatch")]

    found.sort(key=rank)

    if args.print_all:
        for p in found:
            print(p)
        return 0 if found else 1

    if not found:
        print(
            "ERROR: ParaView executable not found. Install ParaView or set PVPYTHON/PVBATCH (or PARAVIEW).",
            file=sys.stderr,
        )
        return 1

    print(found[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
