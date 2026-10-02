#!/usr/bin/env bash
set -euo pipefail

URL="${1:-}"
DEST="${2:-$HOME/Downloads}"

if [ -z "$URL" ]; then
  cat >&2 <<'MSG'
Usage:
  bash cfd/scripts/07_install_paraview_optional.sh <ParaView-Linux-tar.gz-url> [destination_dir]

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

echo "Downloading ParaView:"
echo "  $URL"
echo "to:"
echo "  $archive"

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
