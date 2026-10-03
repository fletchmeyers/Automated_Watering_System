#!/bin/sh
# deploy/install.sh — install or update the Pi's systemd services.
#
# Run on the Pi as the normal user (not with sudo), from anywhere:
#     ~/Automated_Watering_System/deploy/install.sh
#
# Safe to run again after any `git pull`. It:
#   1. stops garden-sensor and garden-api
#   2. moves the Pi's untracked data (sensors.db, node_info.json, archive/,
#      data_from_pico.txt) from the old indoor/ folder into raspberrypi/,
#      if it is still there
#   3. creates /etc/garden-api.env for the weather API keys if it doesn't
#      exist, copying any keys already in the installed garden-api.service
#   4. fills <username> into the deploy/ unit templates, installs them,
#      starts both services, and enables the nightly USB backup timer
set -e

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo; it calls sudo itself."
    exit 1
fi

USER_NAME=$(id -un)
REPO=$(cd "$(dirname "$0")/.." && pwd)
SERVICES="garden-sensor garden-api"
# Installed and enabled, but not started: the timer starts the service.
BACKUP_UNITS="garden-backup.service garden-backup.timer"
ENV_FILE=/etc/garden-api.env
OLD_API_UNIT=/etc/systemd/system/garden-api.service

if [ "$REPO" != "/home/$USER_NAME/Automated_Watering_System" ]; then
    echo "The service templates assume the repo is at /home/$USER_NAME/Automated_Watering_System,"
    echo "but it is at $REPO. Move it there or edit deploy/*.service first."
    exit 1
fi

echo "== Stopping services"
sudo systemctl stop $SERVICES 2>/dev/null || true

echo "== Data files"
OLD="$REPO/indoor"
NEW="$REPO/raspberrypi"
for name in sensors.db sensors.db-wal sensors.db-shm node_info.json data_from_pico.txt archive; do
    if [ -e "$OLD/$name" ]; then
        if [ -e "$NEW/$name" ]; then
            echo "   !! both $OLD/$name and $NEW/$name exist; leaving both alone, sort this out by hand"
        else
            mv "$OLD/$name" "$NEW/$name"
            echo "   moved indoor/$name -> raspberrypi/$name"
        fi
    fi
done
if [ -d "$OLD" ]; then
    rm -rf "$OLD/__pycache__" "$OLD/analysis/__pycache__"
    rmdir "$OLD/analysis" 2>/dev/null || true
    rmdir "$OLD" 2>/dev/null && echo "   removed the empty indoor/ folder" \
        || echo "   indoor/ still has files in it; check them: ls -la $OLD"
fi
[ -e "$NEW/sensors.db" ] && echo "   sensors.db is in raspberrypi/" \
    || echo "   no sensors.db yet; main.py will create one"

echo "== Weather API keys ($ENV_FILE)"
if sudo test -e "$ENV_FILE"; then
    echo "   already exists, leaving it alone"
else
    TMP=$(mktemp)
    # Handles both Environment=KEY=value and Environment="KEY=value".
    if [ -f "$OLD_API_UNIT" ] && grep -q '^Environment="\{0,1\}[A-Z_]*_KEY=' "$OLD_API_UNIT"; then
        grep '^Environment="\{0,1\}[A-Z_]*_KEY=' "$OLD_API_UNIT" \
            | sed 's/^Environment=//; s/^"\(.*\)"$/\1/' > "$TMP"
        echo "   copied $(wc -l < "$TMP") key(s) from the installed garden-api.service"
    else
        printf 'WEATHERAPI_KEY=\nOPENWEATHERMAP_KEY=\nTOMORROWIO_KEY=\n' > "$TMP"
        echo "   created with empty keys; fill them in with: sudo nano $ENV_FILE"
    fi
    sudo install -m 600 -o root -g root "$TMP" "$ENV_FILE"
    rm -f "$TMP"
fi

echo "== Installing services"
for unit in $(for s in $SERVICES; do echo $s.service; done) $BACKUP_UNITS; do
    sed "s/<username>/$USER_NAME/g" "$REPO/deploy/$unit" > /tmp/$unit
    sudo install -m 644 /tmp/$unit /etc/systemd/system/$unit
    rm -f /tmp/$unit
    echo "   /etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable $SERVICES >/dev/null 2>&1
sudo systemctl restart $SERVICES
sudo systemctl enable --now garden-backup.timer >/dev/null 2>&1

sleep 3
echo "== Status"
for svc in $SERVICES; do
    echo "   $svc: $(systemctl is-active $svc)"
done
echo "   nightly USB backup: $(systemctl is-active garden-backup.timer)"
if [ ! -e /dev/disk/by-label/GARDENBAK ]; then
    echo "   (no USB stick labelled GARDENBAK plugged in yet, so backups will fail until one is)"
fi
echo "Follow the logs with: sudo journalctl -u garden-sensor -f"
