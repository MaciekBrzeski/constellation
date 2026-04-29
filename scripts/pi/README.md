# Constellation — Raspberry Pi bundle

Precompiled constellation assets and runtime-only source for running the
display on a Raspberry Pi (tested target: Pi Zero W with HDMI projector).

## Requirements

- Raspberry Pi OS **Bookworm** or later (Python 3.11+ — required for stdlib
  `tomllib`; Bullseye/3.9 also works via the `tomli` fallback installed by
  `install.sh`).
- HDMI output (projector, monitor, TV).
- **Optional** USB microphone for beat-reactive audio mode.

The bundle ships **no OpenCV, no Pillow** — those are only needed for
preprocessing spritesheets, which is done on the dev machine. Assets are
precompiled to JSON under `assets/constellations/`.

## Install

```
./install.sh
```

Installs `python3-pygame`, `python3-numpy`, and (unless `WITH_AUDIO=0`)
`python3-sounddevice` + `libportaudio2` via apt.

## Run

```
./run.sh                       # fullscreen, deer_male, audio on
./run.sh --animal deer_female  # pick another
./run.sh --mode windowed       # windowed
WITH_AUDIO=0 ./run.sh          # disable beat detection (no mic required)
```

Controls inside the window:
- `Q` / `Esc` — quit
- `Space` — cycle animal
- `1`–`8` — pick action (index into current animal's action list)

## Kiosk mode (auto-start on boot)

```
sudo cp systemd/constellation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now constellation.service
```

Logs: `journalctl -u constellation.service -f`.

Edit the unit if your user is not `pi` or the bundle lives elsewhere than
`/home/pi/constellation`.

## Tuning

- `config/default.toml` — `display.fps`, `render.bg_star_count`, tiers, etc.
  All display values honored; fullscreen overrides width/height with the
  current desktop resolution.
- For the Pi Zero specifically, consider dropping `display.fps` to 30 and
  `render.bg_star_count` to ~40 if frame pacing suffers.

## Troubleshooting

- **No display on projector / black screen**: on Lite OS without X, set
  `Environment=SDL_VIDEODRIVER=kmsdrm` in the systemd unit (commented-out
  line is provided).
- **`ModuleNotFoundError: tomllib`**: you're on Python <3.11; re-run
  `./install.sh` to install the `tomli` fallback.
- **`PortAudioError: Could not open microphone`**: no USB mic connected, or
  mic isn't the default input. List devices with
  `python3 -c "import sounddevice as sd; print(sd.query_devices())"` and
  set the `device=` kwarg in `src/constellation/audio/beat.py::start()` if
  you need to pin a specific one.
- **Unresponsive on Pi Zero**: the CPU is genuinely small; reduce FPS and
  bg star count as above, and prefer windowed mode at lower resolution.

## Layout

```
constellation-pi/
├── constellation/         # runtime package (no cv2/PIL)
├── assets/constellations/ # precompiled JSON vertex+star data
├── config/default.toml    # render/animation/audio config
├── install.sh             # apt deps
├── run.sh                 # launch wrapper
├── systemd/               # kiosk unit
└── README.md              # this file
```
