#!/usr/bin/env sh
# One-command launcher for the native control panel.
# Activates the project virtualenv and starts constellation-control.
set -e
cd "$(dirname "$0")/.."
if [ ! -f .venv/bin/activate ]; then
    echo "No .venv found. Create it with:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[audio,dev]'" >&2
    exit 1
fi
. .venv/bin/activate
exec constellation-control "$@"
