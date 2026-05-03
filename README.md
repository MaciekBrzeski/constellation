# constellation

Turn pixel-art animal spritesheets into kinetic, beat-reactive constellation
animations. Vertices and edges are rendered as twinkling stars + faint lines
on a black field; a USB microphone drives action selection so the animal
calms down in quiet sections and breaks into a run on strong beats.

Designed to deploy on a Raspberry Pi Zero 2 W with an HDMI projector — the
preprocessing toolchain (OpenCV, Pillow) runs on the dev host; the Pi only
ships precompiled JSON vertex/star data and pure-Python runtime.

![Demo](docs/demo.gif)

## What it does

Two pipelines share one TOML config (`config/default.toml`):

### Preprocess (dev host)
PNG spritesheet → grayscale frames → binary mask → external contours →
densely-sampled outline + Douglas-Peucker corner indices → JSON per animal.

```
constellation-preprocess -d ~/sprites/deer_male -a deer_male
constellation-preprocess -i sheet.png -a wolf --actions idle walk run
```

Outputs `assets/constellations/<name>.json` — a list of animations
(`idle`, `walk`, `run`, …) where each frame stores dense vertices, an edge
ring, and the indices of "star" vertices (corners). Dense vertices give a
smooth outline; sparse stars keep the silhouette readable as a constellation.

### Run (any host, including Pi)

```
constellation-run --animal deer_male --mode fullscreen --audio
constellation-run --config my.toml --sensitivity 1.4
```

Pygame loop:
- `AnimationController` cycles animals/actions, fetches per-frame vertex
  data, supports linear tweening between adjacent frames.
- `BeatDetector` (background `sounddevice` thread) tracks an adaptive
  noise floor (p10 of recent block RMS) for energy-tier classification,
  then fires beat events via one of two backends:
  - **aubio** (preferred) — `aubio.tempo` autocorrelation tempo tracker,
    phase-locked so subsequent beats land on-beat instead of trailing the
    kick. BPM comes from aubio's autocorrelation estimate.
  - **RMS fallback** — instantaneous RMS > rolling mean × sensitivity past
    cooldown. BPM derived from inter-beat intervals on an audio clock.

  The aubio path is used whenever the library imports successfully; falls
  back silently otherwise.
- `AudioReactiveBehavior` maps energy tier → action set
  (`low → idle/eating`, `medium → walk/alert`, `high → run/jump`). A
  strong-beat lockout (`strong_beat_threshold`, `strong_hold_sec`) holds
  the high-energy state across a burst so the deer doesn't twitch back to
  idle between drum hits.
- Renderer draws background twinkles, dense edges, then sparse star
  glyphs. Glyphs are cached per `(radius, sparkle, color)` and blitted
  with `set_alpha` — ~32× faster than per-pixel drawing.

Runtime keys: `Q`/`Esc` quit, `Space` cycles animal, `1`–`0` pick action.

## Project layout

```
constellation/
├── src/constellation/
│   ├── animation/        # controller, behavior, timing, tweening
│   ├── audio/            # beat detection, adaptive noise floor
│   ├── cli/              # constellation-run, constellation-preprocess
│   ├── geometry/         # vertex scaling, edge fallback, bg stars
│   ├── preprocessing/    # spritesheet → contours → JSON
│   └── rendering/        # display init, glyph-cached drawing
├── assets/
│   ├── constellations/   # precompiled JSON (deer_male, deer_female, …)
│   └── spritesheets/     # source PNGs for preprocess
├── config/default.toml   # display / animation / audio / render / preprocess
├── tests/                # pytest suite (31 tests across all subsystems)
├── scripts/
│   ├── audio_diag.py     # offline replay + stats for tuning the detector
│   ├── build_pi_bundle.sh
│   ├── sd_provision.sh   # host-side SD card provisioner
│   └── pi/               # systemd units + bootstrap script for the Pi
└── offline/              # libportaudio2 .deb + sounddevice/cffi/pycparser
                          # wheels, staged for offline first-boot install
```

## Local dev

Python ≥ 3.11. Install editable with audio + dev extras:

```
python -m venv .venv
source .venv/bin/activate          # or activate.fish
pip install -e '.[audio,dev]'
```

Run:

```
constellation-run --mode windowed --animal deer_male --audio
pytest                              # 31 tests
ruff check src tests
mypy src
```

## Raspberry Pi deployment

Target: **Pi Zero 2 W** (512 MB RAM, ARMv8) running **Pi OS Lite**
(Bookworm or Trixie). Lite is required — Desktop's wayfire compositor owns
DRM and conflicts with the kmsdrm kiosk path.

### One-pass plug-and-play SD card

1. Flash Pi OS Lite arm64 to the SD card with `dd` (or Pi Imager).
2. Build the runtime bundle on the dev host:

   ```
   ./scripts/build_pi_bundle.sh        # → dist/constellation-pi.tar.gz
   ```

3. With the card mounted at `/run/media/$USER/{boot,rootfs}`, run the
   provisioner as root:

   ```
   sudo bash scripts/sd_provision.sh \
       --rootfs /run/media/$USER/rootfs \
       --boot   /run/media/$USER/bootfs \
       --ssid   'MyNetwork' \
       --psk    'mypassword' \
       --ssh-pubkey ~/.ssh/id_ed25519.pub   # optional, for passwordless SSH
   ```

   It drops the bundle into `/home/pi/constellation/`, installs both
   systemd units, writes a NetworkManager profile (mode 600) with
   hardcoded DNS (1.1.1.1, 8.8.8.8), masks `userconfig.service` so the
   first-boot wizard doesn't hang headless boot, enables persistent
   journals, enables `sshd` (with optional pubkey-based auth via
   `--ssh-pubkey`), and adds `pi` to the `audio,video,render,input,tty`
   groups.

