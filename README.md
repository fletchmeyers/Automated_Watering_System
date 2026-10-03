A wireless garden monitoring system using a Raspberry Pi and one or more
radio sensor nodes (CircuitPython and/or Arduino) communicating over RFM69.
The Pi polls nodes, logs data to SQLite, and serves it through a Flask API
behind a Cloudflare Tunnel to a live dashboard on GitHub Pages.

# Setup Guide

This is the current setup as actually deployed — rewritten after a full
disaster-recovery rebuild surfaced a lot of drift from the original guide.
If you're setting up a fresh Pi (new SD card, new hardware), use the
Quick setup below; Parts 1–4 document what it does, step by step. If something here doesn't match reality, it's more likely
this doc that's stale than your memory of how things work — please fix it
inline as you find gaps, the same way this rewrite happened.

---

## Repo layout

```
Automated_Watering_System/
  raspberrypi/     — Pi-side code: main.py, flask_api.py, db.py; the Pi's
                     sensors.db and logs also live here (gitignored)
  dashboard/       — the web dashboard (index.html, JS, CSS) served by GitHub Pages
  circuitpython/   — the garden Pico (node 1), laid out like its CIRCUITPY
                     drive: code.py and modules at the top, libraries in lib/
  arduino/         — PlatformIO project for Arduino-based nodes (node 2+)
  deploy/          — systemd unit templates and install.sh
  tests/           — software_tests (pytest) and hardware_tests per board
  index.html       — redirects GitHub Pages visitors to dashboard/index.html
```

To update the Pico, copy the contents of `circuitpython/` onto its
CIRCUITPY drive (only the files that changed, usually not `lib/`).

Clone into `~/Automated_Watering_System` on the Pi — every systemd unit,
script, and path reference below assumes this exact location.

## Branches

- **`main`** is the only long-lived branch. GitHub Pages serves the
  dashboard from it, and the Pi's clone tracks it (`git clone <repo-url>`).
- **Sensor data never goes into git.** It lives on the Pi — `raspberrypi/sensors.db`,
  plus the `raspberrypi/data_from_pico.txt` and `raspberrypi/archive/` text logs, all
  gitignored — and reaches the dashboard live through the Flask API.
  (Until October 2026 a cron job pushed `data_from_pico.txt` to a separate
  `update_dashboard_data` branch every 5 minutes; that branch and
  `push_data.sh` are retired.)
- **Workflow for new features**: branch off `main` → develop → open a PR
  into `main` → on the Pi, `git pull` and restart whichever service changed
  (`garden-sensor` and/or `garden-api`; if `deploy/` changed, run
  `deploy/install.sh` instead) → confirm it works → delete the
  feature branch. To try a branch on the Pi before merging it, check it out
  there (`git fetch && git checkout <branch>`), then switch back to `main`
  once it's merged.
- **Before deleting a branch on GitHub**, switch your local repo off it
  (`git checkout main && git pull`), then delete the local copy too
  (`git branch -d <branch>`).
- If GitHub Pages' configured source branch (Settings → Pages) ever
  changes, update this section to match — that setting is the actual
  source of truth, this doc is just documentation of it.

## Node IDs

| Node | Hardware | Framework |
|---|---|---|
| 1 | Garden Pico (Pico 2W, solar/car-battery powered) | CircuitPython |
| 2 | Feather M0 | Arduino (PlatformIO) |
| 3+ | reserved for future Arduino boards (Pico, ESP32, etc.) | Arduino |

All nodes share one 915MHz frequency and encryption key — see
`hardware_setup_indoor.py` / `hardware_setup_garden.py` / each
`board_config_*.h`. The key must match exactly across every node and the Pi.

---

## Quick setup (a fresh card)

Works on a Pi 3B or a Pi Zero 2 W. `deploy/setup.sh` does Parts 1–4.

1. **Flash the card** with Raspberry Pi Imager: *Raspberry Pi OS Lite
   (64-bit)*. In its settings (the gear / "Edit settings"), set the
   hostname, username and password, Wi-Fi, time zone, and turn on SSH.
