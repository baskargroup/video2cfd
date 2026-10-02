#!/usr/bin/env pvpython
from pathlib import Path
from paraview.simple import *

case_path = Path("case.foam").resolve()
out_path = Path("paraview_scalar_smoke.png").resolve()

reader = OpenFOAMReader(FileName=str(case_path))

# ParaView versions differ slightly in property names and available arrays.
try:
    reader.MeshRegions = ["internalMesh"]
except Exception:
    pass

for prop_name in ("CellArrays", "PointArrays"):
    try:
        setattr(reader, prop_name, ["T", "U", "p", "k", "epsilon", "nut"])
    except Exception:
        pass

view = CreateView("RenderView")
view.ViewSize = [1600, 1000]

display = Show(reader, view)

try:
    ColorBy(display, ("CELLS", "T"))
    display.RescaleTransferFunctionToDataRange(True, False)
except Exception:
    try:
        ColorBy(display, ("POINTS", "T"))
        display.RescaleTransferFunctionToDataRange(True, False)
    except Exception:
        pass

view.ResetCamera()
Render(view)
SaveScreenshot(str(out_path), view)

print(f"Wrote {out_path}")
