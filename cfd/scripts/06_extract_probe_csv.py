#!/usr/bin/env python3
"""Extract OpenFOAM probe (monitor) histories to CSV and, optionally, compute
the clearance metrics of the paper.

The generated cases write the scalar monitors with a ``probes`` function object
named ``monitors``, so the raw histories are in

    <case>/postProcessing/monitors/<startTime>/T

There is one ``<startTime>`` folder per solver start; a resumed run adds a new
folder. This script reads every such file for the requested field in numeric
order of the start time, joins them (where two runs overlap in time, the later
run is kept) and writes one CSV:

    # comment lines with the probe coordinates
    time,<monitor name 1>,<monitor name 2>,...

Column names are the monitor names stored in ``cfd_case_metadata.json`` when
they are available, otherwise ``probe_0, probe_1, ...``.

With ``--metrics`` it also prints, for every probe, the clearance times t50,
t80 and t90 at which the normalised concentration C* = C / C0 falls to 0.5,
0.2 and 0.1 (linear interpolation between the two bracketing samples), and the
area under the curve AUC = integral of C* dt over the decay window. As in the
paper, time is measured from the start of the decay window and C0 is the
initial (uniform) concentration:

* t0 defaults to the start time of the scalar run, i.e. the name of the first
  ``<startTime>`` folder (1000 for the paper cases: the end of the flow solve).
  The first recorded sample is one time step later.
* C0 defaults to ``ventilation.scalar_initial_value`` of
  ``cfd_case_metadata.json`` (1 for the paper cases); without that file the
  value of the first sample is used.

``--t0`` and ``--c0`` override both. The history is anchored at (t0, C0) when
the first sample lies after t0, so the integral and the crossings cover the
whole window.

Examples:
    python cfd/scripts/06_extract_probe_csv.py runs/cfd/class_a_v2_chairs
    python cfd/scripts/06_extract_probe_csv.py runs/cfd/auditorium --metrics
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

PROBE_HEADER_RE = re.compile(r"^#\s*Probe\s+(\d+)\s*\(([^)]*)\)(.*)$")
VALUE_TOKEN_RE = re.compile(r"\([^()]*\)|[^\s()]+")
THRESHOLDS = (("t50", 0.5), ("t80", 0.2), ("t90", 0.1))
COORD_KEYS = (
    "probe_location", "probe_xyz", "position_xyz_scaled", "position_scaled",
    "xyz_scaled", "location", "position_xyz", "position", "xyz", "coordinates",
    "point",
)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def as_float(text: str):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def is_xyz(value) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


# --------------------------------------------------------------------------
# Locating and reading probe files
# --------------------------------------------------------------------------
def looks_like_probe_file(path: Path) -> bool:
    """True if the file starts with OpenFOAM probe header lines."""
    try:
        with path.open("r", errors="ignore") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                if PROBE_HEADER_RE.match(line.strip()):
                    return True
    except OSError:
        return False
    return False


def find_probe_files(case: Path, field: str, name: str | None):
    """Return (function_object_name, [(start_time, path), ...]) sorted by start time."""
    post = case / "postProcessing"
    if not post.is_dir():
        raise SystemExit(
            f"ERROR: {post} not found. The scalar run writes the monitors there; "
            "check that the case defines monitors and that the scalar stage has run."
        )
    found: dict[str, list[tuple[float, Path]]] = {}
    for fo_dir in sorted(p for p in post.iterdir() if p.is_dir()):
        if name is not None and fo_dir.name != name:
            continue
        for time_dir in fo_dir.iterdir():
            start = as_float(time_dir.name)
            if start is None or not time_dir.is_dir():
                continue
            candidate = time_dir / field
            if candidate.is_file() and looks_like_probe_file(candidate):
                found.setdefault(fo_dir.name, []).append((start, candidate))
    if not found:
        where = f"postProcessing/{name}" if name else "postProcessing/<function object>"
        raise SystemExit(
            f"ERROR: no probe file for field '{field}' found under {case} "
            f"(expected {where}/<startTime>/{field})."
        )
    if len(found) > 1:
        if "monitors" in found:
            chosen = "monitors"
        else:
            raise SystemExit(
                "ERROR: several function objects wrote probe files for field "
                f"'{field}': {', '.join(sorted(found))}. Select one with --name."
            )
    else:
        chosen = next(iter(found))
    return chosen, sorted(found[chosen], key=lambda item: item[0])


def parse_probe_file(path: Path):
    """Parse one OpenFOAM probes file.

    Returns (probes, rows, skipped) where probes is a list of dicts
    {index, xyz, found} from the '# Probe N (x y z)' header lines, rows is a
    list of (time_float, time_text, [value tokens per probe]) and skipped is
    the number of data lines that could not be used.
    """
    probes = []
    rows = []
    skipped = 0
    with path.open("r", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                match = PROBE_HEADER_RE.match(line)
                if match:
                    coords = [as_float(tok) for tok in match.group(2).split()]
                    probes.append({
                        "index": int(match.group(1)),
                        "xyz": coords if len(coords) == 3 and None not in coords else None,
                        "xyz_text": " ".join(match.group(2).split()),
                        "found": "not found" not in match.group(3).lower(),
                    })
                continue
            tokens = VALUE_TOKEN_RE.findall(line)
            if len(tokens) < 2:
                skipped += 1
                continue
            time_value = as_float(tokens[0])
            if time_value is None:
                skipped += 1
                continue
            rows.append((time_value, tokens[0], tokens[1:]))
    n_probes = len(probes)
    if n_probes == 0 and rows:
        n_probes = len(rows[0][2])
        probes = [{"index": i, "xyz": None, "xyz_text": "", "found": True} for i in range(n_probes)]
    good = [row for row in rows if len(row[2]) == n_probes]
    skipped += len(rows) - len(good)
    return probes, good, skipped


def merge_segments(segments):
    """Join the rows of several runs; where runs overlap in time keep the later run.

    segments: list of (start_time, path, rows) sorted by start time.
    Returns (rows, dropped) with rows in increasing time.
    """
    merged = []
    dropped = 0
    for _start, _path, rows in segments:
        if not rows:
            continue
        rows = sorted(rows, key=lambda row: row[0])
        first = rows[0][0]
        eps = 1e-9 * max(1.0, abs(first))
        keep = len(merged)
        while keep > 0 and merged[keep - 1][0] >= first - eps:
            keep -= 1
        dropped += len(merged) - keep
        del merged[keep:]
        merged.extend(rows)
    return merged, dropped


# --------------------------------------------------------------------------
# Monitor names
# --------------------------------------------------------------------------
def find_monitor_list(obj, depth: int = 0):
    """Depth-first search for a 'monitors' entry in the case metadata."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    monitors = obj.get("monitors")
    if isinstance(monitors, list) and monitors and all(isinstance(m, dict) for m in monitors):
        return monitors
    if isinstance(monitors, dict) and monitors:
        converted = []
        for key, value in monitors.items():
            entry = dict(value) if isinstance(value, dict) else {"position": value}
            entry.setdefault("name", key)
            converted.append(entry)
        return converted
    for value in obj.values():
        result = find_monitor_list(value, depth + 1)
        if result:
            return result
    return None


