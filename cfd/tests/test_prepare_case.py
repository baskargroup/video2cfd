#!/usr/bin/env python3
"""Tests for the OpenFOAM case generator (no OpenFOAM installation needed).

Each test builds a small box room and two box "furniture" pieces with trimesh in
a temporary directory, writes a config, runs cfd/scripts/01_prepare_openfoam_case.py
and checks the generated case against the settings stated in the paper.

Run as a plain script:

    python cfd/tests/test_prepare_case.py

or with pytest:

    pytest cfd/tests/test_prepare_case.py
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import trimesh
import yaml

CFD_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = CFD_DIR / "scripts"
GENERATOR_PATH = SCRIPTS / "01_prepare_openfoam_case.py"
VALIDATOR_PATH = SCRIPTS / "00_validate_inputs.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _load(GENERATOR_PATH, "prepare_openfoam_case")
VALIDATE = _load(VALIDATOR_PATH, "validate_inputs")

# Test room in STL units: x [-1.5, 2.5], y [-2, 3], z [-0.5, 2.0] (height 2.5).
# It is scaled to a height of 3.0 m, i.e. by 1.2 about the origin:
# x [-1.8, 3.0], y [-2.4, 3.6], z [-0.6, 2.4].
ROOM_EXTENTS = (4.0, 5.0, 2.5)
ROOM_CENTRE = (0.5, 0.5, 0.75)
SCALE = 1.2
SCALED_MIN = (-1.8, -2.4, -0.6)
SCALED_MAX = (3.0, 3.6, 2.4)

EXPECTED_FILES = [
    "0/U",
    "0/p",
    "0/k",
    "0/epsilon",
    "0/nut",
    "0/T",
    "constant/transportProperties",
    "constant/turbulenceProperties",
    "constant/triSurface/room.stl",
    "system/blockMeshDict",
    "system/snappyHexMeshDict",
    "system/surfaceFeatureExtractDict",
    "system/topoSetDict",
    "system/createPatchDict",
    "system/decomposeParDict",
    "system/controlDict",
    "system/controlDict.flow",
    "system/controlDict.scalar",
    "system/fvSchemes",
    "system/fvSolution",
    "cfd_case_metadata.json",
    "geometry_scaling.json",
    "stl_quality_report.json",
    "case.foam",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _box(extents, centre):
    mesh = trimesh.creation.box(extents=extents)
    mesh.apply_translation(centre)
    return mesh


def make_geometry(folder: Path) -> None:
    """Write room.stl, furniture.stl and the two furniture pieces as separate files."""
    folder.mkdir(parents=True, exist_ok=True)
    _box(ROOM_EXTENTS, ROOM_CENTRE).export(folder / "room.stl")
    piece_a = _box((0.4, 0.4, 0.8), (0.0, 0.0, -0.1))
    piece_b = _box((1.0, 0.6, 0.7), (1.0, 1.5, -0.15))
    trimesh.util.concatenate([piece_a, piece_b]).export(folder / "furniture.stl")
    (folder / "parts").mkdir(exist_ok=True)
    piece_a.export(folder / "parts" / "piece_a.stl")
    piece_b.export(folder / "parts" / "piece_b.stl")


def base_config(stl_dir: Path) -> dict:
    return {
        "case_name": "test_room",
        "description": "Synthetic box room for the generator tests.",
        "paper_case": "test",
        "geometry": {
            "stl_source_dir": str(stl_dir),
            "room_stl": "room.stl",
            "furniture_stl": "furniture.stl",
            "room_height_m": 3.0,
            "scale_to_room_height": True,
        },
        "ventilation": {
            "inlet_velocity": [0.0, 0.0, -0.5],
            "scalar_initial_value": 1.0,
            "scalar_inlet_value": 0.0,
            "ceiling_selection_half_thickness_m": 0.05,
            "patch_reference_height_m": 2.5,
            "patches": [
                {"name": "inlet_1", "role": "inlet", "shape": "rectangle", "center_xy": [0.0, 0.0], "size_xy": [0.4, 0.4]},
                {"name": "inlet_2", "role": "inlet", "shape": "rectangle", "center_xy": [1.0, 1.0], "size_xy": [0.4, 0.4]},
                {"name": "outlet_1", "role": "outlet", "shape": "rectangle", "center_xy": [-1.2, -1.7], "size_xy": [0.5, 0.5]},
                {"name": "outlet_2", "role": "outlet", "shape": "rectangle", "center_xy": [2.2, 2.7], "size_xy": [0.5, 0.5]},
            ],
        },
        "monitors": [
            {"name": "centre", "position_rel": [0.5, 0.5], "height_above_floor_m": 1.1},
            {"name": "absolute", "position_xyz": [1.0, -1.0, 0.5]},
            {"name": "fraction", "position_rel": [0.25, 0.25, 0.5]},
        ],
        "transport": {"scalar_field_name": "T", "molecular_diffusivity_m2_s": 1.5e-5, "turbulent_schmidt_number": 0.7},
        "turbulence": {"k": 0.015, "epsilon": 0.004},
        "mesh": {
            "background_cell_size_m": 0.25,
            "background_padding_m": 0.01,
            "max_local_cells": 2000000,
            "max_global_cells": 20000000,
            "room_refinement_level": [2, 2],
            "furniture_refinement_level": [3, 4],
            "furniture_distance_refinement": [[0.02, 4], [0.05, 3]],
            "diffuser_refinement": {"enabled": True, "level": 3, "bottom_height_above_floor_m": 0.5, "margin_m": 0.0},
        },
        "simulation": {
            "openfoam_version": "2412",
            "airflow_solver": "simpleFoam",
            "scalar_solver": "scalarTransportFoamTurbulent",
            "airflow_end_iter": 1000,
            "scalar_end_time": 2200,
            "scalar_delta_t": 0.05,
            "write_interval": 100,
            "scalar_write_interval": 20,
            "monitor_write_interval_steps": 1,
            "default_tasks": 56,
            "decomposition_method": "scotch",
        },
        "runtime": {"docker_image": "example/image:tag", "apptainer_image": "example.sif"},
    }


def write_config(path: Path, cfg: dict) -> Path:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False)
    return path


def run_generator(cfg_path: Path, out: Path, *flags: str):
    """Run the generator in-process. Returns (exit code or SystemExit payload, printed text)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            code = GEN.main(["--config", str(cfg_path), "--output", str(out), *flags])
        except SystemExit as exc:
            code = exc.code
    return code, buffer.getvalue()


