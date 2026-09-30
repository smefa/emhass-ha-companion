#!/usr/bin/env bash
# Run the test suite from WSL's own filesystem.
#
# The repo on /mnt/c is reached over WSL's slow 9p bridge, which turns a ~1 min
# run into ~10 min (Home Assistant imports thousands of small files). This syncs
# the tree to ~/emhass-test and runs there, with a venv that also lives there.
#
#   scripts/wsl_test.sh [pytest args...]      # default: the whole suite
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST="$HOME/emhass-test"
VENV="$HOME/.venvs/emhass-test"

mkdir -p "$DST"
rsync -a --delete \
  --exclude '.git/' --exclude '.venv*' --exclude 'node_modules/' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '.agents/' --exclude 'site/' \
  "$SRC/" "$DST/"

if [ ! -x "$VENV/bin/python" ]; then
  uv venv "$VENV"
  uv pip install --python "$VENV/bin/python" -r "$DST/requirements-dev.txt" pytest-xdist
fi

cd "$DST"
if [ $# -eq 0 ]; then
  set -- tests
fi
"$VENV/bin/python" -m pytest -q -p no:cacheprovider -n auto "$@"
"$VENV/bin/ruff" check --no-cache custom_components tests
"$VENV/bin/ruff" format --no-cache --check custom_components tests
