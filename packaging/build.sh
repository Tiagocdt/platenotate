#!/usr/bin/env bash
# build.sh — build the Notate desktop app for THIS OS. Run inside a Python venv (or CI).
# Produces dist/Notate.app (macOS) or dist/Notate/ (Windows/Linux) — a windowed app, no terminal.
set -euo pipefail
cd "$(dirname "$0")/.."                       # → label_annotator/
python packaging/gather_deps.py              # refresh vendored sibling modules (no-op on CI)
python -m pip install --upgrade pip
python -m pip install -r requirements-desktop.txt pyinstaller
python -m PyInstaller --noconfirm --clean packaging/notate.spec
echo "built → dist/"; ls -la dist/