def generate(tmp: Path, mutate=None, *flags: str, name: str = "case"):
    """Build geometry + config in tmp, run the generator, return (code, text, case dir)."""
    stl_dir = tmp / "stl"
    if not (stl_dir / "room.stl").is_file():
        make_geometry(stl_dir)
    cfg = base_config(stl_dir)
    if mutate is not None:
        mutate(cfg)
    cfg_path = write_config(tmp / f"{name}.yaml", cfg)
    out = tmp / "out" / name
    code, text = run_generator(cfg_path, out, *flags)
    return code, text, out


def read(case: Path, relative: str) -> str:
    return (case / relative).read_bytes().decode("utf-8")


def metadata(case: Path) -> dict:
    return json.loads(read(case, "cfd_case_metadata.json"))


def assert_fails(code, *fragments: str) -> None:
    assert isinstance(code, str) and code.startswith("ERROR:"), f"expected an ERROR exit, got {code!r}"
    for fragment in fragments:
        assert fragment in code, f"{fragment!r} not in error message: {code}"


def assert_no_leftovers(out: Path) -> None:
    assert not out.exists(), f"failed run left the output directory behind: {out}"
    if out.parent.exists():
        leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(f".{out.name}.tmp")]
        assert not leftovers, f"failed run left temporary directories behind: {leftovers}"


def toposet_actions(text: str):
    pattern = re.compile(r"name\s+(\w+);\s*type\s+faceSet;\s*action\s+(\w+);\s*source\s+(\w+);")
    return pattern.findall(text)


def dict_block(text: str, keyword: str) -> str:
    """Return the body of the first `keyword { ... }` block (no nested-brace keywords inside needed)."""
    match = re.search(r"(?m)^\s*" + re.escape(keyword) + r"\s*\{", text)
    assert match, f"block {keyword!r} not found"
    depth, start = 1, match.end()
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError(f"block {keyword!r} is not closed")


@contextlib.contextmanager
def workdir():
    with tempfile.TemporaryDirectory(prefix="video2cfd_cfd_test_") as tmp:
        yield Path(tmp)


# ---------------------------------------------------------------------------
# Generated files
# ---------------------------------------------------------------------------
def test_expected_files_exist_with_lf_line_endings():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        for relative in EXPECTED_FILES + ["constant/triSurface/furniture.stl"]:
            assert (case / relative).is_file(), f"missing {relative}"
        assert not (case / "constant/triSurface/furniture_and_room.stl").exists()
        template = CFD_DIR / "templates" / "Allrun"
        if template.is_file():
            assert (case / "Allrun").is_file(), "Allrun template exists but was not copied into the case"
            assert (case / "Allrun").read_bytes() == template.read_bytes().replace(b"\r\n", b"\n")
        else:
            assert "Allrun template not found" in text
        for path in case.rglob("*"):
            if path.is_file() and path.suffix != ".stl":
                assert b"\r" not in path.read_bytes(), f"CR line ending in {path.relative_to(case)}"
        assert read(case, "system/controlDict") == read(case, "system/controlDict.flow")


def test_decompose_par_dict_uses_scotch():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        decompose = read(case, "system/decomposeParDict")
        assert "method scotch;" in decompose
        assert re.search(r"numberOfSubdomains\s+56;", decompose)
        assert "simple" not in decompose and "coeffs" not in decompose


def test_fv_solution_matches_paper():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        solution = read(case, "system/fvSolution")
        assert re.findall(r"nNonOrthogonalCorrectors\s+(\S+);", solution) == ["0"]
        assert "residualControl" not in solution
        assert re.search(r"consistent\s+yes;", dict_block(solution, "SIMPLE"))
        relaxation = dict_block(solution, "relaxationFactors")
        assert re.search(r"p\s+1(\.0)?;", dict_block(relaxation, "fields"))
        equations = dict_block(relaxation, "equations")
        for name, value in (("U", "0.7"), ("k", "0.7"), ("epsilon", "0.7"), ("T", "1.0")):
            assert re.search(r"(?m)^\s*" + name + r"\s+" + re.escape(value) + ";", equations), name
        solvers = dict_block(solution, "solvers")
        t_solver = dict_block(solvers, "T")
        assert re.search(r"tolerance\s+1e-08;", t_solver)
        assert re.search(r"relTol\s+0;", t_solver)
        assert re.search(r"solver\s+PBiCGStab;", t_solver) and re.search(r"preconditioner\s+DILU;", t_solver)
        assert '"(U|k|epsilon)"' in solvers and "|T)" not in solvers
        flow_solver = dict_block(solvers, '"(U|k|epsilon)"')
        assert re.search(r"tolerance\s+1e-07;", flow_solver) and re.search(r"relTol\s+0.1;", flow_solver)
        p_solver = dict_block(solvers, "p")
        assert re.search(r"solver\s+PCG;", p_solver) and re.search(r"preconditioner\s+DIC;", p_solver)
        assert re.search(r"tolerance\s+1e-06;", p_solver) and re.search(r"relTol\s+0.01;", p_solver)


