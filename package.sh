#!/usr/bin/env bash
# Build an installable .ankiaddon zip of this directory.
#
# Run from inside the addon folder:
#     ./package.sh                  -> ankisstant-<version>.ankiaddon next to it
#     ./package.sh /tmp/out.zip     -> custom output path
#
# Excludes runtime / user-specific files so the bundle is shareable.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

version="$(python3 -c 'import json,sys; print(json.load(open("manifest.json"))["version"])')"
out="${1:-../ankisstant-${version}.ankiaddon}"

# Resolve to absolute path so the cd below doesn't break it.
case "$out" in
  /*) ;;
  *) out="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")" ;;
esac

rm -f "$out"

# `zip` flag rundown:
#   -r           recurse
#   -X           drop extra file attributes (smaller, deterministic)
#   -q           quiet
#   --exclude    repeated for every path glob to skip
zip -rXq "$out" . \
  --exclude '*/__pycache__/*' \
  --exclude '__pycache__/*' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude '*/.DS_Store' \
  --exclude 'meta.json' \
  --exclude 'user_files/*' \
  --exclude 'user_files' \
  --exclude '.git/*' \
  --exclude '.gitignore' \
  --exclude 'package.sh' \
  --exclude '*.ankiaddon'

echo "wrote $out"