2. **Copy a backup over**, if you have one (see Disaster recovery). From
   the PC, once the new Pi is on the network:
   ```bash
   scp garden_backup_<date>.tar.gz <user>@<new-pi-ip>:~
   ```
3. **On the new Pi:**
   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/fletchmeyers/Automated_Watering_System.git ~/Automated_Watering_System
   ~/Automated_Watering_System/deploy/setup.sh --restore ~/garden_backup_<date>.tar.gz --wittypi
   sudo reboot
   ```
   Leave out `--restore` for a brand-new hub with no data or tunnel yet
   (then set up the tunnel per Part 4), and `--wittypi` if there's no
   Witty Pi HAT. The script is safe to re-run if it stops partway.

**Only run one hub at a time.** Two Pis polling the same nodes would
collide on the radio, and two copies of one Cloudflare tunnel would split
the dashboard's requests between them.

## Part 1: OS-level dependencies (not in git — install fresh every time)

None of this is captured by cloning the repo. Do this before anything else
on a fresh Pi:

```bash
sudo apt update
sudo apt install -y swig liblgpio-dev sqlite3
```

**Enable SPI** (required for the radio; off by default on a fresh OS image):

```bash
sudo raspi-config
```

Interface Options → SPI → Yes → reboot.

Confirm it took effect:

```bash
ls -la /dev/spidev0.0
```

## Part 2: Two separate Python environments

These are genuinely separate on purpose — one needs hardware access, the
other doesn't, and mixing them causes `pip install --user` errors (a venv
hides user site-packages).

### 2.1 Radio/hardware venv (for `main.py`)

```bash
python3 -m venv ~/env
source ~/env/bin/activate
pip install adafruit-circuitpython-rfm69
```

Verify:

```bash
~/env/bin/python3 -c "import board; print('ok')"
```

### 2.2 Flask API (system Python, no venv)

```bash
deactivate   # if you're still in the venv from above
pip install --user flask flask-cors requests gunicorn --break-system-packages
```

`gunicorn`/`flask` land in `~/.local/bin/` — not on `PATH` by default, so
systemd units below reference full paths rather than bare commands.

## Part 3: systemd services

Two services, `garden-sensor` (`main.py`, the radio loop) and `garden-api`
(the Flask API under gunicorn). Templates live in `deploy/`; `install.sh`
fills in your username, installs and enables them, and (re)starts both.
Run it as your normal user — it calls `sudo` itself:

```bash
~/Automated_Watering_System/deploy/install.sh
```

It's safe to re-run after any `git pull` that touches `deploy/`. It also
moves the Pi's data out of the old `indoor/` folder into `raspberrypi/`
if it finds any there (the October 2026 folder rename).

Watch live output:

```bash
sudo journalctl -u garden-sensor -f
sudo journalctl -u garden-api -f
```

### Weather API keys

`flask_api.py` reads `WEATHERAPI_KEY`, `OPENWEATHERMAP_KEY`,
`TOMORROWIO_KEY` via `os.environ`. Since `garden-api.service` runs under
systemd (not your shell), they live in `/etc/garden-api.env` (readable
only by root, never in git), which the unit loads with `EnvironmentFile=`.
`install.sh` creates it the first time, copying any keys from an older
unit file that had them inline. To edit keys:

```bash
sudo nano /etc/garden-api.env       # one KEY=value per line
sudo systemctl restart garden-api
```

A source with no key configured returns a graceful
"not configured" response rather than erroring, so only set the ones you
actually have keys for.

## Part 4: Cloudflare Tunnel

Not installed by default, and its config lives outside git entirely. This
is the one thing where **losing the credentials file means starting over**
with a brand-new tunnel (new UUID) and re-pointing DNS — so it's worth
backing this up somewhere off the Pi (see Disaster Recovery below).

If restoring an existing tunnel (you have the credentials backed up):

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb
sudo mkdir -p /etc/cloudflared
# copy in: <uuid>.json, cert.pem, config.yml
sudo chmod 600 /etc/cloudflared/<uuid>.json
```