def test_fv_schemes_and_properties_match_paper():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        schemes = read(case, "system/fvSchemes")
        assert re.search(r"default\s+Euler;", dict_block(schemes, "ddtSchemes"))
        assert re.search(r"default\s+Gauss linear;", dict_block(schemes, "gradSchemes"))
        assert re.search(r"div\(phi,T\)\s+Gauss upwind;", dict_block(schemes, "divSchemes"))
        assert re.search(r"default\s+Gauss linear corrected;", dict_block(schemes, "laplacianSchemes"))
        transport = read(case, "constant/transportProperties")
        assert re.search(r"nu\s+\[0 2 -1 0 0 0 0\]\s+1.5e-05;", transport)
        assert re.search(r"DT\s+DT \[0 2 -1 0 0 0 0\]\s+1.5e-05;", transport)
        assert re.search(r"Sct\s+Sct \[0 0 0 0 0 0 0\]\s+0.7;", transport)
        assert re.search(r"RASModel\s+kEpsilon;", read(case, "constant/turbulenceProperties"))
        velocity = read(case, "0/U")
        assert "uniform (0 0 -0.5)" in velocity and "noSlip" in velocity
        scalar = read(case, "0/T")
        assert re.search(r"internalField\s+uniform 1;", scalar)
        assert re.search(r'"inlet\.\*"\s*\{\s*type\s+fixedValue;\s*value\s+uniform 0;', scalar)
        assert "kqRWallFunction" in read(case, "0/k") and "epsilonWallFunction" in read(case, "0/epsilon")


def test_toposet_keeps_only_boundary_faces():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        actions = toposet_actions(read(case, "system/topoSetDict"))
        for set_name in ("inletFaces", "outletFaces"):
            own = [(action, source) for name, action, source in actions if name == set_name]
            assert own == [("new", "boxToFace"), ("add", "boxToFace"), ("subset", "boundaryToFace")], own
        patch_dict = read(case, "system/createPatchDict")
        assert re.search(r"name inlet;.*?set inletFaces;", patch_dict, re.S)
        assert re.search(r"name outlet;.*?set outletFaces;", patch_dict, re.S)


def test_snappy_has_one_refinement_box_per_inlet():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        snappy = read(case, "system/snappyHexMeshDict")
        assert snappy.count("type searchableBox;") == 2
        assert re.search(r"addLayers\s+false;", snappy)
        assert 'file "furniture.stl";' in snappy and '"furniture.eMesh"' in snappy
        regions = dict_block(snappy, "refinementRegions")
        assert re.search(r"mode distance;\s*levels \(\(0.02 4\) \(0.05 3\)\);", dict_block(regions, "furniture"))
        for inlet, (cx, cy) in (("inlet_1", (0.0, 0.0)), ("inlet_2", (1.2, 1.2))):
            box = dict_block(dict_block(snappy, "geometry"), f"diffuserBox_{inlet}")
            lo = [float(v) for v in re.search(r"min \(([^)]*)\);", box).group(1).split()]
            hi = [float(v) for v in re.search(r"max \(([^)]*)\);", box).group(1).split()]
            assert np.allclose(lo, [cx - 0.2, cy - 0.2, SCALED_MIN[2] + 0.5], atol=1e-6), lo
            assert np.allclose(hi, [cx + 0.2, cy + 0.2, SCALED_MAX[2]], atol=1e-6), hi
            assert re.search(r"mode inside;\s*levels \(\(1E15 3\)\);", dict_block(regions, f"diffuserBox_{inlet}"))
        assert "diffuserBox_outlet" not in snappy
        features = read(case, "system/surfaceFeatureExtractDict")
        assert "room.stl" in features and "furniture.stl" in features


def test_empty_room_has_no_furniture_entries():
    def empty(cfg):
        cfg["geometry"]["furniture_stl"] = None

    with workdir() as tmp:
        code, text, case = generate(tmp, empty)
        assert code == 0, text
        assert "none (empty room)" in text
        snappy = read(case, "system/snappyHexMeshDict")
        assert "furniture" not in snappy
        assert snappy.count("type searchableBox;") == 2
        assert "furniture" not in read(case, "system/surfaceFeatureExtractDict")
        assert not (case / "constant/triSurface/furniture.stl").exists()
        for relative in EXPECTED_FILES:
            assert (case / relative).is_file(), f"missing {relative}"
        assert metadata(case)["furniture_present"] is False

    def absent(cfg):
        del cfg["geometry"]["furniture_stl"]

    with workdir() as tmp:
        code, text, case = generate(tmp, absent)
        assert code == 0, text
        assert "furniture" not in read(case, "system/snappyHexMeshDict")


