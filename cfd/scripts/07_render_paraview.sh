#!/usr/bin/env bash
set -euo pipefail

CASE_DIR="${1:?Usage: bash 07_render_paraview.sh <case_dir>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PVEXEC="${PVEXEC:-}"
if [ -z "$PVEXEC" ]; then
  PVEXEC="$(python3 "$REPO_ROOT/cfd/scripts/07_find_paraview.py" --prefer pvpython)"
fi

cd "$CASE_DIR"

if [ ! -f case.foam ]; then
  touch case.foam
fi

echo "Using ParaView executable: $PVEXEC"
"$PVEXEC" "$REPO_ROOT/cfd/paraview/render_openfoam_scalar.py"
