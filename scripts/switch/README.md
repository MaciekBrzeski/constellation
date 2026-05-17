# Constellation — Switch OLED brain

The "brain" of a multi-projector setup is a modded Nintendo Switch OLED
running Linux. It captures audio, detects beats, and broadcasts them over
Wi-Fi to the worker Pis — and it **hosts the Wi-Fi network itself**, so the
whole installation is self-contained with no external router.

The brain has no display: it never renders, so it needs neither OpenCV /
Pillow (preprocessing only) nor pygame (the workers render). Only numpy +
sounddevice.

## Assumptions

- The Switch runs a Debian/Ubuntu-family Linux — typically **switchroot
  L4T Ubuntu** (arm64). Anything with `apt` and Python 3 works.
- Python **3.11+** preferred (stdlib `tomllib`); `install.sh` installs the
  `tomli` fallback on older Pythons.
- A **USB microphone** is attached — the Switch has no usable built-in mic.
- You have a shell on the Switch (local, or `ssh` over its initial network).

## 1. Get the code onto the Switch

While the Switch still has internet (before it becomes an AP), e.g. over
its initial Wi-Fi or USB-ethernet:

```
sudo apt install -y git
git clone https://github.com/MaciekBrzeski/constellation.git
cd constellation
```

## 2. Install the brain runtime

```
bash scripts/switch/install.sh
```

This (one-time, needs internet):

- `apt install`s numpy, sounddevice, libportaudio2 and the pip toolchain
  (plus, unless disabled, fastapi/uvicorn for `--gui` and python3-tk for the
  native panel);
- installs the `constellation` package with `pip install -e . --no-deps`
  (console scripts `constellation-master` / `constellation-control`, without
  the heavy render/preprocess deps);
- adds your user to the `audio` group (re-login for it to apply);
- installs `constellation-master.service`, with your user + repo path baked
  in (not enabled yet).

Env toggles: `WITH_GUI=0` skips the web dashboard deps, `WITH_PANEL=0` skips
Tkinter.

## 3. Host the Wi-Fi access point

```
sudo bash scripts/switch/setup_ap.sh --ssid constellation --psk 'a-good-password'
```

Brings up an AP at `192.168.50.1`; worker Pis DHCP into `192.168.50.50–150`.
Auto-detects the backend:

- **NetworkManager** present → an `nmcli` hotspot (NM runs its own DHCP).
- otherwise → `hostapd` + `dnsmasq` + the `constellation-ap.service` unit.

Provision each worker SD card pointing `--ssid`/`--psk` at this AP:

```
sudo bash scripts/sd_provision.sh --role worker --animal deer_male \
    --ssid constellation --psk 'a-good-password' ...
```

### Onboard-chip caveat

The Switch's onboard Broadcom Wi-Fi may not support AP mode under Linux.
`setup_ap.sh` checks `iw list` up front and, if `hostapd` fails to start,
prints the fix: plug a USB Wi-Fi dongle that supports AP mode and re-run
with `--interface wlan1`. All of hostapd / dnsmasq / static-IP config
follow the `--interface` flag.

## 4. Run the brain

Foreground test (Ctrl+C to stop):

```
./scripts/switch/run.sh                       # brain + web dashboard on :8000
WITH_GUI=0 ./scripts/switch/run.sh            # brain only
```

Autostart on boot:

```
sudo systemctl enable --now constellation-master.service
journalctl -u constellation-master.service -f
# Expect: [master] multiband onset backend active
#         [master] broadcasting beats to 255.255.255.255:49500
```

The native Tkinter control panel is an alternative front end (needs a
display + `python3-tk`):

```
constellation-control
```

## Verify

```
iw dev wlan0 info                       # expect: type AP
journalctl -u hostapd -f                # (hostapd backend)
nmcli connection show constellation-ap  # (NetworkManager backend)
python3 -c "import sounddevice as sd; print(sd.query_devices())"   # USB mic listed?
```

From a worker Pi: `nmcli dev wifi` lists `constellation`, and `ip addr`
shows a `192.168.50.x` lease. `journalctl -u constellation.service -f` on
the worker should show `[audio] remote beat receiver active`.

## Troubleshooting

- **`PortAudioError` / no mic** — confirm the USB mic enumerates with the
  `sd.query_devices()` check above; pin it via `[audio] device = "<name>"`
  in `config/default.toml` (or the per-user overlay). Make sure your user is
  in the `audio` group (`id -nG`) and you have re-logged in.
- **AP won't start** — see the onboard-chip caveat; re-run `setup_ap.sh`
  with a USB dongle on `--interface wlan1`.
- **`ModuleNotFoundError: tomllib`** — Python <3.11; re-run `install.sh`
  (installs `tomli`).
- **`constellation-master: command not found` in the unit** — the unit runs
  `python3 -m constellation.cli.master`, not the console script, so this
  only matters for interactive use; re-run `install.sh`.

## Files

```
scripts/switch/
├── install.sh                       # one-time brain runtime installer
├── setup_ap.sh                      # one-time Wi-Fi AP provisioner
├── run.sh                           # foreground launch wrapper
├── systemd/
│   ├── constellation-master.service # brain autostart (@USER@/@WORKDIR@ baked in)
│   └── constellation-ap.service     # static IP, ordered before the broadcaster
└── README.md                        # this file
```