def test_control_dict_scalar_has_probes():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        scalar = read(case, "system/controlDict.scalar")
        assert re.search(r"application\s+scalarTransportFoamTurbulent;", scalar)
        assert re.search(r"startFrom\s+latestTime;", scalar)
        assert re.search(r"endTime\s+2200;", scalar) and re.search(r"deltaT\s+0.05;", scalar)
        # the frozen nut field carries nutkWallFunction patches: the library that
        # registers them must be loaded independently of the solver's link line
        assert re.search(r"(?m)^libs\s+\(turbulenceModels\);", scalar)
        monitors = dict_block(dict_block(scalar, "functions"), "monitors")
        assert re.search(r"type\s+probes;", monitors) and re.search(r"libs\s+\(sampling\);", monitors)
        assert re.search(r"writeControl\s+timeStep;", monitors) and re.search(r"writeInterval\s+1;", monitors)
        assert re.search(r"fields\s+\(T\);", monitors)
        locations = re.search(r"probeLocations\s*\((.*)\);", monitors, re.S).group(1)
        points = [[float(v) for v in m.split()] for m in re.findall(r"\(([^()]*)\)", locations)]
        assert len(points) == 3
        # centre: middle of the scaled footprint, 1.1 m above the floor
        assert np.allclose(points[0], [0.6, 0.6, SCALED_MIN[2] + 1.1], atol=1e-6), points[0]
        # absolute coordinates are used as they are
        assert np.allclose(points[1], [1.0, -1.0, 0.5], atol=1e-9), points[1]
        # fractions of the scaled bounding box
        assert np.allclose(points[2], [-1.8 + 0.25 * 4.8, -2.4 + 0.25 * 6.0, -0.6 + 0.5 * 3.0], atol=1e-6), points[2]
        stored = [m["position_xyz"] for m in metadata(case)["monitors"]]
        assert np.allclose(stored, points, atol=1e-6)
        assert [m["name"] for m in metadata(case)["monitors"]] == ["centre", "absolute", "fraction"]

        flow = read(case, "system/controlDict")
        assert re.search(r"application\s+simpleFoam;", flow)
        assert re.search(r"endTime\s+1000;", flow) and re.search(r"startTime\s+0;", flow)
        assert "probes" not in flow


def test_block_mesh_cells_follow_cell_size():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        block = read(case, "system/blockMeshDict")
        cells = [int(v) for v in re.search(r"hex \(0 1 2 3 4 5 6 7\) \((\d+) (\d+) (\d+)\)", block).groups()]
        extents = [SCALED_MAX[k] - SCALED_MIN[k] + 0.02 for k in range(3)]
        assert cells == [max(1, round(e / 0.25)) for e in extents] == [19, 24, 12], cells
        meta = metadata(case)["background_mesh"]
        assert meta["cells"] == cells and meta["cell_count"] == 19 * 24 * 12
        assert np.allclose(meta["cell_size_m"], [e / n for e, n in zip(extents, cells)])
        assert np.allclose(meta["box_min"], [v - 0.01 for v in SCALED_MIN]) and np.allclose(meta["box_max"], [v + 0.01 for v in SCALED_MAX])

    def explicit(cfg):
        cfg["mesh"]["background_cells"] = [10, 11, 12]

    with workdir() as tmp:
        code, text, case = generate(tmp, explicit)
        assert code == 0, text
        assert "hex (0 1 2 3 4 5 6 7) (10 11 12)" in read(case, "system/blockMeshDict")

    def default_size(cfg):
        del cfg["mesh"]["background_cell_size_m"]

    with workdir() as tmp:
        code, text, case = generate(tmp, default_size)
        assert code == 0, text
        # paper default: 0.08 m
        assert metadata(case)["background_mesh"]["cells"] == [60, 75, 38]


def test_patch_centres_rescale_with_room_height():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        meta = metadata(case)
        assert abs(meta["scale_factor"] - SCALE) < 1e-12
        assert abs(meta["patch_centre_scale"] - 3.0 / 2.5) < 1e-12
        assert np.allclose(meta["scaled_room_bounds"], [SCALED_MIN, SCALED_MAX], atol=1e-6)
        by_name = {p["name"]: p for p in meta["patches"]}
        for name, centre, size in (
            ("inlet_1", (0.0, 0.0), 0.4),
            ("inlet_2", (1.0, 1.0), 0.4),
            ("outlet_1", (-1.2, -1.7), 0.5),
            ("outlet_2", (2.2, 2.7), 0.5),
        ):
            patch = by_name[name]
            assert np.allclose(patch["center_xy"], [centre[0] * 1.2, centre[1] * 1.2]), patch
            assert np.allclose(patch["size_xy"], [size, size]), patch  # sizes stay in metres
            assert abs(patch["nominal_area_m2"] - size * size) < 1e-12
            assert abs(patch["ceiling_overlap_fraction"] - 1.0) < 1e-12
            assert abs(patch["z"] - SCALED_MAX[2]) < 1e-6
        boxes = re.findall(r"box \(([^)]*)\) \(([^)]*)\);", read(case, "system/topoSetDict"))
        lo = [float(v) for v in boxes[1][0].split()]
        hi = [float(v) for v in boxes[1][1].split()]
        assert np.allclose(lo, [1.0, 1.0, 2.35], atol=1e-6) and np.allclose(hi, [1.4, 1.4, 2.45], atol=1e-6), (lo, hi)

    def no_reference(cfg):
        del cfg["ventilation"]["patch_reference_height_m"]

    with workdir() as tmp:
        code, text, case = generate(tmp, no_reference)
        assert code == 0, text
        meta = metadata(case)
        assert meta["patch_centre_scale"] == 1.0
        assert np.allclose({p["name"]: p for p in meta["patches"]}["inlet_2"]["center_xy"], [1.0, 1.0])


