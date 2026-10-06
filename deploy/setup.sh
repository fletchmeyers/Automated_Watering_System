#!/bin/sh
# deploy/setup.sh — turn a freshly flashed Raspberry Pi OS card into the hub.
#
# Works on a Pi 3B or a Pi Zero 2 W (or anything else with the 40-pin
# header) running Raspberry Pi OS Lite, 64-bit. Set the hostname, user,
# Wi-Fi, SSH and time zone in Raspberry Pi Imager's settings before writing
# the card, then on the new Pi:
#
#     sudo apt update && sudo apt install -y git
#     git clone https://github.com/fletchmeyers/Automated_Watering_System.git ~/Automated_Watering_System
#     ~/Automated_Watering_System/deploy/setup.sh --restore ~/garden_backup_<date>.tar.gz
#
# Options:
#     --restore FILE   restore a bundle made by deploy/backup.sh: the data,
#                      weather API keys, Cloudflare tunnel and time zone
#     --wittypi        also install the Witty Pi 4 software
#     --platformio     also install PlatformIO, so raspberrypi/node_setup.py can
#                      build and flash Arduino nodes from the Pi
#
# Safe to run again if it stops partway. Reboot when it finishes.
set -e

RESTORE=""
WITTYPI=0
PLATFORMIO=0
while [ $# -gt 0 ]; do
    case "$1" in
        --restore) RESTORE="$2"; shift 2 ;;
        --wittypi) WITTYPI=1; shift ;;
        --platformio) PLATFORMIO=1; shift ;;
        *) echo "Unknown option: $1"; sed -n 2,20p "$0"; exit 1 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo; it calls sudo itself."
    exit 1
fi

USER_NAME=$(id -un)
REPO=$(cd "$(dirname "$0")/.." && pwd)
if [ "$REPO" != "$HOME/Automated_Watering_System" ]; then
    echo "Clone the repo to ~/Automated_Watering_System first (it's at $REPO)."
    exit 1
fi
if [ -n "$RESTORE" ] && [ ! -f "$RESTORE" ]; then
    echo "Backup file not found: $RESTORE"
    exit 1
fi

step() { echo; echo "== $*"; }

# ── 1. Board check ────────────────────────────────────────────────────────────
step "Board"
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
ARCH=$(dpkg --print-architecture)
echo "   $MODEL, ${MEM_MB} MB RAM, $ARCH"
if [ "$ARCH" != "arm64" ]; then
    echo "   !! This is a 32-bit OS. Use Raspberry Pi OS Lite (64-bit) instead."
    exit 1
fi
if [ "$MEM_MB" -lt 768 ] && [ "$(systemctl get-default)" = graphical.target ]; then
    echo "   !! This board has ${MEM_MB} MB and boots to a desktop. Raspberry Pi OS Lite"
    echo "      leaves much more room for the hub; consider reflashing with Lite."
fi

# ── 2. System packages, SPI and I2C ──────────────────────────────────────────
step "System packages"
sudo apt-get update -q
sudo apt-get install -y -q git sqlite3 swig liblgpio-dev python3-dev python3-venv python3-pip i2c-tools exfatprogs python3-serial tmux

step "Interfaces"
# 0 means "enable" for raspi-config's non-interactive mode. SPI is for the
# radio, I2C for the Witty Pi.
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
echo "   SPI and I2C enabled (active after the reboot at the end)"

# ── 3. Python environments ───────────────────────────────────────────────────
step "Radio venv (~/env, for main.py)"
[ -d "$HOME/env" ] || python3 -m venv "$HOME/env"
"$HOME/env/bin/pip" install -q --upgrade pip
"$HOME/env/bin/pip" install -q adafruit-circuitpython-rfm69
"$HOME/env/bin/python3" -c "import adafruit_rfm69" && echo "   adafruit_rfm69 imports ok"

step "Flask API packages (system Python, ~/.local)"
pip install -q --user flask flask-cors requests gunicorn --break-system-packages
[ -x "$HOME/.local/bin/gunicorn" ] && echo "   gunicorn installed"

# ── 4. Restore a backup ──────────────────────────────────────────────────────
CF_DIR=""
if [ -n "$RESTORE" ]; then
    step "Restoring $RESTORE"
    sudo systemctl stop garden-sensor garden-api garden-wifi 2>/dev/null || true
    BUNDLE=$(mktemp -d)
    trap 'sudo rm -rf "$BUNDLE"' EXIT
    sudo tar -xzf "$RESTORE" -C "$BUNDLE"
    sudo chown -R "$USER_NAME:" "$BUNDLE"

    DATA="$REPO/raspberrypi"
    for name in sensors.db node_info.json data_from_pico.txt archive; do
        [ -e "$BUNDLE/data/$name" ] || continue
        if [ -e "$DATA/$name" ]; then
            echo "   $name already exists here, keeping it (delete it and re-run to restore)"
        else
            cp -a "$BUNDLE/data/$name" "$DATA/"
            echo "   $name"
        fi
    done

    if [ -f "$BUNDLE/etc/garden-api.env" ]; then
        sudo install -m 600 -o root -g root "$BUNDLE/etc/garden-api.env" /etc/garden-api.env
        echo "   /etc/garden-api.env"
    fi

    if [ -s "$BUNDLE/etc/timezone" ]; then
        TZ_NAME=$(cat "$BUNDLE/etc/timezone")
        sudo timedatectl set-timezone "$TZ_NAME"
        echo "   time zone set to $TZ_NAME"
    fi

    CF_DIR="$BUNDLE/cloudflared"
