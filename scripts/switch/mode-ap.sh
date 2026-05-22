#!/usr/bin/env bash
# Clickable mode switch: put the Switch's Wi-Fi into the "constellation"
# access-point mode so the worker Pis can join it. On first run this creates
# the NetworkManager hotspot connection; afterwards it just brings it up.
# Reverse it with mode-wifi.sh.
set -euo pipefail

# --no-pause: skip the closing "Press Enter" prompt (used when a GUI runs us).
NO_PAUSE=0
[[ "${1:-}" == "--no-pause" ]] && NO_PAUSE=1
pause() { [[ $NO_PAUSE -eq 1 ]] || read -rp "Press Enter to close..."; }

IFACE=wlp1s0
SSID=constellation
CON=constellation-ap
ADDRESS=192.168.50.1
CHANNEL=6

# AP password — never hardcode a real PSK here (this script is tracked in a
# public repo). Resolved from, in order: $CONSTELLATION_AP_PSK, the local
# untracked file ~/.config/constellation/ap_psk, else a placeholder.
PSK_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/constellation/ap_psk"
if [[ -n "${CONSTELLATION_AP_PSK:-}" ]]; then
    PSK=$CONSTELLATION_AP_PSK
elif [[ -f "$PSK_FILE" ]]; then
    PSK=$(<"$PSK_FILE")
else
    PSK="changeme-constellation"
    echo "WARNING: using placeholder AP password. Set a real one with:"
    echo "  mkdir -p \"\$(dirname \"$PSK_FILE\")\" && echo 'your-password' > \"$PSK_FILE\""
fi
[[ ${#PSK} -ge 8 ]] || { echo "ERROR: AP password must be >= 8 chars (WPA2)."; pause; exit 1; }

echo "=== Constellation  ->  AP mode ==="
echo

command -v nmcli >/dev/null 2>&1 || {
    echo "ERROR: nmcli (NetworkManager) not found."
    pause
    exit 1
}

if ! nmcli -t -f NAME connection show | grep -qx "$CON"; then
    echo "First run - creating hotspot connection '$CON' ..."
    nmcli connection add type wifi ifname "$IFACE" con-name "$CON" \
        autoconnect no ssid "$SSID"
    nmcli connection modify "$CON" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        802-11-wireless.channel "$CHANNEL" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$PSK" \
        ipv4.method shared \
        ipv4.addresses "$ADDRESS/24"
fi

echo "Bringing up AP '$SSID' on $IFACE ..."
if ! nmcli connection up "$CON"; then
    echo
    echo "ERROR: could not start the AP. The Switch's onboard Wi-Fi chip may"
    echo "       not support AP mode - plug in a USB Wi-Fi dongle, or check"
    echo "       'journalctl -u NetworkManager'."
    pause
    exit 1
fi

echo
echo "AP is UP:  SSID = $SSID   address = $ADDRESS"
echo "Workers DHCP into 192.168.50.50-150."
echo "Internet is now OFF on the Switch - use the 'Wi-Fi mode' button to restore it."
pause
