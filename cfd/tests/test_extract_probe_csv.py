#!/usr/bin/env python3
"""Tests for cfd/scripts/06_extract_probe_csv.py (no OpenFOAM needed).

Each test writes a small fake case with OpenFOAM-style probe files
(postProcessing/monitors/<startTime>/T) and a cfd_case_metadata.json into a
temporary directory, runs the script in-process and checks the CSV and the
clearance metrics. The probe histories are linear decays, so the expected
crossing times are exact.

Run as a plain script:

    python cfd/tests/test_extract_probe_csv.py

or with pytest:

    pytest cfd/tests/test_extract_probe_csv.py
"""
import contextlib
import csv
import importlib.util
import io
import json
import tempfile
import traceback
from pathlib import Path

CFD_DIR = Path(__file__).resolve().parents[1]
SCRIPT = CFD_DIR / "scripts" / "06_extract_probe_csv.py"


def _load():
    spec = importlib.util.spec_from_file_location("extract_probe_csv", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXTRACT = _load()

# The scalar run starts at the end of the flow solve (time 1000) and the probes
# are written after every 0.05 s step, so the first sample is at 1000.05.
START, END, DT = 1000.0, 2200.0, 0.05
POINTS = [[2.85, 3.6, 1.1], [1.0, -1.0, 0.5]]
NAMES = ["occupied_zone", "second"]


def concentration(t: float, probe: int) -> float:
    """Linear decay from C = 1 at t = START: probe 0 reaches 0.5 / 0.2 / 0.1 after
    500 / 800 / 900 s, probe 1 twice as fast."""
    rate = 1.0 / 1000.0 if probe == 0 else 1.0 / 500.0
    return max(0.0, 1.0 - rate * (t - START))


def write_probe_file(path: Path, t_start: float, t_end: float) -> int:
    lines = [f"# Probe {i} ({' '.join(f'{v:g}' for v in POINTS[i])})" for i in range(len(POINTS))]
    lines.append("#       Time" + "".join(f"{i:>16}" for i in range(len(POINTS))))
    steps = int(round((t_end - t_start) / DT))
    for k in range(1, steps + 1):
        t = t_start + k * DT
        lines.append(f"{t:>16.8g}" + "".join(f"{concentration(t, i):>16.8g}" for i in range(len(POINTS))))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return steps


def make_case(tmp: Path, with_metadata: bool = True, initial: float = 1.0) -> Path:
    case = tmp / "case"
    write_probe_file(case / "postProcessing" / "monitors" / "1000" / "T", START, END)
    if with_metadata:
        meta = {
            "monitors": [{"name": n, "position_xyz": p} for n, p in zip(NAMES, POINTS)],
            "ventilation": {"scalar_initial_value": initial, "scalar_inlet_value": 0.0},
        }
        (case / "cfd_case_metadata.json").write_text(json.dumps(meta))
    return case


def run(case: Path, *flags: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = EXTRACT.main([str(case), *flags])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue() + err.getvalue()


def read_csv(case: Path, name: str):
    with (case / name).open() as handle:
        return [line for line in handle]


def read_metrics(case: Path):
    with (case / "clearance_metrics.csv").open() as handle:
        return {row["probe"]: row for row in csv.DictReader(handle)}


@contextlib.contextmanager
def workdir():
    with tempfile.TemporaryDirectory(prefix="video2cfd_probe_test_") as tmp:
        yield Path(tmp)


# ---------------------------------------------------------------------------
def test_csv_has_monitor_names_and_all_rows():
    with workdir() as tmp:
        case = make_case(tmp)
        code, text = run(case)
        assert code == 0, text
        lines = read_csv(case, "monitor_history.csv")
        comments = [l for l in lines if l.startswith("#")]
        data = [l for l in lines if not l.startswith("#")]
        assert data[0].strip() == "time,occupied_zone,second", data[0]
        assert len(data) == 1 + 24000, len(data)
        assert data[1].split(",")[0] == "1000.05" and data[-1].split(",")[0] == "2200"
        assert any("occupied_zone at (2.85 3.6 1.1)" in c for c in comments), comments


def test_metrics_default_to_window_start_and_initial_value():
    """Paper definition: t0 = start of the scalar run, C0 = initial concentration."""
    with workdir() as tmp:
        case = make_case(tmp)
        code, text = run(case, "--metrics", "--metrics-output", "clearance_metrics.csv")
        assert code == 0, text
        assert "t0 = 1000 (start time of postProcessing/monitors/1000)" in text, text
        assert "C0 from ventilation.scalar_initial_value of cfd_case_metadata.json" in text, text
        m = read_metrics(case)
        first = m["occupied_zone"]
        assert float(first["t0"]) == 1000.0 and float(first["C0"]) == 1.0, first
        assert abs(float(first["t50_s"]) - 500.0) < 1e-6, first
        assert abs(float(first["t80_s"]) - 800.0) < 1e-6, first
        assert abs(float(first["t90_s"]) - 900.0) < 1e-6, first
        # the window is the whole decay window, from t0 to the last sample
        assert abs(float(first["window_s"]) - 1200.0) < 1e-6, first
        # AUC of the linear decay 1 - t/1000 over 0..1000 is 500 (zero afterwards)
        assert abs(float(first["AUC_s"]) - 500.0) < 1e-3, first
        assert abs(float(m["second"]["t50_s"]) - 250.0) < 1e-6, m["second"]


def test_explicit_t0_and_c0_override_the_defaults():
    with workdir() as tmp:
        case = make_case(tmp)
        # the old behaviour (first sample as reference) requested explicitly
        code, text = run(case, "--metrics", "--metrics-output", "clearance_metrics.csv",
                         "--t0", "1000.05", "--c0", "0.99995")
        assert code == 0, text
        assert "t0 = 1000.05 (--t0)" in text and "C0 from --c0" in text, text
        first = read_metrics(case)["occupied_zone"]
        assert float(first["t0"]) == 1000.05 and abs(float(first["C0"]) - 0.99995) < 1e-12
        # C/C0 = 0.5 at 1 - (t - 1000)/1000 = 0.499975, i.e. t = 1500.025 -> 499.975 s after t0
        assert abs(float(first["t50_s"]) - 499.975) < 1e-6, first


def test_without_metadata_c0_falls_back_to_the_first_sample():
    with workdir() as tmp:
        case = make_case(tmp, with_metadata=False)
        code, text = run(case, "--metrics", "--metrics-output", "clearance_metrics.csv")
        assert code == 0, text
        assert "first sample of each probe" in text, text
        m = read_metrics(case)
        assert set(m) == {"probe_0", "probe_1"}, set(m)
        first = m["probe_0"]
        # t0 still comes from the folder name; C0 is the first sample 0.99995
        assert float(first["t0"]) == 1000.0 and abs(float(first["C0"]) - 0.99995) < 1e-12, first
        assert abs(float(first["t50_s"]) - 500.025) < 1e-6, first


def test_resumed_run_is_joined_and_keeps_the_window_start():
    with workdir() as tmp:
        case = make_case(tmp)
        # first job killed after writing up to 1600; resumed from 1500
        write_probe_file(case / "postProcessing" / "monitors" / "1000" / "T", START, 1600.0)
        write_probe_file(case / "postProcessing" / "monitors" / "1500" / "T", 1500.0, END)
        code, text = run(case, "--metrics", "--metrics-output", "clearance_metrics.csv")
        assert code == 0, text
        assert "replaced by the later run" in text, text
        data = [l for l in read_csv(case, "monitor_history.csv") if not l.startswith("#")]
        assert len(data) == 1 + 24000, len(data)
        times = [float(l.split(",")[0]) for l in data[1:]]
        assert all(b > a for a, b in zip(times, times[1:])), "times are not strictly increasing"
        first = read_metrics(case)["occupied_zone"]
        assert float(first["t0"]) == 1000.0 and abs(float(first["t50_s"]) - 500.0) < 1e-6, first


def test_missing_probe_output_is_a_clear_error():
    with workdir() as tmp:
        case = tmp / "empty_case"
        case.mkdir()
        code, text = run(case)
        assert isinstance(code, str) and "postProcessing" in code, (code, text)


# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