fi

# ── 5. Garden services ───────────────────────────────────────────────────────
step "Garden services"
"$REPO/deploy/install.sh"

# ── 6. Cloudflare tunnel ─────────────────────────────────────────────────────
step "Cloudflare tunnel"
HAVE_CONFIG=0
HAVE_UNIT=0
if [ -n "$CF_DIR" ]; then
    if [ -n "$(ls -A "$CF_DIR/etc" 2>/dev/null)" ]; then
        sudo mkdir -p /etc/cloudflared
        sudo cp -r "$CF_DIR/etc/." /etc/cloudflared/
        sudo chown -R root:root /etc/cloudflared
    fi
    [ -n "$(ls -A "$CF_DIR/home" 2>/dev/null)" ] && { mkdir -p "$HOME/.cloudflared"; cp -a "$CF_DIR/home/." "$HOME/.cloudflared/"; }
    # The service reads /etc/cloudflared/config.yml; older setups kept it in
    # the home folder.
    if [ ! -f /etc/cloudflared/config.yml ] && [ -f "$HOME/.cloudflared/config.yml" ]; then
        sudo mkdir -p /etc/cloudflared
        sudo cp "$HOME/.cloudflared/config.yml" /etc/cloudflared/config.yml
    fi
    [ -f /etc/cloudflared/config.yml ] && HAVE_CONFIG=1
    [ -f "$CF_DIR/cloudflared.service" ] && HAVE_UNIT=1
fi

if [ $HAVE_CONFIG = 1 ] || [ $HAVE_UNIT = 1 ]; then
    if ! command -v cloudflared >/dev/null; then
        curl -fsSL -o /tmp/cloudflared.deb \
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb"
        sudo dpkg -i /tmp/cloudflared.deb
        rm -f /tmp/cloudflared.deb
    fi
    echo "   $(cloudflared --version)"

    if [ $HAVE_CONFIG = 1 ]; then
        # The credentials path in config.yml names the old user's home
        # folder if it pointed there; move the file to /etc/cloudflared and
        # point config.yml at it, so the new username doesn't matter.
        CRED=$(sudo sed -n 's/^credentials-file:[[:space:]]*//p' /etc/cloudflared/config.yml | tr -d "\"'")
        if [ -n "$CRED" ] && ! sudo test -f "$CRED"; then
            NAME=$(basename "$CRED")
            for src in "/etc/cloudflared/$NAME" "$HOME/.cloudflared/$NAME"; do
                if sudo test -f "$src"; then
                    [ "$src" = "/etc/cloudflared/$NAME" ] || sudo cp "$src" "/etc/cloudflared/$NAME"
                    sudo sed -i "s|^credentials-file:.*|credentials-file: /etc/cloudflared/$NAME|" /etc/cloudflared/config.yml
                    echo "   credentials-file now /etc/cloudflared/$NAME"
                    break
                fi
            done
        fi
        sudo chmod 600 /etc/cloudflared/*.json 2>/dev/null || true
        if [ ! -f /etc/systemd/system/cloudflared.service ]; then
            sudo cloudflared service install
        fi
    else
        # Token-based tunnel: the token is in the old unit file itself.
        sudo install -m 644 "$CF_DIR/cloudflared.service" /etc/systemd/system/cloudflared.service
        sudo systemctl daemon-reload
    fi
    sudo systemctl enable cloudflared >/dev/null 2>&1
    sudo systemctl restart cloudflared
    echo "   cloudflared: $(systemctl is-active cloudflared)"
else
    echo "   No tunnel config to restore; skipped. See README Part 4 to set one up."
fi

# ── 7. Witty Pi (optional) ───────────────────────────────────────────────────
if [ $WITTYPI = 1 ]; then
    step "Witty Pi 4 software"
    if [ -d "$HOME/wittypi" ]; then
        echo "   already installed (~/wittypi)"
    else
        (cd "$HOME" && curl -fsSL -o install.sh https://www.uugear.com/repo/WittyPi4/install.sh \
            && sudo sh install.sh && rm -f install.sh)
    fi
fi

# ── 8. PlatformIO, for flashing Arduino nodes (optional) ─────────────────────
if [ $PLATFORMIO = 1 ]; then
    step "PlatformIO"
    if [ -x "$HOME/.platformio/penv/bin/pio" ]; then
        echo "   already installed (~/.platformio)"
    else
        curl -fsSL -o /tmp/get-platformio.py \
            https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py
        python3 /tmp/get-platformio.py
        rm -f /tmp/get-platformio.py
    fi
    # Uploading needs the board's serial port, which belongs to "dialout".
    sudo usermod -aG dialout "$USER_NAME"
    # Fetch every board's platform, compiler and libraries now (a long
    # download, especially RadioHead's), so a first node_setup.py for any
    # board only has to compile. Safe to run again: it skips what's there.
    echo "   Downloading compilers and libraries for every board (can take 20+ minutes)..."
    "$HOME/.platformio/penv/bin/pio" pkg install -d "$REPO/arduino"
    echo "   pio: $HOME/.platformio/penv/bin/pio"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
step "Done"
echo "   Time zone: $(timedatectl show -p Timezone --value)"
echo "   Now reboot:  sudo reboot"
echo "   Then check:  ls /dev/spidev0.0"
echo "                sudo journalctl -u garden-sensor -f"
echo "                curl https://api.fletchermeyers.com/api/health"
