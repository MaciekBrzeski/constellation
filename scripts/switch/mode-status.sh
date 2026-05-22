#!/usr/bin/env bash
# Print the constellation brain's current Wi-Fi mode on stdout, one word:
#   ap      - the constellation-ap hotspot is active
#   wifi    - connected to a normal Wi-Fi network as a client
#   unknown - neither (radio off, not connected, or no nmcli)
# Always exits 0 so callers can rely on the printed word.
set -uo pipefail

CON=constellation-ap

if ! command -v nmcli >/dev/null 2>&1; then
    echo unknown
    exit 0
fi

active=$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null || true)

if grep -qx "$CON:802-11-wireless" <<<"$active"; then
    echo ap
elif grep -q ':802-11-wireless$' <<<"$active"; then
    echo wifi
else
    echo unknown
fi