def test_selection_box_stays_off_the_walls():
    """A rectangle that reaches a wall must not select faces of that wall."""

    def touching(cfg):
        # after rescaling: centre x = 2.7504, so the rectangle ends 0.4 mm outside the wall at x = 3.0
        cfg["ventilation"]["patches"][3]["center_xy"] = [2.292, 2.7]

    with workdir() as tmp:
        code, text, case = generate(tmp, touching)
        assert code == 0, text
        patch = {p["name"]: p for p in metadata(case)["patches"]}["outlet_2"]
        assert abs(patch["rect_max_xy"][0] - 3.0004) < 1e-9
        assert 0.99 < patch["ceiling_overlap_fraction"] < 1.0
        assert abs(patch["box_max"][0] - (SCALED_MAX[0] - 0.001)) < 1e-9, patch["box_max"]
        assert abs(patch["box_min"][0] - 2.5004) < 1e-9 and abs(patch["box_max"][1] - 3.49) < 1e-9
        boxes = re.findall(r"box \(([^)]*)\) \(([^)]*)\);", read(case, "system/topoSetDict"))
        assert abs(float(boxes[3][1].split()[0]) - 2.999) < 1e-6, boxes[3]


def test_metadata_reports_flow_rate_and_hashes():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        meta = metadata(case)
        summary = meta["ventilation_summary"]
        assert abs(summary["inlet_area_m2"] - 0.32) < 1e-9 and abs(summary["outlet_area_m2"] - 0.5) < 1e-9
        assert abs(summary["supply_flow_rate_m3_s"] - 0.16) < 1e-9
        volume = 4.8 * 6.0 * 3.0
        assert abs(summary["room_bbox_volume_m3"] - volume) < 1e-6
        assert abs(summary["nominal_air_changes_per_hour"] - 0.16 * 3600.0 / volume) < 1e-6
        assert abs(summary["room_stl_volume_m3"] - volume) < 1e-6
        hashes = meta["source_stl_sha256"]
        assert len(hashes) == 2
        for source, digest in hashes.items():
            assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == digest
        location = meta["location_in_mesh"]
        assert all(SCALED_MIN[k] < location[k] < SCALED_MAX[k] for k in range(3))
        assert 0.0 < SCALED_MAX[2] - location[2] < 0.6, "locationInMesh should be just below the ceiling"
        box_min, cell = meta["background_mesh"]["box_min"], meta["background_mesh"]["cell_size_m"]
        for k in range(3):
            fraction = ((location[k] - box_min[k]) / cell[k]) % 1.0
            assert 0.2 < fraction < 0.8, "locationInMesh must not lie on a background-mesh face"
        assert f"locationInMesh ({' '.join(GEN.fmt(v) for v in location)});" in read(case, "system/snappyHexMeshDict")
        scaled_room = trimesh.load_mesh(case / "constant/triSurface/room.stl", force="mesh")
        assert np.allclose(scaled_room.bounds, [SCALED_MIN, SCALED_MAX], atol=1e-5)
        scaling = json.loads(read(case, "geometry_scaling.json"))
        assert abs(scaling["scale_factor"] - SCALE) < 1e-12


# ---------------------------------------------------------------------------
# Failures: clear message, nothing left behind
# ---------------------------------------------------------------------------
def test_patch_outside_ceiling_fails():
    def outside(cfg):
        cfg["ventilation"]["patches"][3]["center_xy"] = [2.6, 2.7]  # sticks out of the +x wall

    with workdir() as tmp:
        code, text, case = generate(tmp, outside)
        assert_fails(code, "outlet_2", "ceiling")
        assert_no_leftovers(case)

    with workdir() as tmp:
        code, text, case = generate(tmp, outside, "--allow-partial-patches")
        assert code == 0, (code, text)
        assert "WARNING" in text and "outlet_2" in text
        overlap = {p["name"]: p for p in metadata(case)["patches"]}["outlet_2"]["ceiling_overlap_fraction"]
        assert 0.0 < overlap < 0.99

    def wrong_plane(cfg):
        cfg["ventilation"]["patches"][0]["z"] = 1.0  # 1.2 m after rescaling, far below the ceiling at 2.4 m

    with workdir() as tmp:
        code, text, case = generate(tmp, wrong_plane)
        assert_fails(code, "inlet_1", "ceiling plane")
        assert_no_leftovers(case)


def test_rotated_room_is_rejected():
    """A room that is not wall-aligned has patches off its ceiling even if they are inside its bounding box."""
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        room = _box(ROOM_EXTENTS, ROOM_CENTRE)
        room.apply_transform(trimesh.transformations.rotation_matrix(np.radians(35.0), [0, 0, 1], ROOM_CENTRE))
        room.export(stl_dir / "room.stl")
        code, text, case = generate(tmp)
        assert_fails(code, "ceiling")
        assert_no_leftovers(case)


