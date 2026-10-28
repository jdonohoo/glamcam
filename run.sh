#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up GlamCam (one time only)..."
  ./setup.sh || exit 1
fi
exec .venv/bin/python glamcam.py "$@"
