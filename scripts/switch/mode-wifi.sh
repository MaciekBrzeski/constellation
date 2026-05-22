#!/usr/bin/env bash
# Clickable mode switch: take the Switch's Wi-Fi out of AP mode and reconnect
# to a normal Wi-Fi network (internet). Reverse of mode-ap.sh.
set -euo pipefail

# --no-pause: skip the closing "Press Enter" prompt (used when a GUI runs us).
NO_PAUSE=0
[[ "${1:-}" == "--no-pause" ]] && NO_PAUSE=1
pause() { [[ $NO_PAUSE -eq 1 ]] || read -rp "Press Enter to close..."; }

CON=constellation-ap

echo "=== Constellation  ->  Wi-Fi client mode ==="
echo

command -v nmcli >/dev/null 2>&1 || {
    echo "ERROR: nmcli (NetworkManager) not found."
    pause
    exit 1
}

if nmcli -t -f NAME connection show --active | grep -qx "$CON"; then
    echo "Shutting down the AP ..."
    nmcli connection down "$CON"
fi

nmcli radio wifi on

# Reconnect to the first saved Wi-Fi network that isn't our own AP.
CLIENT=$(nmcli -t -f NAME,TYPE connection show \
    | awk -F: '$2 == "802-11-wireless" && $1 != "'"$CON"'" { print $1; exit }')

if [[ -n "${CLIENT:-}" ]]; then
    echo "Reconnecting to '$CLIENT' ..."
    nmcli connection up "$CLIENT" \
        || echo "warn: could not bring up '$CLIENT' - pick a network from the Wi-Fi menu."
else
    echo "No saved client Wi-Fi found - pick a network from the Wi-Fi menu."
fi

echo
nmcli -t -f DEVICE,STATE,CONNECTION device | grep -E '^wlp1s0:' || true
echo "Done."
pause