def test_failed_run_leaves_no_output_directory():
    def monitor_outside(cfg):
        cfg["monitors"].append({"name": "lost", "position_xyz": [50.0, 0.0, 0.0]})

    with workdir() as tmp:
        code, text, case = generate(tmp, monitor_outside)
        assert_fails(code, "lost", "outside the room bounds")
        assert_no_leftovers(case)
        assert not (tmp / "out").exists(), "the parent of the output directory must not be created by a failed run"

    def monitor_too_high(cfg):
        cfg["monitors"] = [{"name": "high", "position_rel": [0.5, 0.5], "height_above_floor_m": 3.5}]

    with workdir() as tmp:
        code, text, case = generate(tmp, monitor_too_high)
        assert_fails(code, "high")
        assert_no_leftovers(case)


def test_failure_while_writing_cleans_up():
    original = GEN.render_system_and_constants

    def failing(case, plan):
        original(case, plan)
        raise RuntimeError("simulated failure while writing the case")

    with workdir() as tmp:
        GEN.render_system_and_constants = failing
        try:
            try:
                generate(tmp)
            except RuntimeError as exc:
                assert "simulated failure" in str(exc)
            else:
                raise AssertionError("the simulated failure was swallowed")
        finally:
            GEN.render_system_and_constants = original
        assert_no_leftovers(tmp / "out" / "case")


def test_overwrite_and_failed_overwrite():
    with workdir() as tmp:
        code, text, case = generate(tmp)
        assert code == 0, text
        marker = case / "marker.txt"
        marker.write_text("keep me")
        cfg_path = tmp / "case.yaml"

        code, _ = run_generator(cfg_path, case)
        assert_fails(code, "output exists", "--overwrite")
        assert marker.is_file()

        # a failing --overwrite run must not destroy the existing case
        bad = base_config(tmp / "stl")
        bad["ventilation"]["patches"][3]["center_xy"] = [9.0, 9.0]
        bad_path = write_config(tmp / "bad.yaml", bad)
        code, _ = run_generator(bad_path, case, "--overwrite")
        assert_fails(code, "outlet_2")
        assert marker.is_file() and (case / "system/controlDict").is_file()

        code, text = run_generator(cfg_path, case, "--overwrite")
        assert code == 0, text
        assert not marker.exists() and (case / "system/controlDict").is_file()


def test_config_errors_are_reported():
    def missing_room(cfg):
        cfg["geometry"]["room_stl"] = "does_not_exist.stl"

    def missing_furniture(cfg):
        cfg["geometry"]["furniture_stl"] = "does_not_exist.stl"

    def missing_dir(cfg):
        cfg["geometry"]["stl_source_dir"] = cfg["geometry"]["stl_source_dir"] + "_missing"

    def no_patches(cfg):
        del cfg["ventilation"]["patches"]

    def empty_patches(cfg):
        cfg["ventilation"]["patches"] = []

    def bad_shape(cfg):
        cfg["ventilation"]["patches"][0]["shape"] = "circle"

    def no_outlet(cfg):
        cfg["ventilation"]["patches"] = cfg["ventilation"]["patches"][:2]

    def no_height(cfg):
        del cfg["geometry"]["room_height_m"]

    def zero_height(cfg):
        cfg["geometry"]["room_height_m"] = 0

    def both_furniture(cfg):
        cfg["geometry"]["furniture_dir"] = "parts"

    def bad_monitor(cfg):
        cfg["monitors"] = [{"name": "m", "position_rel": [0.5, 0.5]}]

    def scalar_end_too_early(cfg):
        cfg["simulation"]["scalar_end_time"] = 500

    cases = [
        (missing_room, ["missing room STL"]),
        (missing_furniture, ["missing furniture STL"]),
        (missing_dir, ["stl_source_dir not found"]),
        (no_patches, ["ventilation.patches"]),
        (empty_patches, ["ventilation.patches"]),
        (bad_shape, ["unsupported patch shape 'circle'"]),
        (no_outlet, ["role 'outlet'"]),
        (no_height, ["room_height_m is required"]),
        (zero_height, ["room_height_m must be a positive number"]),
        (both_furniture, ["furniture_stl", "furniture_dir"]),
        (bad_monitor, ["height_above_floor_m"]),
        (scalar_end_too_early, ["scalar_end_time"]),
    ]
    for mutate, fragments in cases:
        with workdir() as tmp:
            code, text, case = generate(tmp, mutate)
            assert_fails(code, *fragments)
            assert_no_leftovers(case)


def test_zero_height_room_stl_fails():
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        flat = trimesh.Trimesh(vertices=[[0, 0, 0], [4, 0, 0], [4, 5, 0], [0, 5, 0]], faces=[[0, 1, 2], [0, 2, 3]])
        flat.export(stl_dir / "room.stl")
        code, text, case = generate(tmp)
        assert_fails(code, "zero height")
        assert_no_leftovers(case)


# ---------------------------------------------------------------------------
# Config variants
# ---------------------------------------------------------------------------
def test_old_height_keys_are_accepted():
    def old_keys(cfg):
        geometry = cfg["geometry"]
        geometry["measured_height_m"] = geometry.pop("room_height_m")
        geometry["scale_to_measured_height"] = geometry.pop("scale_to_room_height")

    with workdir() as tmp:
        code, text, case = generate(tmp, old_keys)
        assert code == 0, text
        assert "deprecated" in text
        assert abs(metadata(case)["scale_factor"] - SCALE) < 1e-12

    def no_scaling(cfg):
        cfg["geometry"]["scale_to_room_height"] = False
        del cfg["geometry"]["room_height_m"]
        cfg["monitors"] = [{"name": "centre", "position_rel": [0.5, 0.5], "height_above_floor_m": 1.1}]

    with workdir() as tmp:
        code, text, case = generate(tmp, no_scaling)
        assert code == 0, text
        meta = metadata(case)
        assert meta["scale_factor"] == 1.0 and abs(meta["room_height_m"] - 2.5) < 1e-9
        assert abs(meta["patch_centre_scale"] - 1.0) < 1e-9


