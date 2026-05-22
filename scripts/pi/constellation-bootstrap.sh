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

# Install the full runtime closure (pygame, numpy, aubio, libportaudio2 and
# every SDL / BLAS / mesa dependency) from staged .debs — no internet needed.
# dpkg runs twice so inter-package configure ordering resolves on the 2nd pass.
echo "[boot] installing runtime packages from staged .debs (offline)"
if ls "$OFFLINE_DEBS"/*.deb >/dev/null 2>&1; then
    dpkg -i "$OFFLINE_DEBS"/*.deb 2>&1 || dpkg -i "$OFFLINE_DEBS"/*.deb 2>&1 || true
else
    echo "[boot] no staged .debs found"
fi

# Verify the kiosk's hard requirements. Fall back to apt (needs internet)
# only if the offline set was incomplete — e.g. a Pi OS base image newer than
# the one the .debs were staged against.
echo "[boot] verifying pygame + numpy"
if ! python3 -c "import pygame, numpy" 2>/dev/null; then
    echo "[boot] pygame/numpy still missing — apt fallback (needs internet)"
    apt-get update -y || true
    DEBIAN_FRONTEND=noninteractive apt-get -f install -y || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3-pygame python3-numpy python3-pip || true
fi

# sounddevice via offline wheels (pure-python sounddevice + arch-specific cffi).
# Workers that only listen for remote beats don't strictly need it, so a
# failure here is non-fatal.
echo "[boot] pip install sounddevice from staged wheels (offline)"
pip install --break-system-packages --no-index --find-links="$OFFLINE_WHEELS" \
    sounddevice cffi pycparser \
    || pip install --break-system-packages sounddevice \
    || echo "[boot] sounddevice install failed — remote-beat workers don't need it"

# Mark done before enabling the kiosk so the bootstrap unit ConditionPath
# fires correctly even if the reboot is interrupted.
mkdir -p "$(dirname "$SENTINEL")"
touch "$SENTINEL"

ROLE_FILE=/var/lib/constellation/role
ROLE=$(cat "$ROLE_FILE" 2>/dev/null || echo standalone)
echo "[boot] role=$ROLE"
case $ROLE in
    brain)
        echo "[boot] install GUI deps (fastapi + uvicorn) for control dashboard"
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3-fastapi python3-uvicorn 2>/dev/null || \
            pip install --break-system-packages fastapi 'uvicorn[standard]' || \
            echo "[boot] GUI deps install failed — --gui will be unavailable"
        echo "[boot] enable constellation-master.service"
        systemctl enable constellation-master.service
        ;;
    standalone|worker|*)
        echo "[boot] enable constellation.service"
        systemctl enable constellation.service
        ;;
esac

echo "[boot] disable bootstrap, reboot"
systemctl disable constellation-bootstrap.service || true
sync
sleep 1
systemctl reboot
