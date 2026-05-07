#!/usr/bin/env bash
# Stage a Raspberry Pi OS SD card for plug-and-play kiosk boot of the
# constellation deer animation.
#
# What it does (idempotently, run as root):
#   - validates the rootfs looks like Pi OS (Bookworm or Trixie)
#   - drops the runtime bundle into /home/pi/constellation/
#   - installs systemd units (constellation.service + constellation-bootstrap.service)
#   - installs the bootstrap shell script to /usr/local/bin/
#   - creates Wants symlinks under /etc/systemd/system/multi-user.target.wants/
#   - masks userconfig.service (Pi OS first-boot wizard) so headless boot works
#   - drops a Wi-Fi NetworkManager profile into /etc/NetworkManager/system-connections/
#   - adds the pi user to audio,video,render,input,tty groups
#
# Usage (must be root):
#   sudo bash scripts/sd_provision.sh \
#       --rootfs /run/media/wruszbit/rootfs \
#       --boot   /run/media/wruszbit/bootfs \
#       --ssid   'MyNetwork' \
#       --psk    'mypassword' \
#       [--role standalone|brain|worker] \
#       [--animal NAME]            # required for --role worker
#       [--master HOST:PORT]       # for worker; default 0.0.0.0:49500
#       [--ssh-pubkey ~/.ssh/id_ed25519.pub] \
#       [--bundle dist/constellation-pi.tar.gz] \
#       [--user pi]
#
# Roles:
#   standalone (default): mic + display on one Pi, current behavior.
#   brain:                mic-only Pi, broadcasts beats over UDP, no display.
#   worker:               display-only Pi, listens for beats from a brain Pi.

set -euo pipefail

die() { echo "[provision] error: $*" >&2; exit 1; }
log() { echo "[provision] $*"; }

# ----- arg parsing
ROOTFS=""
BOOT=""
SSID=""
PSK=""
BUNDLE="dist/constellation-pi.tar.gz"
PI_USER="pi"
SSH_PUBKEY=""
ROLE="standalone"
ANIMAL=""
MASTER="0.0.0.0:49500"
while [[ $# -gt 0 ]]; do
    case $1 in
        --rootfs)      ROOTFS=$2;     shift 2 ;;
        --boot)        BOOT=$2;       shift 2 ;;
        --ssid)        SSID=$2;       shift 2 ;;
        --psk)         PSK=$2;        shift 2 ;;
        --bundle)      BUNDLE=$2;     shift 2 ;;
        --user)        PI_USER=$2;    shift 2 ;;
        --ssh-pubkey)  SSH_PUBKEY=$2; shift 2 ;;
        --role)        ROLE=$2;       shift 2 ;;
        --animal)      ANIMAL=$2;     shift 2 ;;
        --master)      MASTER=$2;     shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done

case $ROLE in
    standalone|brain|worker) ;;
    *) die "--role must be one of: standalone, brain, worker (got: $ROLE)" ;;
esac
if [[ $ROLE == "worker" && -z $ANIMAL ]]; then
    die "--role worker requires --animal NAME"
fi

