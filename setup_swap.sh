#!/usr/bin/env bash
# Adds a swapfile to the server, as a safety net for small instances (e.g.
# Oracle's free 1GB AMD Micro): without one, a brief memory spike gets a
# container killed outright instead of just running slowly for a moment.
#
# Safe to re-run -- every step checks whether it's already been done.
#
#   sudo ./setup_swap.sh          # 2G (default)
#   sudo ./setup_swap.sh 1G       # or any size fallocate understands
set -euo pipefail

SIZE="${1:-2G}"
SWAPFILE=/swapfile

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 ${1:-}" >&2
    exit 1
fi

if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
    echo "$SWAPFILE is already active -- leaving it as it is."
else
    if [ ! -f "$SWAPFILE" ]; then
        fallocate -l "$SIZE" "$SWAPFILE"
        echo "Created $SIZE $SWAPFILE"
    fi
    chmod 600 "$SWAPFILE"
    mkswap "$SWAPFILE" >/dev/null
    swapon "$SWAPFILE"
    echo "Enabled $SWAPFILE"
fi

if ! grep -q "^$SWAPFILE " /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
    echo "Added to /etc/fstab (survives reboots)"
fi

# Low swappiness: only swap under real memory pressure, so it stays a safety
# net rather than something the system leans on day to day.
echo "vm.swappiness=10" > /etc/sysctl.d/99-swappiness.conf
sysctl -q -w vm.swappiness=10

echo
free -h
