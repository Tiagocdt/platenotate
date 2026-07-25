#!/usr/bin/env bash
# run_app.command — double-click in Finder to launch the Medaka Annotator as a
# NATIVE DESKTOP WINDOW (pywebview), not a browser tab. Optional: pass a data folder.
#
#   double-click            → default local data root
#   ./run_app.command PATH  → open a specific folder (e.g. an SMB mount)
cd "$(dirname "$0")" || exit 1
PY="${PY:-/Users/tiago/miniforge3/envs/twinnet/bin/python}"
exec "$PY" desktop.py "$@"
