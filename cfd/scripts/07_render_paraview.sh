#!/usr/bin/env bash
# Optional: render a quick-look image of the scalar field of a finished case
# with ParaView (pvpython or pvbatch). For a parallel run, the case must have
# been reconstructed (Allrun does this with reconstructPar -latestTime).
#
# Usage:
#   bash cfd/scripts/07_render_paraview.sh <case_dir> [output.png]
#
# Environment variables:
#   PVEXEC   pvpython or pvbatch executable (default: found with
#            cfd/scripts/07_find_paraview.py)
#   PYTHON   Python used to run 07_find_paraview.py (default python3)
set -euo pipefail

CASE_DIR="${1:?Usage: bash cfd/scripts/07_render_paraview.sh <case_dir> [output.png]}"
OUTPUT="${2:-paraview_scalar_smoke.png}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PVEXEC="${PVEXEC:-}"
if [ -z "$PVEXEC" ]; then
    PVEXEC="$("${PYTHON:-python3}" "$REPO_ROOT/cfd/scripts/07_find_paraview.py" --prefer pvpython --batch-only)"
fi

cd "$CASE_DIR"

if [ ! -f case.foam ]; then
    touch case.foam
fi

echo "Using ParaView executable: $PVEXEC"
"$PVEXEC" "$REPO_ROOT/cfd/paraview/render_openfoam_scalar.py" case.foam "$OUTPUT"
