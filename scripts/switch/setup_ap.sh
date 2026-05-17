#!/usr/bin/env bash
# Turn the constellation "brain" (a modded Switch OLED running Linux) into a
# self-contained Wi-Fi access point so the worker Pis can join it with no
# external router. Run once, as root, on the brain.
#
# Topology after this runs:
#   brain  = AP at 192.168.50.1  (this machine)
#   workers= DHCP clients in 192.168.50.50-150
# UDP beat broadcast (255.255.255.255:49500) works because all three sit on
# one L2 segment.
#
# Two backends, auto-detected:
#   NetworkManager present -> an `nmcli` hotspot connection (NM runs its own
#                             DHCP via ipv4.method=shared). Robust, preferred.
#   otherwise              -> hostapd + dnsmasq + the constellation-ap.service
#                             unit shipped alongside this script.
#
# Usage (must be root):
#   sudo bash scripts/switch/setup_ap.sh \
#       --ssid constellation --psk 'a-good-password' \
#       [--channel 6] [--country PL] [--interface wlan0] \
#       [--address 192.168.50.1]
#
# If the Switch's onboard Broadcom chip rejects AP mode, plug a USB Wi-Fi
# dongle that supports it and re-run with --interface wlan1.

set -euo pipefail

die() { echo "[switch-ap] error: $*" >&2; exit 1; }
log() { echo "[switch-ap] $*"; }

# ----- args
SSID=""
PSK=""
CHANNEL=6
COUNTRY=PL
IFACE=wlan0
ADDRESS=192.168.50.1
while [[ $# -gt 0 ]]; do
    case $1 in
        --ssid)      SSID=$2;     shift 2 ;;
        --psk)       PSK=$2;      shift 2 ;;
        --channel)   CHANNEL=$2;  shift 2 ;;
        --country)   COUNTRY=$2;  shift 2 ;;
        --interface) IFACE=$2;    shift 2 ;;
        --address)   ADDRESS=$2;  shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"
[[ -n $SSID ]] || die "need --ssid"
[[ -n $PSK  ]] || die "need --psk"
[[ ${#PSK} -ge 8 ]] || die "--psk must be at least 8 characters (WPA2 requirement)"
DHCP_BASE=${ADDRESS%.*}   # e.g. 192.168.50

# ----- pre-flight: does the interface support AP mode at all?
if command -v iw >/dev/null 2>&1; then
    if iw list 2>/dev/null | grep -A 12 "Supported interface modes" | grep -qE '\* AP$'; then
        log "$IFACE's chipset advertises AP mode — good"
    else
        log "WARNING: no AP mode in 'iw list'. The onboard Broadcom chip may"
        log "         not support it. If hostapd/NM fails below, plug a USB"
        log "         Wi-Fi dongle and re-run with --interface wlan1."
    fi
fi

# Make sure the radio isn't soft-blocked.
command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi || true

# ----- backend A: NetworkManager-native hotspot
if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager; then
    log "NetworkManager detected — creating an nmcli hotspot on $IFACE"
    nmcli connection delete constellation-ap >/dev/null 2>&1 || true
    nmcli connection add type wifi ifname "$IFACE" con-name constellation-ap \
        autoconnect yes ssid "$SSID"
    nmcli connection modify constellation-ap \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        802-11-wireless.channel "$CHANNEL" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$PSK" \
        ipv4.method shared \
        ipv4.addresses "$ADDRESS/24" \
        connection.autoconnect-priority 100
    nmcli connection up constellation-ap \
        || die "nmcli failed to bring up the hotspot — see: journalctl -u NetworkManager"
    log "hotspot '$SSID' up on $IFACE at $ADDRESS (NM-managed, shared DHCP)"
    log "verify: iw dev $IFACE info   |   nmcli connection show constellation-ap"
    exit 0
fi

# ----- backend B: standalone hostapd + dnsmasq
log "NetworkManager not in use — configuring hostapd + dnsmasq"

if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y hostapd dnsmasq \
        || die "apt-get failed to install hostapd/dnsmasq — install them manually"
else
    command -v hostapd >/dev/null 2>&1 || die "hostapd not installed and no apt-get"
    command -v dnsmasq >/dev/null 2>&1 || die "dnsmasq not installed and no apt-get"
fi

log "writing /etc/hostapd/hostapd.conf"
mkdir -p /etc/hostapd
cat > /etc/hostapd/hostapd.conf <<EOF
interface=$IFACE
driver=nl80211
ssid=$SSID
country_code=$COUNTRY
hw_mode=g
channel=$CHANNEL
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$PSK
EOF
chmod 600 /etc/hostapd/hostapd.conf
# Point the hostapd unit at our config (Debian default).
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' \
    /etc/default/hostapd 2>/dev/null || true

log "writing /etc/dnsmasq.d/constellation.conf"
mkdir -p /etc/dnsmasq.d
cat > /etc/dnsmasq.d/constellation.conf <<EOF
# constellation brain access point — workers get DHCP leases here.
interface=$IFACE
bind-interfaces
dhcp-range=${DHCP_BASE}.50,${DHCP_BASE}.150,12h
# Offline install: no gateway, no DNS handed out.
dhcp-option=3
dhcp-option=6
EOF

# Keep NetworkManager (if installed but inactive) off this interface.
if [[ -d /etc/NetworkManager/conf.d ]]; then
    cat > /etc/NetworkManager/conf.d/constellation-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:$IFACE
EOF
fi

log "installing constellation-ap.service"
UNIT_SRC="$(dirname "$0")/systemd/constellation-ap.service"
if [[ -f $UNIT_SRC ]]; then
    install -m 0644 "$UNIT_SRC" /etc/systemd/system/constellation-ap.service
    # Bake the chosen interface/address into the unit's pre-start.
    sed -i "s|@IFACE@|$IFACE|g; s|@ADDRESS@|$ADDRESS|g" \
        /etc/systemd/system/constellation-ap.service
else
    log "WARNING: $UNIT_SRC missing — static IP will not persist across reboot"
fi

systemctl daemon-reload
systemctl unmask hostapd 2>/dev/null || true
systemctl enable --now constellation-ap.service hostapd dnsmasq \
    || true

sleep 2
if journalctl -u hostapd --since "1 min ago" --no-pager 2>/dev/null \
        | grep -qiE 'AP mode not supported|nl80211: Failed to set|Failed to start'; then
    die "hostapd could not start an AP on $IFACE. The onboard chip likely
       lacks AP mode — plug a USB Wi-Fi dongle and re-run:
         sudo bash $0 --ssid '$SSID' --psk '****' --interface wlan1"
fi

log "hostapd + dnsmasq up — '$SSID' on $IFACE at $ADDRESS"
log "verify: iw dev $IFACE info   |   journalctl -u hostapd -f"
