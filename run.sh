#!/usr/bin/env bash
# run.sh — (re)start PlateNotate. Self-updates first, then stops any annotator already
# running and launches ONE fresh server on a fixed port (:8765) and opens the browser.
#
#   ./run.sh                                                 # local data
#   ./run.sh /Volumes/aulehla/Tiago/AQ-EMBL/PROCESSED        # a server (SMB) folder
#   NO_UPDATE=1 ./run.sh                                     # skip the update check
#
# (You can also switch folders live from inside the app with the 📂 Open button.)
cd "$(dirname "$0")" || exit 1

# --- self-update -------------------------------------------------------------
# A fast-forward only: if this checkout has local commits or uncommitted edits it is
# left completely alone and says so. Never blocks the launch — no network, no remote,
# no problem, the app still starts on whatever is on disk.
if [ -z "${NO_UPDATE:-}" ] && [ -d .git ] && command -v git >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "PlateNotate: local changes present — skipping the update check"
  elif git remote get-url origin >/dev/null 2>&1; then
    git fetch --quiet 2>/dev/null
    BEHIND=$(git rev-list --count HEAD..@{upstream} 2>/dev/null || echo 0)
    if [ "${BEHIND:-0}" -gt 0 ]; then
      echo "PlateNotate: $BEHIND new commit(s) — updating…"
      git merge --ff-only @{upstream} --quiet 2>/dev/null \
        && echo "PlateNotate: now at v$(cat VERSION 2>/dev/null)" \
        || echo "PlateNotate: could not fast-forward — running the current version"
    fi
  fi
fi

# stop any annotator server already running (this is why you kept getting new ports)
pkill -f "label_annotator/server.py" 2>/dev/null && { echo "stopped the previous server"; sleep 1; }

DATA="${1:-/Users/tiago/metameda/imaging/data/AQ-EMBL}"
PORT="${PORT:-8765}"
PY="${PY:-/Users/tiago/miniforge3/envs/twinnet/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"                    # portable fallback

echo "PlateNotate v$(cat VERSION 2>/dev/null)  →  http://127.0.0.1:${PORT}/     (data: ${DATA})"
echo "  Ctrl-C to stop."
( sleep 2; open "http://127.0.0.1:${PORT}/" 2>/dev/null ) &   # open the browser once it's up
exec "$PY" server.py --data-root "$DATA" --port "$PORT"       # run in foreground; Ctrl-C stops it