def monitor_xyz(entry: dict):
    for key in COORD_KEYS:
        if is_xyz(entry.get(key)):
            return [float(v) for v in entry[key]]
    for value in entry.values():
        if is_xyz(value):
            return [float(v) for v in value]
    return None


def clean_name(name: str) -> str:
    cleaned = re.sub(r"[\s,;\"']+", "_", str(name).strip())
    return cleaned or "probe"


def unique_names(names):
    seen: dict[str, int] = {}
    result = []
    for name in names:
        if name in seen:
            seen[name] += 1
            result.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 0
            result.append(name)
    return result


def names_from_metadata(case: Path, probes):
    """Monitor names for the probes from cfd_case_metadata.json, or None."""
    meta_path = case / "cfd_case_metadata.json"
    if not meta_path.is_file():
        return None
    try:
        metadata = json.loads(meta_path.read_text())
    except (OSError, ValueError) as exc:
        warn(f"could not read {meta_path}: {exc}")
        return None
    monitors = find_monitor_list(metadata)
    if not monitors:
        return None
    if len(monitors) != len(probes):
        warn(
            f"{meta_path.name} lists {len(monitors)} monitor(s) but the probe file has "
            f"{len(probes)} probe(s); using probe_<i> column names"
        )
        return None
    names = [clean_name(m.get("name", f"probe_{i}")) for i, m in enumerate(monitors)]
    coords = [monitor_xyz(m) for m in monitors]
    if all(c is not None for c in coords) and all(p["xyz"] is not None for p in probes):
        def close(a, b):
            return all(abs(x - y) <= 1e-3 * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))

        if not all(close(c, p["xyz"]) for c, p in zip(coords, probes)):
            # Same set in a different order? Then match by coordinates.
            order = []
            for probe in probes:
                hits = [i for i, c in enumerate(coords) if close(c, probe["xyz"])]
                order.append(hits[0] if len(hits) == 1 else None)
            if None not in order and len(set(order)) == len(order):
                names = [names[i] for i in order]
            else:
                warn(
                    "monitor coordinates in cfd_case_metadata.json do not match the probe "
                    "coordinates in the OpenFOAM output; names are assigned by position in the list"
                )
    return unique_names(names)


