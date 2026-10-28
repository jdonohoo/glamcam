#!/bin/bash
# Builds dist/GlamCam.app (macOS). Drag it to Applications afterwards.
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./setup.sh
.venv/bin/python -m pip install -q pyinstaller
.venv/bin/python glamcam.py --download-models
rm -rf build dist
.venv/bin/python -m PyInstaller --noconfirm --log-level WARN glamcam.spec
if [ "$(uname)" = "Darwin" ]; then
  dist/GlamCam.app/Contents/MacOS/GlamCam --self-test
  echo
  echo "Built dist/GlamCam.app  ->  drag it into /Applications and double-click."
else
  dist/GlamCam/GlamCam --self-test
  echo "Built dist/GlamCam/"
fi
