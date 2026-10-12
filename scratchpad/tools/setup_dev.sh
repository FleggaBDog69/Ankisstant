#!/usr/bin/env bash
# One-time: a private Python env with the lint tools the checks use.
#   scratchpad/tools/setup_dev.sh
# Lives in scratchpad/.venv — gitignored and left out of the .ankiaddon.
set -euo pipefail
# CDPATH unset: with it set, cd prints the folder and that ends up in $here.
here="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$here/.venv"
"$here/.venv/bin/pip" install -q --upgrade pip
"$here/.venv/bin/pip" install -q pyflakes vulture autoflake
echo "dev tools ready in $here/.venv"
