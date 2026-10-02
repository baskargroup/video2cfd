#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path


def parse_probe_file(path: Path):
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.replace("(", " ").replace(")", " ").split()
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            continue
        if len(vals) >= 2:
            rows.append(vals)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Extract OpenFOAM probe/monitor histories to CSV.")
    ap.add_argument("case_dir", help="OpenFOAM case directory")
    ap.add_argument("--field", default="T", help="Field to extract, default T")
    ap.add_argument("--output", default="monitor_history.csv")
    args = ap.parse_args()

    case = Path(args.case_dir)
    candidates = sorted((case / "postProcessing").rglob(args.field))

    if not candidates:
        raise SystemExit(f"ERROR: no postProcessing probe files named {args.field} found under {case}")

    out = case / args.output
    with out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_file", "row_index", "time", "value_1", "value_2", "value_3"])
        for src in candidates:
            rows = parse_probe_file(src)
            for i, vals in enumerate(rows):
                padded = vals + ["", "", ""]
                writer.writerow([str(src.relative_to(case)), i, padded[0], padded[1], padded[2], padded[3]])

    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
