#!/usr/bin/env bash
# Build a runtime-only tarball for deploying the constellation display to a
# Raspberry Pi. Strips preprocessing code that depends on cv2/Pillow (Pi can't
# easily build those) and ships precompiled JSON vertex/star data instead.
set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR=dist/constellation-pi
TAR=dist/constellation-pi.tar.gz

echo "[build] cleaning $OUT_DIR"
rm -rf "$OUT_DIR" "$TAR"
mkdir -p "$OUT_DIR/constellation"

echo "[build] copying runtime source (no cv2/PIL)"
# Top-level package files
cp src/constellation/__init__.py      "$OUT_DIR/constellation/"
cp src/constellation/__main__.py      "$OUT_DIR/constellation/"
cp src/constellation/config.py        "$OUT_DIR/constellation/"

# Subpackages that don't need cv2/PIL
for sub in animation audio geometry rendering control web; do
    cp -r "src/constellation/$sub" "$OUT_DIR/constellation/"
done

# cli: run.py + master.py (preprocess.py needs cv2/PIL — skip)
mkdir -p "$OUT_DIR/constellation/cli"
cp src/constellation/cli/__init__.py "$OUT_DIR/constellation/cli/"
cp src/constellation/cli/run.py      "$OUT_DIR/constellation/cli/"
cp src/constellation/cli/master.py   "$OUT_DIR/constellation/cli/"

# preprocessing: only export.py (pure stdlib, imported by controller)
mkdir -p "$OUT_DIR/constellation/preprocessing"
cp src/constellation/preprocessing/__init__.py "$OUT_DIR/constellation/preprocessing/"
cp src/constellation/preprocessing/export.py   "$OUT_DIR/constellation/preprocessing/"

# Strip __pycache__ dirs from the copied tree
find "$OUT_DIR/constellation" -type d -name __pycache__ -exec rm -rf {} +

echo "[build] copying precompiled assets + config"
mkdir -p "$OUT_DIR/assets/constellations"
cp assets/constellations/*.json "$OUT_DIR/assets/constellations/"
cp -r config "$OUT_DIR/"

if [ -d offline ]; then
    echo "[build] copying offline debs + wheels for first-boot installs"
    cp -r offline "$OUT_DIR/"
fi

echo "[build] copying Pi scripts"
cp scripts/pi/install.sh "$OUT_DIR/"
cp scripts/pi/run.sh     "$OUT_DIR/"
cp scripts/pi/README.md  "$OUT_DIR/"
cp scripts/pi/constellation-bootstrap.sh "$OUT_DIR/"
mkdir -p "$OUT_DIR/systemd"
cp scripts/pi/systemd/constellation.service "$OUT_DIR/systemd/"
cp scripts/pi/systemd/constellation-bootstrap.service "$OUT_DIR/systemd/"
cp scripts/pi/systemd/constellation-master.service "$OUT_DIR/systemd/"
chmod +x "$OUT_DIR"/install.sh "$OUT_DIR"/run.sh "$OUT_DIR"/constellation-bootstrap.sh

echo "[build] tarball"
tar czf "$TAR" -C dist constellation-pi

SIZE=$(du -sh "$TAR" | cut -f1)
COUNT=$(find "$OUT_DIR" -type f | wc -l)
echo "[build] done: $TAR ($SIZE, $COUNT files)"
