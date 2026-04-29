#!/usr/bin/env bash
# Install runtime dependencies for constellation on Raspberry Pi.
# Target: Pi OS Bookworm (Python 3.11+). On Bullseye (3.9) tomllib is absent —
# install tomli via pip as a fallback.
set -euo pipefail

echo "[install] apt update"
sudo apt-get update

echo "[install] pygame + numpy (system packages)"
sudo apt-get install -y python3-pygame python3-numpy

# Audio is optional — only needed for --audio (beat-reactive mode).
if [ "${WITH_AUDIO:-1}" = "1" ]; then
    echo "[install] sounddevice + libportaudio2 (audio mode)"
    sudo apt-get install -y libportaudio2 python3-sounddevice \
        || echo "[warn] sounddevice not available via apt — skipping audio"
fi

# Check tomllib (stdlib on 3.11+); install tomli as fallback for 3.9/3.10.
if ! python3 -c "import tomllib" 2>/dev/null; then
    echo "[install] Python <3.11 detected — installing tomli fallback"
    pip3 install --break-system-packages --user tomli \
        || pip3 install --user tomli
fi

echo "[install] done. Run:   ./run.sh"
echo "          or kiosk:    sudo cp systemd/constellation.service /etc/systemd/system/"
echo "                       sudo systemctl enable --now constellation.service"