def test_furniture_dir_and_list_are_merged():
    def from_dir(cfg):
        del cfg["geometry"]["furniture_stl"]
        cfg["geometry"]["furniture_dir"] = "parts"

    def from_list(cfg):
        cfg["geometry"]["furniture_stl"] = ["parts/piece_a.stl", "parts/piece_b.stl"]

    for mutate in (from_dir, from_list):
        with workdir() as tmp:
            code, text, case = generate(tmp, mutate)
            assert code == 0, text
            furniture = trimesh.load_mesh(case / "constant/triSurface/furniture.stl", force="mesh")
            assert len(furniture.faces) == 24
            assert np.allclose(furniture.bounds, [[-0.24, -0.24, -0.6], [1.8, 2.16, 0.36]], atol=1e-5)
            assert len(metadata(case)["source_stl_sha256"]) == 3


def test_watertight_policy():
    def open_tube() -> trimesh.Trimesh:
        tube = trimesh.creation.cylinder(radius=0.2, height=0.6, sections=24)
        side = np.abs(tube.face_normals[:, 2]) < 0.5
        tube.update_faces(side)
        tube.remove_unreferenced_vertices()
        tube.apply_translation((1.5, -1.0, -0.2))
        return tube

    # open furniture: warning by default, error with --strict-watertight
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        box = trimesh.load_mesh(stl_dir / "furniture.stl", force="mesh")
        trimesh.util.concatenate([box, open_tube()]).export(stl_dir / "furniture.stl")

        code, text, case = generate(tmp)
        assert code == 0, text
        assert "WARNING" in text and "not watertight" in text
        report = json.loads(read(case, "stl_quality_report.json"))
        furniture = [g for g in report["geometry"] if g["label"] == "furniture"][0]
        assert furniture["is_watertight"] is False and furniture["repaired_by_trimesh"] is False
        assert furniture["bodies"]["body_count"] == 3 and furniture["bodies"]["non_watertight_bodies"] == 1
        assert report["require_watertight_room"] is True and report["require_watertight_furniture"] is False

        code, text, strict_case = generate(tmp, None, "--strict-watertight", name="strict")
        assert_fails(code, "furniture STL is not watertight")
        assert_no_leftovers(strict_case)

    # open room: always an error, except with the deprecated --allow-non-watertight
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        room = _box(ROOM_EXTENTS, ROOM_CENTRE)
        keep = np.ones(len(room.faces), dtype=bool)
        keep[np.abs(room.face_normals[:, 0]) > 0.5] = False  # remove both x walls
        room.update_faces(keep)
        room.export(stl_dir / "room.stl")

        code, text, case = generate(tmp, None, "--no-repair-stl")
        assert_fails(code, "room STL is not watertight")
        assert_no_leftovers(case)

        code, text, case = generate(tmp, None, "--no-repair-stl", "--allow-non-watertight")
        assert code == 0, (code, text)
        assert "deprecated" in text

        code, text, case = generate(tmp, None, "--strict-watertight", "--allow-non-watertight", name="both")
        assert_fails(code, "mutually exclusive")


def test_tiered_floor_monitor_height_is_local():
    """Height above the floor is measured from the floor below the monitor, not from the lowest point."""
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        # floor rises from z = 0 at y = 0 to z = 1 at y = 5; ceiling at z = 3
        corners = [[0, 0, 0], [4, 0, 0], [0, 5, 1], [4, 5, 1], [0, 0, 3], [4, 0, 3], [0, 5, 3], [4, 5, 3]]
        trimesh.Trimesh(vertices=corners).convex_hull.export(stl_dir / "room.stl")

        def sloped(cfg):
            cfg["geometry"]["furniture_stl"] = None
            cfg["ventilation"].pop("patch_reference_height_m")
            cfg["ventilation"]["patches"] = [
                {"name": "inlet_1", "role": "inlet", "shape": "rectangle", "center_xy": [2.0, 2.5], "size_xy": [0.4, 0.4]},
                {"name": "outlet_1", "role": "outlet", "shape": "rectangle", "center_xy": [0.5, 0.5], "size_xy": [0.5, 0.5]},
            ]
            cfg["monitors"] = [
                {"name": "front", "position_rel": [0.5, 0.2], "height_above_floor_m": 1.1},
                {"name": "back", "position_rel": [0.5, 0.8], "height_above_floor_m": 1.1},
            ]

        code, text, case = generate(tmp, sloped)
        assert code == 0, text
        monitors = {m["name"]: m for m in metadata(case)["monitors"]}
        assert np.allclose(monitors["front"]["position_xyz"], [2.0, 1.0, 0.2 + 1.1], atol=1e-6)
        assert np.allclose(monitors["back"]["position_xyz"], [2.0, 4.0, 0.8 + 1.1], atol=1e-6)
        assert abs(monitors["back"]["local_floor_z_m"] - 0.8) < 1e-6

        def below_floor(cfg):
            sloped(cfg)
            cfg["monitors"] = [{"name": "buried", "position_rel": [0.5, 0.9, 0.1]}]  # z = 0.3, floor there is at 0.9

        code, text, case = generate(tmp, below_floor, name="buried")
        assert_fails(code, "buried", "outside the room")
        assert_no_leftovers(case)


