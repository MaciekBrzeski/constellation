#!/usr/bin/env bash
# First-boot bootstrap: install runtime apt packages, enable the kiosk
# service, and reboot. Runs once via constellation-bootstrap.service, then
# stamps a sentinel file so the unit short-circuits on subsequent boots.
set -euo pipefail

LOG=/var/log/constellation-bootstrap.log
SENTINEL=/var/lib/constellation/bootstrap-done
exec > >(tee -a "$LOG") 2>&1

echo "=== constellation-bootstrap $(date -Is) ==="

# Wait for the network to be reachable. network-online.target is already a
# dependency, but DNS sometimes takes a few extra seconds on Wi-Fi.
for i in $(seq 1 30); do
    if getent hosts deb.debian.org >/dev/null 2>&1; then
        echo "[boot] DNS resolved on attempt $i"
        break
    fi
    sleep 2
done

OFFLINE_DEBS=/home/pi/constellation/offline/debs
OFFLINE_WHEELS=/home/pi/constellation/offline/wheels

echo "[boot] install libportaudio2 from staged .deb (offline)"
if dpkg -i "$OFFLINE_DEBS"/libportaudio2_*.deb 2>&1; then
    echo "[boot] libportaudio2 installed offline"
else
    echo "[boot] offline .deb failed, falling back to apt"
    apt-get update -y || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y libportaudio2 || true
fi

# pygame, numpy, pip already on Pi OS Trixie. Verify.
echo "[boot] verify python deps already present"
python3 -c "import pygame, numpy" || {
    echo "[boot] missing pygame/numpy — apt install"
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pygame python3-numpy python3-pip
}

# sounddevice via offline wheels (pure-python sounddevice + arch-specific cffi)
echo "[boot] pip install sounddevice from staged wheels (offline)"
pip install --break-system-packages --no-index --find-links="$OFFLINE_WHEELS" \
    sounddevice cffi pycparser || {
    echo "[boot] offline wheels failed, falling back to PyPI"
    pip install --break-system-packages sounddevice
}

# Mark done before enabling the kiosk so the bootstrap unit ConditionPath
# fires correctly even if the reboot is interrupted.
mkdir -p "$(dirname "$SENTINEL")"
touch "$SENTINEL"

echo "[boot] enable constellation.service"
systemctl enable constellation.service

echo "[boot] disable bootstrap, reboot"
systemctl disable constellation-bootstrap.service || true
sync
sleep 1
systemctl reboot