[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"
[[ -n $ROOTFS && -d $ROOTFS ]] || die "--rootfs path missing or not a directory"
[[ -n $BOOT   && -d $BOOT   ]] || die "--boot path missing or not a directory"
[[ -n $SSID && -n $PSK ]] || die "need --ssid and --psk"
[[ -f $BUNDLE ]] || die "bundle not found at $BUNDLE — run scripts/build_pi_bundle.sh first"
[[ -z $SSH_PUBKEY || -f $SSH_PUBKEY ]] || die "--ssh-pubkey path not a file: $SSH_PUBKEY"

# ----- sanity-check the rootfs
[[ -f "$ROOTFS/etc/os-release" ]] || die "$ROOTFS doesn't look like a Linux rootfs"
. <(grep -E '^(ID|VERSION_CODENAME|PRETTY_NAME)=' "$ROOTFS/etc/os-release" | sed 's/^/OS_/')
log "rootfs: $OS_PRETTY_NAME"
[[ $OS_ID == "debian" || $OS_ID == "raspbian" ]] \
    || die "expected Debian/Raspbian rootfs, got ID=$OS_ID"

# Detect the Pi user (uid 1000 by default). Caller may override via --user.
DETECTED_USER=$(awk -F: '$3 == 1000 {print $1; exit}' "$ROOTFS/etc/passwd")
[[ -n $DETECTED_USER ]] || die "no UID-1000 user on rootfs"
[[ $DETECTED_USER == "$PI_USER" ]] || \
    log "warn: requested --user=$PI_USER but rootfs has $DETECTED_USER (using $DETECTED_USER)"
PI_USER=$DETECTED_USER

PI_HOME=$(awk -F: -v u="$PI_USER" '$1 == u {print $6}' "$ROOTFS/etc/passwd")
PI_UID=$(awk -F: -v u="$PI_USER" '$1 == u {print $3}' "$ROOTFS/etc/passwd")
PI_GID=$(awk -F: -v u="$PI_USER" '$1 == u {print $4}' "$ROOTFS/etc/passwd")
log "pi user: $PI_USER (uid=$PI_UID gid=$PI_GID home=$PI_HOME)"

# ----- extract the runtime bundle into the user's home
BUNDLE_ABS=$(readlink -f "$BUNDLE")
TARGET_DIR="$ROOTFS$PI_HOME/constellation"
log "extracting $BUNDLE_ABS → $TARGET_DIR"
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
tar xzf "$BUNDLE_ABS" -C "$TARGET_DIR" --strip-components=1
chown -R "$PI_UID:$PI_GID" "$TARGET_DIR"

# ----- install systemd units
log "installing systemd units (role=$ROLE)"
install -m 0644 "$TARGET_DIR/systemd/constellation-bootstrap.service" \
    "$ROOTFS/etc/systemd/system/constellation-bootstrap.service"

UNIT_DST="$ROOTFS/etc/systemd/system/constellation.service"
MASTER_UNIT_DST="$ROOTFS/etc/systemd/system/constellation-master.service"

if [[ -f "$TARGET_DIR/systemd/constellation-master.service" ]]; then
    install -m 0644 "$TARGET_DIR/systemd/constellation-master.service" \
        "$MASTER_UNIT_DST"
fi

case $ROLE in
    standalone)
        install -m 0644 "$TARGET_DIR/systemd/constellation.service" "$UNIT_DST"
        ;;
    brain)
        # No display unit on brain Pi — only the broadcaster runs.
        rm -f "$UNIT_DST"
        if [[ ! -f "$MASTER_UNIT_DST" ]]; then
            die "constellation-master.service missing from bundle — rebuild with scripts/build_pi_bundle.sh"
        fi
        ;;
    worker)
        # Replace the standalone unit's ExecStart so the Pi listens for beats
        # over UDP and renders a specific animal instead of opening a mic.
        install -m 0644 "$TARGET_DIR/systemd/constellation.service" "$UNIT_DST"
        sed -i -E "s|^ExecStart=.*|ExecStart=/usr/bin/python3 -m constellation --config config/default.toml --animal ${ANIMAL} --mode fullscreen --remote-beat ${MASTER}|" \
            "$UNIT_DST"
        ;;
esac

# Stamp the role for the bootstrap script to read.
mkdir -p "$ROOTFS/var/lib/constellation"
echo "$ROLE" > "$ROOTFS/var/lib/constellation/role"

# ----- install bootstrap script
log "installing /usr/local/bin/constellation-bootstrap.sh"
install -m 0755 "$TARGET_DIR/constellation-bootstrap.sh" \
    "$ROOTFS/usr/local/bin/constellation-bootstrap.sh"

# ----- enable units (multi-user.target.wants symlinks)
log "enabling units in multi-user.target.wants"
WANTS="$ROOTFS/etc/systemd/system/multi-user.target.wants"
mkdir -p "$WANTS"
ln -sfn ../constellation-bootstrap.service "$WANTS/constellation-bootstrap.service"

case $ROLE in
    standalone|worker)
        # Pre-create the kiosk symlink — bootstrap re-enables on first boot,
        # this just makes it survive re-runs.
        ln -sfn ../constellation.service "$WANTS/constellation.service"
        rm -f "$WANTS/constellation-master.service"
        ;;
    brain)
        ln -sfn ../constellation-master.service "$WANTS/constellation-master.service"
        rm -f "$WANTS/constellation.service"
        ;;
esac

# ----- mask the first-boot wizard so headless boot doesn't hang on it
log "masking userconfig.service (Pi OS first-boot wizard)"
ln -sfn /dev/null "$ROOTFS/etc/systemd/system/userconfig.service"