def test_monitor_inside_furniture_warns():
    def inside(cfg):
        # centre of the first furniture piece (0, 0, -0.1) scaled by 1.2
        cfg["monitors"] = [{"name": "blocked", "position_xyz": [0.0, 0.0, -0.12]}]

    with workdir() as tmp:
        code, text, case = generate(tmp, inside)
        assert code == 0, text
        assert "blocked" in text and "inside a furniture" in text
        assert metadata(case)["monitors"][0]["inside_furniture_estimate"] is True


# ---------------------------------------------------------------------------
# Validator, command line, shipped configs
# ---------------------------------------------------------------------------
def test_validate_inputs_script():
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        good = write_config(tmp / "good.yaml", base_config(stl_dir))
        bad_cfg = base_config(stl_dir)
        bad_cfg["ventilation"]["patches"][3]["center_xy"] = [9.0, 9.0]
        bad = write_config(tmp / "bad.yaml", bad_cfg)
        broken_cfg = base_config(stl_dir)
        del broken_cfg["geometry"]["room_height_m"]
        broken = write_config(tmp / "broken.yaml", broken_cfg)

        def run(*argv):
            buffer, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
                code = VALIDATE.main(list(argv))
            return code, buffer.getvalue() + errors.getvalue()

        code, text = run("--config", str(good))
        assert code == 0 and "validation passed" in text and "scene.json" not in text, text
        code, text = run("--config", str(bad))
        assert code == 1 and "outlet_2" in text, text
        code, text = run("--config", str(bad), "--config-only")
        assert code == 0, text
        code, text = run("--config", str(broken), "--config-only")
        assert code == 1 and "room_height_m" in text, text
        code, text = run("--config", str(tmp / "missing.yaml"))
        assert code == 2, text
        assert not list(tmp.glob("out*")), "the validator must not write a case"


def test_command_line_interface():
    with workdir() as tmp:
        stl_dir = tmp / "stl"
        make_geometry(stl_dir)
        cfg_path = write_config(tmp / "cli.yaml", base_config(stl_dir))
        out = tmp / "cli_case"
        done = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--config", str(cfg_path), "--output", str(out)],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Prepared OpenFOAM case" in done.stdout and (out / "system/fvSolution").is_file()

        bad = base_config(stl_dir)
        bad["geometry"]["room_stl"] = "nope.stl"
        bad_path = write_config(tmp / "cli_bad.yaml", bad)
        failed = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--config", str(bad_path), "--output", str(tmp / "cli_bad")],
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0 and "ERROR: missing room STL" in failed.stderr
        assert not (tmp / "cli_bad").exists()


def test_shipped_configs_match_paper_values():
    configs = sorted((CFD_DIR / "configs").glob("*.yaml"))
    assert len(configs) == 6, [c.name for c in configs]
    for path in configs:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        settings, errors, warnings = GEN.normalise_config(copy.deepcopy(cfg))
        assert not errors, (path.name, errors)
        assert not warnings, (path.name, warnings)
        auditorium = cfg["case_name"] == "auditorium"
        geometry, ventilation = settings["geometry"], settings["ventilation"]
        assert cfg["case_name"] == path.stem and cfg.get("paper_case")
        assert geometry["stl_source_dir"].startswith("data/projects/") and geometry["stl_source_dir"].endswith("/4_stl/axis_aligned")
        assert geometry["room_height_m"] == (12.87 if auditorium else 2.90)
        assert ventilation["patch_reference_height_m"] == (12.85 if auditorium else 3.00)
        assert ventilation["inlet_velocity"] == [0.0, 0.0, -0.5]
        roles = [p["role"] for p in ventilation["patches"]]
        assert roles.count("inlet") == (6 if auditorium else 4) and roles.count("outlet") == 2
        assert [m["name"] for m in settings["monitors"]] == (["front", "middle", "back"] if auditorium else ["occupied_zone"])
        mesh, simulation = settings["mesh"], settings["simulation"]
        assert mesh["background_cell_size_m"] == 0.08 and mesh["background_cells"] is None
        assert mesh["max_global_cells"] == (40000000 if auditorium else 20000000)
        assert mesh["furniture_distance_refinement"] == [[0.02, 4], [0.05, 3]]
        assert mesh["diffuser_refinement"] == {"enabled": True, "level": 3, "bottom_height_above_floor_m": 0.5, "margin_m": 0.0}
        assert simulation["airflow_end_iter"] == 1000 and simulation["scalar_delta_t"] == 0.05
        assert simulation["scalar_end_time"] == (5000 if auditorium else 2200)
        assert simulation["default_tasks"] == 56 and simulation["decomposition_method"] == "scotch"
        assert settings["transport"]["molecular_diffusivity_m2_s"] == 1.5e-5
        assert settings["transport"]["turbulent_schmidt_number"] == 0.7
        assert settings["turbulence"]["k"] == 0.015 and settings["turbulence"]["epsilon"] == 0.004
        if cfg["case_name"] == "class_a_v1_empty":
            assert geometry["furniture_files"] == [] and geometry["furniture_dir"] is None
        else:
            assert geometry["furniture_files"], path.name


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