Check `config.yml`'s `credentials-file:` path matches where you actually
put the `.json` — this is the #1 thing that breaks on a restore.

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

If starting fully fresh (no backup — creates a **new** tunnel identity):

```bash
cloudflared tunnel login
cloudflared tunnel create garden-api
cloudflared tunnel route dns garden-api api.fletchermeyers.com
```

Then write `config.yml` pointing `service: http://127.0.0.1:5000` at the
Flask API, and proceed with `service install` as above.

Verify end to end:

```bash
curl https://api.fletchermeyers.com/api/health
```

## Part 5: (no cron job or GitHub token needed)

Earlier versions of this guide set up a GitHub push token and a cron job
running `push_data.sh` every 5 minutes. Both are retired: the dashboard
reads data live from the API, and nothing on the Pi pushes to GitHub. The
Pi only needs read access to `git pull`, which a public repo doesn't
require credentials for.

---

## Disaster recovery

Do these **now**, while a card is known-good — not after the next failure.

0. **Backup bundle** — everything on the Pi that isn't in git (the
   database and logs, `node_info.json`, the weather API keys, the
   Cloudflare tunnel credentials and the time zone), in one file that
   `setup.sh --restore` puts back:
   ```bash
   ~/Automated_Watering_System/deploy/backup.sh          # on the Pi
   scp <user>@<pi-ip>:~/garden_backup_<date>.tar.gz .    # on the PC
   ```
   Add `--stop` when moving to a new card: it stops the services first
   (and leaves them stopped), so no readings arrive after the snapshot.
   The bundle contains secrets — keep it off GitHub and other public places.
1. **Full SD card image** of a fully-configured, working card: shut the
   Pi down, put its card in the PC, and in Win32DiskImager pick a file
   name, tick *Read Only Allocated Partitions*, and click *Read*. Restore
   by writing the `.img` to a card of the same size or larger. This is the single highest-leverage backup: it captures
   every apt package, both Python environments, the SPI toggle, systemd
   units, `/etc/garden-api.env`, and cloudflared config in one shot. Restoring an image
   is minutes; rebuilding by hand (what this doc replaces) took days.
2. **`sensors.db` backups**, off-box:
   ```bash
   sqlite3 ~/Automated_Watering_System/raspberrypi/sensors.db ".backup /path/sensors_backup.db"
   ```
   Never copy `sensors.db` directly while `garden-sensor` is running — it's
   a live WAL-mode database; `.backup` gives a consistent snapshot, a raw
   copy can grab an inconsistent one.
3. **Cloudflare Tunnel credentials** (`/etc/cloudflared/*.json`,
   `cert.pem`, `config.yml`) — back these up somewhere off the Pi. There's
   no way to regenerate the same tunnel identity if lost; only a new one.

## Notes

- **Radio range**: 915MHz RFM69 has limited range, especially through
  walls or with RF interference nearby (construction equipment has
  measurably degraded link quality here before). A struggling link shows
  up as scattered `"ts": "unknown"` packets and low ping-test hit rates —
  this is signal quality, not a bug, and clears up on its own once
  conditions improve.
- **Two data stores**: `data_from_pico.txt` (flat file, used by
  `sensor_health_report()`/`/api/health`) and `sensors.db` (SQLite, used
  by `/api/data` and the actual dashboard) are separate and can drift out
  of sync — a healthy `/api/health` does not guarantee the dashboard has
  data.
- **`archive/`**: intentionally never trimmed/rotated. Not tracked in git
  (would grow the repo unboundedly) — back it up separately if wanted.
- **`__pycache__/` directories**: auto-generated compiled bytecode, safe
  to delete anytime (`find . -name "__pycache__" -type d -exec rm -rf {} +`)
  — they regenerate automatically and should be in `.gitignore`, not
  committed.
