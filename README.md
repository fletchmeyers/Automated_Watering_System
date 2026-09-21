A wireless garden monitoring system using a Raspberry Pi and one or more
radio sensor nodes (CircuitPython and/or Arduino) communicating over RFM69.
The Pi polls nodes, logs data to SQLite, and serves it through a Flask API
behind a Cloudflare Tunnel to a live dashboard on GitHub Pages.

# Setup Guide

This is the current setup as actually deployed — rewritten after a full
disaster-recovery rebuild surfaced a lot of drift from the original guide.
If you're setting up a fresh Pi (new SD card, new hardware), follow this
top to bottom. If something here doesn't match reality, it's more likely
this doc that's stale than your memory of how things work — please fix it
inline as you find gaps, the same way this rewrite happened.

---

## Repo layout

```
Automated_Watering_System/
  indoor/          — Pi-side code: main.py, flask_api.py, dashboard, db.py
  garden/          — CircuitPython code for the garden Pico (node 1)
  arduino/         — PlatformIO project for Arduino-based nodes (node 2+)
  deploy/          — systemd unit file templates
  push_data.sh
  index.html, dashboard.js, analysis.js, weather.js, style.css   — dashboard
```

Clone into `~/Automated_Watering_System` on the Pi — every systemd unit,
script, and path reference below assumes this exact location.

## Branches

- **`update_dashboard_data`** is the live branch — this is what GitHub
  Pages actually deploys from, what `push_data.sh` commits
  `data_from_pico.txt` to every 5 minutes via cron, and where the Pi's
  clone should be checked out. **Clone with
  `git clone -b update_dashboard_data <repo-url>`.**
- **`main`** is version-control history only — it does not run anywhere
  and is not what the Pi should be cloned from. New feature branches are
  cut from `update_dashboard_data`, not from `main`.
- **Workflow for new features**: branch off `update_dashboard_data` →
  develop → open a PR back into `update_dashboard_data` → once merged and
  confirmed working live, merge `update_dashboard_data` into `main` (to
  keep history current) → delete the feature branch.
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

Two services. Templates live in `deploy/` in this repo — copy them in,
then edit the placeholders (`<username>`) for your actual Pi username.

```bash
sudo cp deploy/garden-sensor.service /etc/systemd/system/
sudo cp deploy/garden-api.service /etc/systemd/system/
sudo nano /etc/systemd/system/garden-sensor.service   # fix <username>
sudo nano /etc/systemd/system/garden-api.service       # fix <username> + weather keys
sudo systemctl daemon-reload
sudo systemctl enable garden-sensor garden-api
sudo systemctl start garden-sensor garden-api
sudo systemctl status garden-sensor garden-api
```

Watch live output:

```bash
sudo journalctl -u garden-sensor -f
sudo journalctl -u garden-api -f
```

### Weather API keys

`flask_api.py` reads `WEATHERAPI_KEY`, `OPENWEATHERMAP_KEY`,
`TOMORROWIO_KEY` via `os.environ`. Since `garden-api.service` runs under
systemd (not your shell), these **must** be set as `Environment=` lines
inside the unit file itself — see `deploy/garden-api.service`'s
placeholders. A source with no key configured returns a graceful
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

## Part 5: git push access (for `push_data.sh` / cron)

```bash
git remote set-url origin https://<your-token>@github.com/fletchmeyers/Automated_Watering_System.git
```

Generate a token: GitHub → Settings → Developer settings → Personal
access tokens (classic) → `repo` scope. Never commit this URL/token
anywhere.

## Part 6: cron

```bash
crontab -e
```

```
*/5 * * * * /home/<username>/Automated_Watering_System/push_data.sh
```

Retype rather than paste — some editors introduce hidden characters cron
chokes on. Double-check `push_data.sh`'s own internal username/paths match
the current Pi user before relying on it (this has silently gone stale
across at least one migration already).

---

## Disaster recovery

Do these **now**, while a card is known-good — not after the next failure.

1. **Full SD card image** (`Win32DiskImager` or `dd`) of a fully-configured,
   working card. This is the single highest-leverage backup: it captures
   every apt package, both Python environments, the SPI toggle, systemd
   units, cloudflared config, and crontab in one shot. Restoring an image
   is minutes; rebuilding by hand (what this doc replaces) took days.
2. **`sensors.db` backups**, off-box:
   ```bash
   sqlite3 ~/Automated_Watering_System/indoor/sensors.db ".backup /path/sensors_backup.db"
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