def initial_value_from_metadata(case: Path):
    """ventilation.scalar_initial_value of cfd_case_metadata.json, or None."""
    meta_path = case / "cfd_case_metadata.json"
    if not meta_path.is_file():
        return None
    try:
        metadata = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    ventilation = metadata.get("ventilation") if isinstance(metadata, dict) else None
    value = ventilation.get("scalar_initial_value") if isinstance(ventilation, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


# --------------------------------------------------------------------------
# Clearance metrics
# --------------------------------------------------------------------------
def crossing_time(times, values, threshold):
    """First time at which values falls from above the threshold to it or below
    (linear interpolation between the two bracketing samples); None if never."""
    for i in range(len(times) - 1):
        if values[i] > threshold and values[i + 1] <= threshold:
            fraction = (threshold - values[i]) / (values[i + 1] - values[i])
            return times[i] + fraction * (times[i + 1] - times[i])
    return None


def clearance_metrics(times, values, t0=None, c0=None):
    """t50, t80, t90, AUC and window length of one probe history.

    t0 is the start of the decay window and c0 the reference concentration
    (defaults: first sample). Samples before t0 are dropped; when the first kept
    sample lies after t0 the history is anchored at (t0, c0), as in the paper,
    where C0 is the uniform initial concentration at the window start.
    """
    if t0 is None:
        t0 = times[0]
    if c0 is None:
        c0 = values[0]
    if c0 == 0:
        raise ValueError("reference concentration C0 is zero")
    eps = 1e-9 * max(1.0, abs(t0))
    kept = [(t, v) for t, v in zip(times, values) if t >= t0 - eps]
    if not kept:
        raise ValueError(f"no samples at or after t0 = {t0:g}")
    times = [t for t, _ in kept]
    values = [v for _, v in kept]
    if times[0] > t0 + eps:
        times.insert(0, t0)
        values.insert(0, c0)
    rel_t = [t - t0 for t in times]
    c_star = [v / c0 for v in values]
    result = {"t0": t0, "c0": c0}
    for label, threshold in THRESHOLDS:
        result[label] = crossing_time(rel_t, c_star, threshold)
    result["auc"] = sum(
        0.5 * (c_star[i] + c_star[i + 1]) * (rel_t[i + 1] - rel_t[i])
        for i in range(len(rel_t) - 1)
    )
    result["window"] = rel_t[-1] - rel_t[0]
    result["c_star_end"] = c_star[-1]
    return result


def fmt(value, digits=2):
    return "not reached" if value is None else f"{value:.{digits}f}"


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract OpenFOAM probe/monitor histories to CSV and compute clearance metrics.",
    )
    parser.add_argument("case_dir", help="OpenFOAM case directory")
    parser.add_argument("--field", default="T", help="field to extract (default: T, the scalar C of the paper)")
    parser.add_argument("--name", default=None,
                        help="name of the probes function object under postProcessing/ "
                             "(default: 'monitors' if present, else the only one found)")
    parser.add_argument("--output", default="monitor_history.csv",
                        help="output CSV; a relative path is placed inside the case directory "
                             "(default: monitor_history.csv)")
    parser.add_argument("--names", default=None,
                        help="comma-separated column names, overriding cfd_case_metadata.json")
    parser.add_argument("--no-comment-header", action="store_true",
                        help="do not write the '#' comment lines with the probe coordinates")
    parser.add_argument("--metrics", action="store_true",
                        help="also print t50, t80, t90 and AUC for every probe")
    parser.add_argument("--metrics-output", default=None,
                        help="with --metrics: also write the metrics to this CSV "
                             "(relative paths are placed inside the case directory)")
    parser.add_argument("--t0", type=float, default=None,
                        help="start of the decay window for --metrics (default: the start time of the "
                             "scalar run, i.e. the name of the first postProcessing/<fo>/<startTime> folder; "
                             "1000 for the paper cases)")
    parser.add_argument("--c0", type=float, default=None,
                        help="reference concentration C0 for --metrics (default: ventilation.scalar_initial_value "
                             "of cfd_case_metadata.json, 1 for the paper cases; without that file the value "
                             "of the first sample)")
    args = parser.parse_args(argv)

    case = Path(args.case_dir)
    if not case.is_dir():
        raise SystemExit(f"ERROR: case directory not found: {case}")

    fo_name, files = find_probe_files(case, args.field, args.name)

    segments = []
    probes = None
    for start, path in files:
        seg_probes, rows, skipped = parse_probe_file(path)
        rel = path.relative_to(case).as_posix()
        if skipped:
            warn(f"{rel}: {skipped} incomplete or unreadable line(s) skipped")
        if not rows:
            warn(f"{rel}: no data rows")
            continue
        if probes is None:
            probes = seg_probes
        elif len(seg_probes) != len(probes):
            raise SystemExit(
                f"ERROR: {rel} has {len(seg_probes)} probe(s) but earlier files have "
                f"{len(probes)}; the monitors were changed between runs."
            )
        elif [p["xyz_text"] for p in seg_probes] != [p["xyz_text"] for p in probes]:
            warn(f"{rel}: probe coordinates differ from those of the earlier run(s)")
            probes = seg_probes
        segments.append((start, path, rows))
    if not segments or probes is None:
        raise SystemExit(f"ERROR: the probe files for field '{args.field}' contain no data rows.")

    rows, dropped = merge_segments(segments)

    # Column names
    n_probes = len(probes)
    if args.names:
        names = [clean_name(n) for n in args.names.split(",")]
        if len(names) != n_probes:
            raise SystemExit(f"ERROR: --names gives {len(names)} name(s) but there are {n_probes} probe(s).")
        names = unique_names(names)
    else:
        names = names_from_metadata(case, probes) or [f"probe_{p['index']}" for p in probes]

    for probe, name in zip(probes, names):
        if not probe["found"]:
            warn(
                f"probe {probe['index']} ({name}) at ({probe['xyz_text']}) was NOT FOUND in the mesh by "
                "OpenFOAM (outside the fluid or inside an obstacle); its column is not meaningful. "
                "Move the monitor in the YAML config."
            )

    # Scalar or multi-component field?
    n_comp = [len(tok.strip("()").split()) if tok.startswith("(") else 1 for tok in rows[0][2]]
    header = ["time"]
    for name, comps in zip(names, n_comp):
        if comps == 1:
            header.append(name)
        elif comps == 3:
            header.extend(f"{name}_{axis}" for axis in "xyz")
        else:
            header.extend(f"{name}_{i}" for i in range(comps))

    out = Path(args.output)
    if not out.is_absolute():
        out = case / out
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as handle:
        if not args.no_comment_header:
            handle.write(f"# OpenFOAM probe history, field {args.field}, function object {fo_name}\n")
            handle.write(f"# case: {case.resolve().name}\n")
            handle.write("# source files (start-time order): "
                         + "; ".join(path.relative_to(case).as_posix() for _s, path, _r in segments) + "\n")
            for probe, name in zip(probes, names):
                note = "" if probe["found"] else "  NOT FOUND IN MESH"
                handle.write(f"# probe {probe['index']}: {name} at ({probe['xyz_text']}) m{note}\n")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for _time, time_text, tokens in rows:
            record = [time_text]
            for tok in tokens:
                if tok.startswith("("):
                    record.extend(tok.strip("()").split())
                else:
                    record.append(tok)
            writer.writerow(record)

    print(f"Function object : {fo_name}")
    for start, path, seg_rows in segments:
        print(f"  read {path.relative_to(case).as_posix()}: {len(seg_rows)} rows, "
              f"t = {seg_rows[0][1]} .. {seg_rows[-1][1]}")
    if dropped:
        print(f"  {dropped} row(s) of earlier run(s) replaced by the later run where they overlap")
    print(f"Probes          : {', '.join(names)}")
    print(f"Wrote {out} ({len(rows)} rows, t = {rows[0][1]} .. {rows[-1][1]})")

    if args.metrics:
        if any(c != 1 for c in n_comp):
            raise SystemExit("ERROR: --metrics needs a scalar field.")
        times = [row[0] for row in rows]
        # Defaults as in the paper: the window starts when the scalar run starts
        # (the <startTime> folder of the first probe file) and C0 is the uniform
        # initial concentration of the case.
        if args.t0 is not None:
            t0, t0_source = args.t0, "--t0"
        else:
            t0 = segments[0][0]
            t0_source = f"start time of postProcessing/{fo_name}/{segments[0][1].parent.name}"
        meta_c0 = initial_value_from_metadata(case)
        if args.c0 is not None:
            c0, c0_source = args.c0, "--c0"
        elif meta_c0 is not None:
            c0, c0_source = meta_c0, "ventilation.scalar_initial_value of cfd_case_metadata.json"
        else:
            c0, c0_source = None, "first sample of each probe (cfd_case_metadata.json not found)"
        records = []
        for i, name in enumerate(names):
            values = [as_float(row[2][i]) for row in rows]
            if None in values:
                warn(f"probe {name}: non-numeric values; metrics skipped")
                continue
            try:
                metrics = clearance_metrics(times, values, t0, c0)
            except ValueError as exc:
                warn(f"probe {name}: {exc}; metrics skipped")
                continue
            metrics["name"] = name
            records.append(metrics)
        if records:
            print()
            print(f"Clearance metrics for {args.field}: elapsed time from t0 = {t0:g} ({t0_source}); "
                  f"C* = C / C0 with C0 from {c0_source}; linear interpolation between samples")
            width = max(len("probe"), max(len(r["name"]) for r in records))
            print(f"  {'probe':<{width}}  {'C0':>10}  {'t50 [s]':>12}  {'t80 [s]':>12}  {'t90 [s]':>12}  "
                  f"{'AUC [s]':>10}  {'window [s]':>10}  {'C* at end':>10}")
            for r in records:
                print(f"  {r['name']:<{width}}  {r['c0']:>10.6g}  {fmt(r['t50']):>12}  {fmt(r['t80']):>12}  "
                      f"{fmt(r['t90']):>12}  {r['auc']:>10.2f}  {r['window']:>10.2f}  {r['c_star_end']:>10.4f}")
            if args.metrics_output:
                mout = Path(args.metrics_output)
                if not mout.is_absolute():
                    mout = case / mout
                mout.parent.mkdir(parents=True, exist_ok=True)
                with mout.open("w", newline="") as handle:
                    writer = csv.writer(handle, lineterminator="\n")
                    writer.writerow(["probe", "t0", "C0", "t50_s", "t80_s", "t90_s", "AUC_s", "window_s", "C_star_end"])
                    for r in records:
                        writer.writerow([
                            r["name"], repr(r["t0"]), repr(r["c0"]),
                            "" if r["t50"] is None else f"{r['t50']:.6g}",
                            "" if r["t80"] is None else f"{r['t80']:.6g}",
                            "" if r["t90"] is None else f"{r['t90']:.6g}",
                            f"{r['auc']:.6g}", f"{r['window']:.6g}", f"{r['c_star_end']:.6g}",
                        ])
                print(f"Wrote {mout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
