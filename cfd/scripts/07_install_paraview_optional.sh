#!/usr/bin/env bash
# Optional helper: download and unpack a ParaView Linux binary archive for the
# optional rendering step (cfd/scripts/07_render_paraview.sh). Nothing in the
# CFD workflow needs it.
#
# The script has no default download location. It refuses to run unless the
# URL of the archive is given explicitly, as the first argument or in the
# environment variable PARAVIEW_URL, and it prints what it will download and
# where before it starts. The archive is not verified: download only from
# https://www.paraview.org/download/ and compare the checksum published there
# yourself.
set -euo pipefail

URL="${1:-${PARAVIEW_URL:-}}"
DEST="${2:-$HOME/Downloads}"

if [ -z "$URL" ]; then
  cat >&2 <<'MSG'
Refusing to run: no download URL given.

Usage:
  bash cfd/scripts/07_install_paraview_optional.sh <ParaView-Linux-tar.gz-url> [destination_dir]
or
  PARAVIEW_URL=<ParaView-Linux-tar.gz-url> bash cfd/scripts/07_install_paraview_optional.sh

Download the official Linux binary from:
  https://www.paraview.org/download/

Then pass the copied download URL to this script.

Example:
  bash cfd/scripts/07_install_paraview_optional.sh 'https://.../ParaView-...-Linux-...tar.gz' "$HOME/Downloads"
MSG
  exit 2
fi

mkdir -p "$DEST"
archive="$DEST/$(basename "$URL")"

echo "This will download a ParaView archive"
echo "  from: $URL"
echo "  to:   $archive"
echo "and extract it under"
echo "  $DEST"

if command -v curl >/dev/null 2>&1; then
  curl -L "$URL" -o "$archive"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$archive" "$URL"
else
  echo "ERROR: need curl or wget" >&2
  exit 1
fi

tar -xzf "$archive" -C "$DEST"

echo "Extracted under $DEST"
echo "Run:"
echo "  python3 cfd/scripts/07_find_paraview.py --print-all"
