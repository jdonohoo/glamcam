#!/bin/bash
# GlamCam one-time setup for macOS (and Linux): makes .venv, installs deps, fetches models.
set -euo pipefail
cd "$(dirname "$0")"

# Needs Python 3.10-3.13; prefer 3.12. Check Homebrew paths too, since pyenv shims
# often hide them behind macOS's ancient system Python.
PY=""
for c in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 \
         python3.11 /opt/homebrew/bin/python3.11 python3.13 /opt/homebrew/bin/python3.13 \
         python3.10 python3; do
  if "$c" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))' 2>/dev/null; then
    PY=$(command -v "$c"); break
  fi
done
if [ -z "$PY" ]; then
  echo "GlamCam needs Python 3.10-3.13 (found: $(python3 --version 2>&1))."
  echo "Install one with:  brew install python@3.12"
  echo "then run ./setup.sh again."
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

rm -rf .venv
"$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python glamcam.py --download-models
echo
echo "Setup done. Get glam with:  ./run.sh"