# ----- Wi-Fi NetworkManager profile
log "writing Wi-Fi profile for SSID '$SSID'"
NM_DIR="$ROOTFS/etc/NetworkManager/system-connections"
mkdir -p "$NM_DIR"
NM_FILE="$NM_DIR/constellation-wifi.nmconnection"
UUID=$(cat /proc/sys/kernel/random/uuid)
cat > "$NM_FILE" <<EOF
[connection]
id=constellation-wifi
uuid=$UUID
type=wifi
autoconnect=true
autoconnect-priority=100

[wifi]
mode=infrastructure
ssid=$SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$PSK

[ipv4]
method=auto
dns=1.1.1.1;8.8.8.8;
ignore-auto-dns=true

[ipv6]
addr-gen-mode=default
method=auto
EOF
chmod 600 "$NM_FILE"
chown 0:0 "$NM_FILE"

# ----- enable SSH (Pi OS firstboot trigger + systemd wants symlink belt-and-braces)
log "enabling sshd"
touch "$BOOT/ssh"
# Pre-create the wants symlink in case the firstboot trigger doesn't fire
# (e.g. if the bootfs partition isn't mounted at /boot/firmware). ssh.service
# ships in openssh-server which is preinstalled on Pi OS Lite.
ln -sfn /lib/systemd/system/ssh.service \
    "$WANTS/ssh.service"

# Drop authorized_keys if a pubkey was provided.
if [[ -n $SSH_PUBKEY ]]; then
    log "installing pubkey for $PI_USER from $SSH_PUBKEY"
    SSH_DIR="$ROOTFS$PI_HOME/.ssh"
    mkdir -p "$SSH_DIR"
    cat "$SSH_PUBKEY" >> "$SSH_DIR/authorized_keys"
    chmod 700 "$SSH_DIR"
    chmod 600 "$SSH_DIR/authorized_keys"
    chown -R "$PI_UID:$PI_GID" "$SSH_DIR"
else
    log "no --ssh-pubkey given; you'll need to set a password on the Pi (sudo passwd $PI_USER)"
fi

# ----- enable persistent journal so we can debug across reboots
log "enabling persistent systemd journal"
mkdir -p "$ROOTFS/var/log/journal"
chown 0:0 "$ROOTFS/var/log/journal"

# ----- group memberships (audio + video + render + input + tty for kmsdrm)
log "adding $PI_USER to audio,video,render,input,tty"
for g in audio video render input tty; do
    if grep -qE "^$g:" "$ROOTFS/etc/group"; then
        # Append PI_USER to comma list if absent.
        if ! awk -F: -v g="$g" -v u="$PI_USER" '$1 == g {
            n = split($4, arr, ","); for (i=1; i<=n; i++) if (arr[i] == u) found=1
            exit
        } END { exit !found }' "$ROOTFS/etc/group"; then
            sed -i -E "/^$g:/{ s/:([^:]*)$/:\1,$PI_USER/; s/::,/:/; s/:,$PI_USER/:$PI_USER/ }" \
                "$ROOTFS/etc/group"
        fi
    fi
done

# ----- final sync
sync
log "done. summary:"
echo "  role:        $ROLE"
case $ROLE in
    worker)
        echo "  animal:      $ANIMAL"
        echo "  master:      $MASTER (UDP)"
        ;;
    brain)
        echo "  master:      broadcasting on port 49500 (config/default.toml)"
        ;;
esac
echo "  bundle:      $TARGET_DIR ($(find "$TARGET_DIR" -type f | wc -l) files)"
case $ROLE in
    standalone) echo "  units:       constellation.service + constellation-bootstrap.service" ;;
    brain)      echo "  units:       constellation-master.service + constellation-bootstrap.service" ;;
    worker)     echo "  units:       constellation.service (worker, --remote-beat) + constellation-bootstrap.service" ;;
esac
echo "  wifi:        $NM_FILE (mode 600)"
echo "  user:        $PI_USER added to audio,video,render,input,tty"
echo "  wizard:      userconfig.service masked"
if [[ -n $SSH_PUBKEY ]]; then
    echo "  ssh:         enabled, pubkey installed for $PI_USER"
else
    echo "  ssh:         enabled (no pubkey — set password on Pi: sudo passwd $PI_USER)"
fi
echo
echo "Next: unmount and eject the card, then insert into the Pi Zero 2 W."
echo "  udisksctl unmount -b /dev/sdb1"
echo "  udisksctl unmount -b /dev/sdb2"
echo "  udisksctl power-off -b /dev/sdb"
