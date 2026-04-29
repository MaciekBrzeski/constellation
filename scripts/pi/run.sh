#!/usr/bin/env bash
# Launch the constellation display. Extra args pass through to the CLI.
# Defaults: fullscreen, deer_male, audio enabled.
# Override with e.g.:   ./run.sh --animal deer_female
#                       ./run.sh --mode windowed
#                       WITH_AUDIO=0 ./run.sh     (skip --audio)
set -euo pipefail

cd "$(dirname "$0")"

AUDIO_FLAG="--audio"
if [ "${WITH_AUDIO:-1}" = "0" ]; then
    AUDIO_FLAG=""
fi

exec python3 -m constellation \
    --config config/default.toml \
    --animal deer_male \
    --mode fullscreen \
    $AUDIO_FLAG \
    "$@"
