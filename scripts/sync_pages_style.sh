#!/usr/bin/env bash
# Copy workbench CSS truth (web/static/style.css) to GitHub Pages assets/.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/web/static/style.css"
dst="$root/assets/style.css"
if [[ ! -f "$src" ]]; then
  echo "missing $src" >&2
  exit 1
fi
cp "$src" "$dst"
echo "synced $src -> $dst"
