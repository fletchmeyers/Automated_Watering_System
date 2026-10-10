#!/bin/sh
# deploy/backup.sh — bundle everything on the Pi that isn't in git.
#
# Run on the Pi as the normal user:
#     ~/Automated_Watering_System/deploy/backup.sh           # services keep running
#     ~/Automated_Watering_System/deploy/backup.sh --stop    # stop them first, and
#                                                            # leave them stopped
#
# Use --stop when moving to a new card, so no readings arrive after the
# snapshot and get left behind on the old card. Bring the services back
# with: sudo systemctl start garden-sensor garden-api garden-wifi
#
# Writes ~/garden_backup_<date>.tar.gz containing:
#   data/        sensors.db (a consistent .backup snapshot), nodes.json (the
#                node list), node_info.json,
#                data_from_pico.txt, archive/
#   etc/         /etc/garden-api.env (weather API keys)
#   cloudflared/ /etc/cloudflared/ and ~/.cloudflared/ (tunnel credentials)
#
# The bundle holds secrets (API keys, tunnel credentials): keep it off any
# public place. deploy/setup.sh restores it onto a fresh card.
set -e

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo; it calls sudo itself."
    exit 1
fi

REPO=$(cd "$(dirname "$0")/.." && pwd)
DATA="$REPO/raspberrypi"
STAMP=$(date +%Y-%m-%d_%H%M)
OUT="$HOME/garden_backup_$STAMP.tar.gz"
WORK=$(mktemp -d)
trap 'sudo rm -rf "$WORK"' EXIT

if [ "$1" = "--stop" ]; then
    echo "== Stopping services (they stay stopped)"
    sudo systemctl stop garden-sensor garden-api garden-wifi 2>/dev/null || true
fi

echo "== Data"
mkdir -p "$WORK/data"
if [ -e "$DATA/sensors.db" ]; then
    # .backup gives a consistent snapshot even while main.py is writing;
    # copying the file directly can catch it half-written.
    sqlite3 "$DATA/sensors.db" ".backup '$WORK/data/sensors.db'"
    echo "   sensors.db ($(du -h "$WORK/data/sensors.db" | cut -f1))"
fi
for name in nodes.json node_info.json data_from_pico.txt archive; do
    if [ -e "$DATA/$name" ]; then
        cp -a "$DATA/$name" "$WORK/data/"
        echo "   $name ($(du -sh "$DATA/$name" | cut -f1))"
    fi
done

echo "== Secrets"
mkdir -p "$WORK/etc" "$WORK/cloudflared/etc" "$WORK/cloudflared/home"
if sudo test -e /etc/garden-api.env; then
    sudo cp /etc/garden-api.env "$WORK/etc/"
    echo "   /etc/garden-api.env"
fi
if [ -d /etc/cloudflared ]; then
    sudo cp -a /etc/cloudflared/. "$WORK/cloudflared/etc/"
    echo "   /etc/cloudflared/"
fi
if [ -d "$HOME/.cloudflared" ]; then
    cp -a "$HOME/.cloudflared/." "$WORK/cloudflared/home/"
    echo "   ~/.cloudflared/"
fi
# A tunnel installed with `cloudflared service install <token>` keeps its
# token in the unit file rather than in a config.yml, so keep that too.
if [ -f /etc/systemd/system/cloudflared.service ]; then
    sudo cp /etc/systemd/system/cloudflared.service "$WORK/cloudflared/"
    echo "   cloudflared.service"
fi
# Nodes take their clock from the Pi's local time, so a new card must
# use the same time zone.
timedatectl show -p Timezone --value > "$WORK/etc/timezone"
echo "   time zone: $(cat "$WORK/etc/timezone")"

echo "== Writing $OUT"
sudo tar -czf "$OUT" -C "$WORK" .
sudo chown "$(id -un):" "$OUT"
chmod 600 "$OUT"
echo "   $(du -h "$OUT" | cut -f1)"
echo
echo "Copy it to your PC (run this on the PC):"
echo "   scp $(id -un)@$(hostname -I | cut -d' ' -f1):$OUT ."
