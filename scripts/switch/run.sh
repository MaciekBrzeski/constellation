#!/usr/bin/env bash
# Launch the constellation audio brain in the foreground. Extra args pass
# through to constellation-master. Defaults: config/default.toml, web
# dashboard on :8000.
#
#   ./run.sh                          # brain + web dashboard
#   WITH_GUI=0 ./run.sh               # brain only, no web UI
#   ./run.sh --host 192.168.50.255    # explicit broadcast address
#
# For the native Tkinter panel instead of the web dashboard, run
# `constellation-control` directly (needs a display + python3-tk).
set -euo pipefail

cd "$(dirname "$0")/../.."

GUI_FLAGS="--gui --web-host 0.0.0.0 --web-port 8000"
if [ "${WITH_GUI:-1}" = "0" ]; then
    GUI_FLAGS=""
fi

exec python3 -m constellation.cli.master \
    --config config/default.toml \
    $GUI_FLAGS \
    "$@"
