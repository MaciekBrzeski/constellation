#!/usr/bin/env bash
# Install the constellation "brain" runtime on a Switch OLED running Linux
# (switchroot L4T Ubuntu, arm64) — or any Debian/Ubuntu host acting as the
# audio brain.
#
# The brain captures audio, detects beats and broadcasts them over UDP. It
# has no display, so it needs neither OpenCV/Pillow (preprocessing only) nor
# pygame (the worker Pis do the rendering) — only numpy + sounddevice. The
# constellation package is therefore installed with --no-deps.
#
# Run from anywhere inside the repo, as a normal user (it calls sudo itself):
#   bash scripts/switch/install.sh
#
# Env toggles:
#   WITH_GUI=0    skip fastapi/uvicorn (no --gui web dashboard)
#   WITH_PANEL=0  skip python3-tk (no constellation-control native panel)
#
# Needs internet for this one-time step (apt + pip). Host the Wi-Fi AP
# afterwards with scripts/switch/setup_ap.sh.
set -euo pipefail

log() { echo "[switch-install] $*"; }

REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
log "repo root: $REPO"

[[ -f pyproject.toml ]] || { echo "[switch-install] error: pyproject.toml not found — run from inside the repo" >&2; exit 1; }

log "apt update"
sudo apt-get update

# Required: numpy (multiband detector) + sounddevice/PortAudio (mic capture)
# + the pip build toolchain for the editable install.
log "installing numpy, sounddevice, libportaudio2, pip toolchain"
sudo apt-get install -y \
    python3-numpy python3-sounddevice libportaudio2 \
    python3-pip python3-setuptools python3-wheel

# Optional: the browser control dashboard (constellation-master --gui).
if [ "${WITH_GUI:-1}" = "1" ]; then
    log "installing fastapi + uvicorn (web dashboard)"
    sudo apt-get install -y python3-fastapi python3-uvicorn \
        || log "warn: fastapi/uvicorn not in apt — '--gui' will be unavailable"
fi

# Optional: the native Tkinter control panel (constellation-control).
if [ "${WITH_PANEL:-1}" = "1" ]; then
    log "installing python3-tk (constellation-control panel)"
    sudo apt-get install -y python3-tk \
        || log "warn: python3-tk unavailable — constellation-control won't open"
fi

# Optional: aubio. The multiband backend is the default and needs nothing,
# so this is best-effort only (handy for A/B comparison in calibration).
sudo apt-get install -y python3-aubio 2>/dev/null \
    || log "note: python3-aubio not installed — multiband backend is the default"

# Install the constellation package itself: console scripts + importable,
# --no-deps so the heavy preprocess/render deps (opencv, pillow, pygame) the
# brain never uses are NOT pulled in.
log "installing the constellation package (constellation-master / -control)"
sudo pip3 install -e . --no-deps --break-system-packages

# tomllib is stdlib on Python 3.11+; install the tomli fallback otherwise.
if ! python3 -c "import tomllib" 2>/dev/null; then
    log "Python <3.11 — installing the tomli fallback"
    sudo pip3 install --break-system-packages tomli
fi

# The capture stream needs the user in the audio group.
USER_NAME=${SUDO_USER:-$USER}
if ! id -nG "$USER_NAME" 2>/dev/null | grep -qw audio; then
    log "adding $USER_NAME to the 'audio' group (re-login for it to take effect)"
    sudo usermod -aG audio "$USER_NAME"
fi

# Install the autostart unit, baking in this user + repo path.
log "installing constellation-master.service"
UNIT=/etc/systemd/system/constellation-master.service
sudo cp "$REPO/scripts/switch/systemd/constellation-master.service" "$UNIT"
sudo sed -i "s|@USER@|$USER_NAME|g; s|@WORKDIR@|$REPO|g" "$UNIT"
sudo systemctl daemon-reload

log "done. next steps:"
log "  1. host the Wi-Fi AP:"
log "       sudo bash scripts/switch/setup_ap.sh --ssid constellation --psk 'a-good-password'"
log "  2. test the brain in the foreground (Ctrl+C to stop):"
log "       ./scripts/switch/run.sh"
log "  3. autostart it on boot:"
log "       sudo systemctl enable --now constellation-master.service"
log "       journalctl -u constellation-master.service -f"
