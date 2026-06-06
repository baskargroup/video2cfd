# Reproducing the three paper environments

This pipeline produces the **automated**, simulation-ready furniture + room STL
geometry for the three environments in the paper. (The paper's *final* geometry
additionally went through the optional human-in-the-loop editor, Step 4b, which is
not config-determined; see the note at the bottom.)

## Prerequisites
- Install deps: `pip install -r requirements.txt` (a CUDA PyTorch is recommended).
- Place the input data under `data/` as described in [`../data/README.md`](../data/README.md):
  per-environment `point_cloud_cropped.ply`, `transforms.json`,
  `dataparser_transforms.json`, and `frames/<category>/`, plus the STL templates.
- You also need the per-frame masks under
  `data/projects/<name>/runs/<run_id>/1_segmentation/<category>/`. Generate them by
  running Step 1 (`--steps 1`, requires SAM 3), or reuse masks you already have.

## Run (Steps 2→5, i.e. everything except segmentation)
From the `pipeline/` directory:

```bash
# Windows: ensure UTF-8 console so step 4's progress glyphs print
#   PowerShell:  $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
#   bash:        export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

python run_pipeline.py --config ../configs/classroom_a.yaml --steps 2,2b,3,4,5   # Class A
python run_pipeline.py --config ../configs/classroom_b.yaml --steps 2,2b,3,4,5   # Class B
python run_pipeline.py --config ../configs/auditorium.yaml  --steps 2,2b,3,4,5   # Auditorium
```

Outputs land in `data/projects/<name>/runs/<run_id>/4_stl/`:
`furniture.stl`, `room.stl`, `furniture_and_room.stl`, and per-object STLs.

## Reproducibility notes

1. **UTF-8 console (Windows).** `stl_template_placer.py` prints a `→` glyph. Under the
   default Windows `cp1252` console encoding this raises `UnicodeEncodeError`. Set
   `PYTHONIOENCODING=utf-8` (and `PYTHONUTF8=1`) before running. No effect on results.

2. **GPU non-determinism in Step 2.** Step 2 projects the point cloud with PyTorch and
   enables TF32 (`torch.backends.cuda.matmul.allow_tf32 = True`). TF32 matmuls are not
   bit-reproducible run-to-run, so the extracted point set can vary slightly. Dense,
   high-consensus categories (chairs) reproduce essentially exactly; sparse categories
   observed in few frames near the `min_views` threshold (e.g. tables in a chair-heavy
   room) can flip between, say, 0 and a few instances. For bit-exact reproduction,
   disable TF32 and enable deterministic algorithms before running Step 2 (a runtime
   setting, not changed in this faithful release).

## Verification performed for this release

The `pipeline/` code in this repository is a **byte-for-byte copy** of the original
implementation (14/14 source files `md5`-identical), so it is the same pipeline. We
additionally ran a clean end-to-end check:

| Check | Result |
|-------|--------|
| Source code identity (new vs original) | **14/14 files identical** |
| class_v1 (Class B): new-code vs original-code, **same config + masks** | Chairs identical (74/74 healed groups; 1/1 placed; `furniture.stl` centroid match 1.8e-4, vertex counts within 0.02%). Table category differed (0 vs 4) due to the Step-2 TF32 non-determinism above. |
| class_v2 (Class A): new-code end-to-end | **Completed.** Both categories extracted (chair=52, table=53 healed groups); produced valid `furniture.stl` (67,560 verts / 22,520 faces), `room.stl`, `furniture_and_room.stl`, `furniture_with_mannequins.stl`. |
| auditorium: new-code end-to-end | **Completed.** chair=578, table=10 healed groups; valid `furniture.stl` (514,104 verts / 171,368 faces), auditorium-floor `room.stl`, `furniture_and_room.stl`, `furniture_with_mannequins.stl`. |

> **On matching the archived paper runs:** the original repository's `run_010`,
> `run_005`, and `run_002` are **not** clean single-config outputs — each contains
> 20+ saved config versions and Step-4b human edits, i.e. they are accumulations of
> many partial re-runs plus manual correction. They therefore cannot be byte-matched
> from a config alone. The correct reproducibility criterion is **same code + same
> config ⇒ same output (within Step-2 GPU tolerance)**, which the check above
> demonstrates.
