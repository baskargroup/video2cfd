#!/usr/bin/env pvpython
"""Quick-look rendering of the scalar field T of an OpenFOAM case with ParaView.

Run with pvpython or pvbatch from the case directory (or use
cfd/scripts/07_render_paraview.sh):

    pvpython cfd/paraview/render_openfoam_scalar.py [case.foam] [output.png]

It loads the reconstructed case, goes to the last time step and saves one
image of the outer surface of the mesh coloured by T. This is a smoke-test
image that shows the case can be opened and post-processed; it is not the
script that produced the figures of the paper.
"""
import sys
from pathlib import Path

from paraview.simple import *  # noqa: F401,F403

args = [a for a in sys.argv[1:] if not a.startswith("-")]
case_path = Path(args[0] if len(args) > 0 else "case.foam").resolve()
out_path = Path(args[1] if len(args) > 1 else "paraview_scalar_smoke.png").resolve()

if not case_path.exists():
    # The .foam file is only a marker that tells ParaView where the case is.
    case_path.touch()

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

# Show the last time step (the end of the scalar run), not the first one.
last_time = None
try:
    reader.UpdatePipelineInformation()
    times = list(reader.TimestepValues)
    if times:
        last_time = times[-1]
        view.ViewTime = last_time
        reader.UpdatePipeline(last_time)
except Exception:
    pass

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

if last_time is not None:
    print(f"Time step shown: {last_time}")
print(f"Wrote {out_path}")