### Connecting from another device (laptop, Android Termux, …)

After first boot, find the Pi's IP from your router or run `hostname -I`
on the Pi. Then:

```
ssh pi@<pi-ip>
```

If you provisioned without `--ssh-pubkey`, set a password on the Pi
once with `sudo passwd pi` before SSH will accept you. From Android,
`pkg install openssh` in Termux gives you the same `ssh` client.

4. Unmount + eject. Insert into the Pi, power on.

### First-boot timeline (~3–4 min)

1. Pi OS Lite boots, fs auto-resizes, reboots once.
2. NetworkManager joins Wi-Fi via the baked profile.
3. `constellation-bootstrap.service` (oneshot, runs once via a
   `ConditionPathExists=!/var/lib/constellation/bootstrap-done` sentinel):
   - `dpkg -i` the staged `libportaudio2` .deb (offline, no apt needed)
   - `pip install --no-index --find-links=offline/wheels sounddevice cffi
     pycparser` (offline)
   - falls back to apt + PyPI if the offline path fails
   - stamps the sentinel, enables `constellation.service`, reboots
4. `constellation.service` launches the deer fullscreen via `kmsdrm` SDL
   driver. Restarts on failure. Logs to journal.

### Updating an already-deployed Pi over SSH

The kiosk service runs out of `/home/pi/constellation/`, populated by the
provisioner from `dist/constellation-pi.tar.gz`. To roll out new code
without re-flashing the SD card:

**Option A — clone the repo on the Pi (one-time setup, then `git pull`):**

```
# on Pi (first time only)
cd ~
sudo apt install -y git
git clone https://github.com/MaciekBrzeski/constellation.git constellation-src
# wire the runtime files into where the unit expects them
rsync -a constellation-src/src/constellation/ constellation/constellation/
rsync -a constellation-src/assets/ constellation/assets/
rsync -a constellation-src/config/ constellation/config/

# updates after that
cd ~/constellation-src && git pull
rsync -a src/constellation/ ~/constellation/constellation/
rsync -a assets/ ~/constellation/assets/
rsync -a config/ ~/constellation/config/
sudo systemctl restart constellation.service
```

**Option B — rebuild bundle on dev host, scp over:**

```
# on dev host
./scripts/build_pi_bundle.sh
scp dist/constellation-pi.tar.gz pi@<pi-ip>:/tmp/

# on Pi
cd ~/constellation
tar xzf /tmp/constellation-pi.tar.gz --strip-components=1
sudo systemctl restart constellation.service
```

**Installing aubio** (one-time, for phase-locked beat tracking):

```
sudo apt install -y python3-aubio
python3 -c "import aubio; print(aubio.version)"   # expects 0.4.9
sudo systemctl restart constellation.service
journalctl -u constellation.service -n 5 --no-pager
# look for: [audio] aubio tempo tracker active (phase-locked)
```

Without aubio installed, `BeatDetector` falls back to the RMS path
silently — log will say `aubio unavailable, using RMS onset detection`.

### Debugging on the Pi

- `journalctl -u constellation-bootstrap.service` — first-boot log
- `journalctl -u constellation.service -f` — kiosk runtime
- `/var/log/constellation-bootstrap.log` — explicit tee from the script
- USB mic check: `python3 -c "import sounddevice as sd; print(sd.query_devices())"`

## Audio detector tuning

Use `scripts/audio_diag.py` to record or replay a `.wav` with the live
detector and dump per-block RMS, rolling mean, smoothed tier energy,
noise floor, beat strength, and BPM:

```
python scripts/audio_diag.py record --seconds 30 --out tmp/sample.wav
python scripts/audio_diag.py replay tmp/sample.wav --sensitivity 1.5
```

Knobs in `[audio]` of `config/default.toml`:

| key | meaning |
|-----|---------|
| `sensitivity` | beat threshold = rolling_mean × sensitivity |
| `noise_gate` / `min_noise_floor` | absolute floor below which RMS is ignored |
| `low_intensity_ratio` / `high_intensity_ratio` | tier boundaries vs noise floor |
| `cooldown_ms` | min interval between beat events (audio-clock) |
| `strong_beat_threshold` | beat_strength ≥ this → force `high` tier |
| `strong_hold_sec` | how long a strong beat locks the high tier |
| `silence_timeout_sec` | retained as kwarg for back-compat (not used) |
| `use_aubio` | toggle the aubio tempo tracker (defaults to `true`) |

## Gotchas

- `BeatDetector._last_beat_time` stores **milliseconds**;
  `last_beat_time` property returns **seconds**. Don't compare the raw
  field against `time.monotonic()`.
- `drawing._ensure_star_radii` reseeds only when vertex count changes —
  switching between two animals with the same vert count keeps the old
  twinkle pattern.
- `AudioReactiveBehavior._set_action_if_available` and
  `cli/run.py._get_available_actions` reach into `_animals` directly;
  rename ripples both places.
- The Trixie repo has no `python3-sounddevice` package, hence the
  staged wheels in `offline/`.

## License

MIT (or specify your own).
