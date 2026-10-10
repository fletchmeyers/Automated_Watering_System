#!/bin/sh
# deploy/backup_db.sh — copy sensors.db and the node list (nodes.json) to
# the backup USB stick.
#
# Run nightly as root by garden-backup.timer (see deploy/install.sh). To run
# one now and see the result:
#     sudo systemctl start garden-backup
#     sudo journalctl -u garden-backup -n 20 --no-pager
#
# The stick must have the volume label GARDENBAK (exFAT, so the PC can read
# it too). It's mounted only while the backup is written, so a power cut
# the rest of the time can't corrupt it.
#
# Keeps the last KEEP_DAILY nightly copies, plus the copy from the 1st of
# each month for KEEP_MONTHLY_DAYS days.
set -eu

GARDEN_USER=${GARDEN_USER:?set GARDEN_USER to the Pi user that owns the repo}
DB="/home/$GARDEN_USER/Automated_Watering_System/raspberrypi/sensors.db"
LABEL=GARDENBAK
DEVICE="/dev/disk/by-label/$LABEL"
MOUNT=/mnt/garden_backup
DEST="$MOUNT/garden_db"
KEEP_DAILY=14
KEEP_MONTHLY_DAYS=400

TODAY=$(date +%Y-%m-%d)
# /var/tmp is on the SD card; /tmp may be RAM, too small for a big database.
SNAP="/var/tmp/garden_backup_$TODAY.db"
WE_MOUNTED=0

cleanup() {
    rm -f "$SNAP" "$SNAP.gz"
    if [ $WE_MOUNTED = 1 ]; then
        sync
        umount "$MOUNT" || echo "WARNING: could not unmount $MOUNT"
    fi
}
trap cleanup EXIT

if [ ! -e "$DEVICE" ]; then
    echo "ERROR: no USB stick labelled $LABEL is plugged in; nothing backed up."
    exit 1
fi
if [ ! -f "$DB" ]; then
    echo "ERROR: $DB not found."
    exit 1
fi

# Snapshot as the repo's user, not root: if root opened the live database
# and SQLite had to create its -wal/-shm files, they'd be owned by root and
# main.py could no longer write. .backup is consistent even mid-write.
runuser -u "$GARDEN_USER" -- sqlite3 "$DB" ".backup '$SNAP'"
CHECK=$(sqlite3 "$SNAP" "PRAGMA quick_check;")
if [ "$CHECK" != "ok" ]; then
    echo "ERROR: the snapshot failed its integrity check: $CHECK"
    exit 1
fi
ROWS=$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM readings;" 2>/dev/null || echo "?")
gzip -6 "$SNAP"

mkdir -p "$MOUNT"
if ! mountpoint -q "$MOUNT"; then
    mount "$DEVICE" "$MOUNT"
    WE_MOUNTED=1
fi
mkdir -p "$DEST"

NEED_KB=$(du -k "$SNAP.gz" | cut -f1)
FREE_KB=$(df -Pk "$MOUNT" | awk 'NR==2 {print $4}')
if [ "$FREE_KB" -lt $((NEED_KB * 2)) ]; then
    echo "ERROR: stick nearly full (${FREE_KB} KB free, backup is ${NEED_KB} KB)."
    exit 1
fi

# Write under a temporary name and rename, so a half-written file never
# looks like a finished backup.
cp "$SNAP.gz" "$DEST/.sensors_$TODAY.db.gz.part"
mv "$DEST/.sensors_$TODAY.db.gz.part" "$DEST/sensors_$TODAY.db.gz"
echo "Backed up $ROWS readings to $DEST/sensors_$TODAY.db.gz ($(du -h "$DEST/sensors_$TODAY.db.gz" | cut -f1))"

# The node list too (tiny): a new card needs it to know its nodes.
NODES="$(dirname "$DB")/nodes.json"
if [ -f "$NODES" ]; then
    cp "$NODES" "$DEST/.nodes_$TODAY.json.part"
    mv "$DEST/.nodes_$TODAY.json.part" "$DEST/nodes_$TODAY.json"
    echo "Backed up the node list to $DEST/nodes_$TODAY.json"
fi

# Retention (the same for both kinds of file).
NOW=$(date +%s)
for f in "$DEST"/sensors_????-??-??.db.gz "$DEST"/nodes_????-??-??.json; do
    [ -e "$f" ] || continue
    d=$(basename "$f" | sed 's/^[a-z]*_\(....-..-..\)\..*$/\1/')
    age=$(( (NOW - $(date -d "$d" +%s)) / 86400 ))
    if [ "$age" -lt $KEEP_DAILY ]; then
        continue
    fi
    if [ "${d#*-??-}" = "01" ] && [ "$age" -lt $KEEP_MONTHLY_DAYS ]; then
        continue
    fi
    rm -f "$f"
    echo "Removed old backup $(basename "$f")"
done
rm -f "$DEST"/.sensors_*.part "$DEST"/.nodes_*.part

COUNT=$(ls "$DEST"/sensors_*.db.gz | wc -l)
echo "$COUNT backups on the stick, $(df -Ph "$MOUNT" | awk 'NR==2 {print $4}') free"
