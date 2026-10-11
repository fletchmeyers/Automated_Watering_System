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
| 3+ | any board listed in `nodes.json` (see "Adding or updating a node") | CircuitPython or Arduino |

All nodes share one 915MHz frequency and encryption key — see
`hardware_setup_indoor.py` / `hardware_setup_garden.py` / each
`board_config_*.h`. The key must match exactly across every node and the Pi.

---

## Quick setup (a fresh card)

Works on a Pi 3B or a Pi Zero 2 W. `deploy/setup.sh` does Parts 1–4.

1. **Flash the card** with Raspberry Pi Imager: *Raspberry Pi OS Lite
   (64-bit)*, found under *Raspberry Pi OS (other)*. Lite is the same OS
   without the desktop, which a headless hub doesn't need (the desktop
   costs 200–300 MB of RAM; too much for a Zero 2 W's 512 MB). The full
   OS also works on a Pi 3B; run `sudo raspi-config nonint
   do_boot_behaviour B1` after setup so it boots to the console. In the
   Imager's settings (the gear / "Edit settings"), set the
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

## Adding or updating a node

Every node's settings live in the Pi's node list,
**`raspberrypi/nodes.json`**: name, framework (`circuitpython` or
`arduino`), board, Wi-Fi or radio, sense and log intervals, storage (`sd`,
`flash` for Arduino SAMD boards, or `none`), pins, and an optional
`sleep_window`. It's the Pi's own file, not in git (so adding a node never
needs a commit), and it's in both backups (`deploy/backup.sh` and the
nightly USB stick). A fresh Pi starts it from `nodes.example.json`, the two
garden nodes. `main.py` takes its node list from it (restart
`garden-sensor` after changing it), `wifi_nodes.py` which nodes may connect
over Wi-Fi, the dashboard names and colors (through `/api/nodes`), and
`node_setup.py` builds each node's settings from it. Sensors aren't
listed: both firmwares look for every sensor they know at boot and use
whichever answer.

**Adding a node** — plug the board into the Pi and run:

```bash
cd ~/Automated_Watering_System/raspberrypi
python3 node_setup.py add
```

It asks about the node, with a default for each question (press Enter to
take it): board (skipped when it can tell from what's plugged in),
framework, Wi-Fi or radio, node ID (the lowest one that's free and has no
old readings in the database), name, where it logs, pins (the board's usual
wiring, or your own), how often it reads its sensors, how often it logs
(only with somewhere to log to), RTC and sleep window. Type `b` to go back
a question. At the end it lists every answer with a number: type one to
change it, or press Enter to save. Then it offers to set the board up as
the new node.

**The hardware check** — after setting a board up (new or updated),
`node_setup.py` reads its sensors a few times over USB and lists every one
that answered, with a `!` under any whose readings look wrong: out of
range, a soil sensor at the top of its range or reading as if in air,
readings jumping about (often a noisy USB charger), or a sensor that
stopped answering. A soil sensor that isn't calibrated yet can then take
its dry (or wet) reading on the spot, from about 30 s of live readings
(see "Calibrating soil sensors"). Run the check on its own, with the node
plugged in, with `python3 node_setup.py check`. To check another kind of
sensor, add a row to `CHECKS` in `raspberrypi/sensor_check.py`.

Plain `python3 node_setup.py`
offers the same questions for a board that isn't a node yet. To take a
node off the list (its readings stay): `python3 node_setup.py remove 3`.
Anything the questions don't cover (a `battery` label for the Nodes card,
a different `color`) can be edited in `nodes.json` with `nano`; it's
checked every time it's read.

To update a node to the latest code, plug it into the Pi by USB and run:

```bash
cd ~/Automated_Watering_System/raspberrypi
python3 node_setup.py --dry-run     # see what would change first
python3 node_setup.py               # update it
python3 node_setup.py list          # the nodes in the list
```

It works out which node is plugged in and what it runs. A CircuitPython
board gets the changed files from `circuitpython/` plus the libraries they
use, and a `node_config.py` written from `nodes.json` (and is restarted
from its console if `boot.py` changed). An Arduino board is built with its
settings and flashed. The library `.mpy` files in
`circuitpython/lib` must match the board's CircuitPython major version.

**Switching a board's framework, or giving it another node's ID** — change
`"framework"` in `nodes.json` (`circuitpython` or `arduino`) if needed,
plug it in, and:

```bash
python3 node_setup.py --node 3
```

`nodes.json` says what the node should be, and the script gets the board
there from whatever it runs now. A board without CircuitPython gets
CircuitPython (the version in `nodes.py`, downloaded for its board from
circuitpython.org) before the node code; an Arduino node on a Pico or
Feather RP2040 gets its firmware built with the node's settings and copied
to the board's bootloader drive; the Feather M0 is uploaded over serial.
To reach the bootloader, a board running CircuitPython is restarted from
its console and one running Arduino with a "1200-baud touch". A blank Pico
shows its bootloader drive by itself; otherwise hold BOOTSEL (Pico) or BOOT
(Feather RP2040) as you plug it in.

Boards (`"board"` in `nodes.json`): `pico`, `picow`, `pico2`, `pico2w` and
`feather_rp2040_adalogger` run either framework; `feather_m0`,
`feather_esp32s2` (ESP32-S2 Feather) and `feather_esp32_v2` (ESP32 Feather
V2) are Arduino only. The M0 and ESP32 boards are uploaded over their
serial port: the V2's USB-serial chip resets it for the upload; if the
ESP32-S2 doesn't take the upload (e.g. its factory program ignores the
reset), hold BOOT, tap RESET, release BOOT, and run the script again. On
the RP2 and ESP32 boards the radio uses the board's default SPI pins unless
`spi_sck`/`spi_mosi`/`spi_miso` say otherwise; an SD card on its own bus
(the Adalogger's slot: `sd_sck` 18, `sd_mosi` 19, `sd_miso` 20, `sd_cs` 23)
goes on the RP2 boards' second one. Arduino nodes other than the M0 have no
RTC unless `"rtc": "pcf8523"` is set: their clock is set by the Pi's polls
and nothing is logged after a restart until the first poll, and a sleep
command turns the radio off but keeps the chip awake. A board with
no radio, no I2C sensors or no RTC still runs (USB only), which makes a
bare board easy to test on the bench.

**Wi-Fi, two links, sleep and battery saver** — see
[How nodes reach the Pi](#how-nodes-reach-the-pi-links-sleep-and-battery-saver)
below for every option and command.

**Calibrating soil sensors** — out of the box the dashboard shows every
soil sensor on one fixed scale (raw 200 = 0%, 1023 = 100%), which suits
few pots. Calibrate a sensor with two readings in its own soil (not air or
a jar of water: soil reads well inside that range, and differently for
different soils): "dry" while the plant is due for watering, and "wet"
30-60 minutes after watering.

The Pi does it by itself for a soil sensor it hasn't seen before (on a new
node, or a new seesaw on an existing one): put the sensor in dry soil,
wait a few minutes, then water it thoroughly. `garden-api` checks every 5
minutes; it sees the watering as a jump in the readings, takes dry from
just before it and wet from 30-60 minutes after it, and saves both. Until
then the dashboard shows that sensor's gauge in purple with its raw reading
and what it's waiting for ("water it", or "done ~14:20"). Start it for a
sensor by hand (after swapping one, say) with
`python3 node_setup.py calibrate 3 s1 auto`; sensors that were already
there when this first ran are left alone. Or set either end yourself:

    python3 node_setup.py calibrate 3 s1 dry                 # median of the last hour
    python3 node_setup.py calibrate 3 s1 wet --minutes 20
    python3 node_setup.py calibrate 3 s1 dry --until 18:30   # the hour before 18:30
    python3 node_setup.py calibrate 3 s1 dry --value 940     # a reading you already know
    python3 node_setup.py calibrate 3 s1 clear               # back to the fixed scale

It shows the readings it found (how many, the median and how spread out
they are) and asks before saving them to nodes.json (`"soil": {"s1": {"dry":
940, "wet": 610}}`); the dashboard then shows that sensor's dry reading as
0% and its wet one as 100%. An uncalibrated sensor's label has a `*`, and
hovering over it shows the raw reading. Each reading the nodes send is
already the middle of 9 quick ones, so a noisy USB charger's spikes are
thrown out. The hardware check after setup (`python3 node_setup.py
check`) also offers to take either reading from live readings while the
node is plugged into the Pi.

**Bench testing** — don't add test boards to `nodes.json` (`main.py` would
start polling them). `tests/hardware_tests/` has bench files with the same
board IDs (9–12) as Arduino or CircuitPython nodes; point the script at one:

```bash
python3 node_setup.py --nodes ../tests/hardware_tests/bench_nodes_arduino.json --node 9
```

A node with a mistake in `nodes.json` is skipped (with a warning) rather
than stopping `main.py` or `node_setup.py` from running for the others.

The dashboard reads node names, colors and battery labels from the Pi's
list too (`/api/nodes`; its built-in list is only used if the Pi can't be
reached), so a new node needs no other edits: restart `garden-sensor` on
the Pi so it starts polling a radio node (a Wi-Fi node is picked up by
itself).

Flashing Arduino boards needs PlatformIO on the Pi, a one-time install
(`deploy/setup.sh --platformio` does the same):

```bash
curl -fsSL -o get-platformio.py https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py
python3 get-platformio.py && rm get-platformio.py
~/.platformio/penv/bin/pio pkg install -d ~/Automated_Watering_System/arduino
```

The last line downloads every board's compiler and libraries up front (20+
minutes, mostly RadioHead from its author's slow site), so setting up a
node only has to compile: a few minutes for the first build of each board
type on a Pi 3B, a minute or two after that.

---

## How nodes reach the Pi: links, sleep and battery saver

Everything here is set per node in the Pi's `raspberrypi/nodes.json`, by
the `node_setup.py add` questions or the commands below (each is listed in
[the quick reference](#quick-reference) at the end). In every case **the
Pi is in charge**: a node never sends anything on its own, it only answers
the Pi's commands (poll, ping, sync, info, sleep, ...).

### The two links

| | RFM69 radio | Wi-Fi |
|---|---|---|
| On the Pi | `main.py` (`garden-sensor` service) | `raspberrypi/wifi_nodes.py` (`garden-wifi` service), TCP port 5006 |
| Range | Long (the garden) | The house Wi-Fi's reach |
| Power on the node | Low: about 16 mA listening | High: tens of mA (M0 WiFi) to 80–130 mA (ESP32) while connected |
| Wiring | An RFM69HCW (FeatherWing or breakout): CS, IRQ (G0 on a breakout), RST and SPI | None: the board's own Wi-Fi chip |
| Readings while the Pi can't be reached | Logged to SD (or M0 flash) and synced later | Lost, unless the node also has storage and the radio |
| Speed | Slow; big packets are split | Fast; one JSON line per packet |

### Which boards can use Wi-Fi

| Board (`"board"`) | Arduino | CircuitPython | Notes |
|---|---|---|---|
| `picow`, `pico2w` | Yes | Yes | |
| `feather_esp32_v2` | Yes | No | |
| `feather_esp32s2` | Yes | No | Needs a u.FL antenna |
| `feather_m0_wifi` (Adafruit 3044) | Yes | No | Wi-Fi takes about 18 KB of the M0's 256 KB flash; setup asks first. It looks like a plain `feather_m0` over USB, so `link` asks which it is |
| `pico`, `pico2`, `feather_m0`, Adalogger | No | No | No Wi-Fi chip |

### Choosing a node's links

`"link"` in `nodes.json` is one of:

| `"link"` | Means |
|---|---|
| `"radio"` (or left out) | Radio only. Needs the radio pins. |
| `"wifi"` | Wi-Fi only. No radio pins; storage defaults to none. |
| `["wifi", "radio"]` | Both, Wi-Fi preferred: the radio is the backup. |
| `["radio", "wifi"]` | Both, radio preferred: Wi-Fi is the backup. |

The `node_setup.py add` questions offer these four on a Wi-Fi board. For a
node that's already in the list:

    python3 node_setup.py link 3 wifi radio   # both, Wi-Fi first (asks for the radio pins if it's new to the radio)
    python3 node_setup.py link 3 radio wifi   # both, radio first
    python3 node_setup.py link 3 wifi         # Wi-Fi only

Changing the links changes what's built into the node's code, so **re-flash
it afterwards** (plug it in, `python3 node_setup.py`), and if it just gained
the radio, restart the radio loop so it starts polling it:
`sudo systemctl restart garden-sensor`. Where it logs is kept as it was.

### Wi-Fi settings

The node joins the same Wi-Fi network as the Pi. `node_setup.py` gives it
the network name and password (read from the Pi's own NetworkManager
connection, with sudo) and the Pi's address and hostname, **never through
git or the terminal**:

- **Arduino** nodes get them over USB right after flashing, and keep them in
  their own flash: an ESP32's NVS or a Pico W's EEPROM sector (both survive
  re-flashing), or on the M0 WiFi a page of program flash (an upload erases
  it, so they're sent again after every upload).
- **CircuitPython** nodes get them written into `settings.toml` on the
  CIRCUITPY drive (`GARDEN_WIFI_SSID`, `GARDEN_WIFI_PASSWORD`,
  `GARDEN_PI_HOST`, `GARDEN_PI_HOSTNAME`, `GARDEN_PI_PORT`). Code updates
  never touch that file, and anything else in it is kept. They're not the
  usual `CIRCUITPY_WIFI_SSID` names on purpose: those would make
  CircuitPython join by itself and start its web workflow.

If the Wi-Fi password or the Pi's address changes, plug the node in and
re-send them without flashing anything:

    python3 node_setup.py --wifi-only

If the Pi's address has moved, the node also looks the Pi up by name: an
ESP32 as `pi.local` (mDNS), the others through the router's DNS. While a
node can't join the network it prints why on its USB console every 30 s
("network not found", "wrong password?", ...), and keeps trying; the Pico W
and M0 retry the join every 30 s (the M0 backs off to every 5 minutes,
since each try makes it wait up to 8 s).

### Nodes with both links

The node listens on both links and answers each command over the link it
came in on, so which link is used is up to the Pi. Each minute the Pi
polls it over **the first link in its list that's up**
(`raspberrypi/links.py`), and only that one, so no reading is stored twice:

- **Wi-Fi is up** while `garden-wifi` has heard from the node in the last
  3 minutes (it pings a connected node each minute even when the radio is
  in use, to know).
- **The radio is up** while `main.py` counts the node as reachable: 3
  missed commands in a row and it's not, until it answers one of the checks
  `main.py` sends every 5 minutes. If `main.py` isn't running at all, the
  radio counts as down after 3 minutes.
- If neither is up, the first link keeps trying.

So a failover takes about 3 minutes either way. To change which link comes
first, **with no re-flash and no restart** (both services re-read
`nodes.json` within a minute, and each poll tells the node too):

    python3 node_setup.py prefer 3 radio      # or: prefer 3 wifi

The dashboard's ping test menu shows each node's link in use right now,
e.g. "ESP32-V2 · Wi-Fi (radio backup)", and Refresh and the ping test go
over it.

### Sleep windows

Any node can sleep every night: `"sleep_window": ["19:00", "07:00"]` (Pi's
local time; it may cross midnight), set by the add questions or in
`nodes.json`. Inside the window the Pi sends the node a sleep command (over
the link in use) with its wake time; the node answers, switches its radio
and Wi-Fi off, and wakes at that time (an M0 also puts its chip into
standby; other boards stay awake on their own clock with the links off). It
still takes a reading every log interval while asleep, and the next day's
sync collects them (nodes with storage). Both services record the sleep in
`node_info.json`, so neither polls a sleeping node or counts it as missing,
and the dashboard shows it as asleep. On waking it rejoins Wi-Fi by itself.

To sleep a node now (bench tests): `python3 sync_indoor.py sleep <node> <minutes>`
(this goes through `main.py`, so only to a node on the radio at the time).
**CircuitPython nodes don't sleep yet**: give them no `sleep_window`.

### Battery saver

For a node with both links on a small battery: `"battery_saver": true`
keeps **only the link in use powered**, since Wi-Fi draws far more than a
listening radio. What it does depends on which link comes first:

| Links | Battery saver | Radio | Wi-Fi |
|---|---|---|---|
| Both | Off (default) | Always on | Always on |
| Radio first | On | Always on | **Off, and never checked.** If the radio fails, the node can't be reached until it's fixed. |
| Radio first | On, `"wifi_check_hours": N` | Always on | Off, except for 5 minutes every N hours (the first right after boot). If the Pi uses Wi-Fi during a check (which only happens when the radio is down), it stays on while the Pi keeps using it. |
| Wi-Fi first | On | Off while Wi-Fi works; on after 5 minutes without hearing the Pi over Wi-Fi, and off again once the Pi is back on Wi-Fi | Always on |

Details worth remembering:

- Only real commands (polls, sync, ...) count as the Pi using a link; the
  "still there?" pings `garden-wifi` sends don't keep Wi-Fi on.
- `prefer` works with battery saver too: each poll tells the node which link
  the Pi prefers, and it switches which one it keeps on (until its next
  restart, when it starts from the order it was built with, and the Pi's
  next poll corrects it).
- After `node_setup.py` gives it new Wi-Fi settings, the node keeps Wi-Fi on
  for 5 minutes even if it's radio first, so setup can see it connect.
- `wifi_check_hours` only matters with the radio first (with Wi-Fi first,
  Wi-Fi is always on anyway).
- Battery saver is built into the node's code, so **re-flash after turning
  it on or off**:

        python3 node_setup.py saver 2 on                         # Wi-Fi never checked
        python3 node_setup.py saver 2 on --wifi-check-hours 24   # check Wi-Fi once a day
        python3 node_setup.py saver 2 off

  Going down to one link (`link 2 radio`) turns battery saver off, since it
  has nothing left to choose between.

A sleep window and battery saver work together: battery saver decides which
link is on during the day, and at night both are off.

### Quick reference

| Command (in `raspberrypi/`) | What it does | Re-flash? | Restart? |
|---|---|---|---|
| `python3 node_setup.py add` | Add a node (links, battery saver, sleep window among the questions) | Sets it up | `garden-sensor`, for a radio node |
| `python3 node_setup.py link N wifi radio` | Change a node's links (in order of preference) | Yes | `garden-sensor`, if it gained the radio |
| `python3 node_setup.py prefer N radio` | Change which of its two links comes first | No | No |
| `python3 node_setup.py saver N on [--wifi-check-hours H]` | Battery saver on (or `off`) | Yes | No |
| `python3 node_setup.py --wifi-only` | Re-send the Wi-Fi settings to the plugged-in node | No | No |
| `python3 node_setup.py list` | Each node's links, storage and sleep window | | |
| `sudo journalctl -u garden-wifi -f` | Wi-Fi nodes connecting, polls, sleeps | | |
| `sudo journalctl -u garden-sensor -f` | Radio polls, pings, sleeps | | |

**After pulling new code on the Pi**, restart the services
(`sudo systemctl restart garden-sensor garden-wifi garden-api`): running
services keep the old code until then. A Wi-Fi node that's turned away with
"isn't a Wi-Fi node in nodes.json" in `garden-wifi`'s log is usually a
`garden-wifi` still running code from before that board could use Wi-Fi.

## Syncing a node over USB

For a big backlog (days of readings waiting on a node's SD card), plug the
node into the Pi with a USB data cable and pull it over the cable, at
hundreds of lines a second instead of a few:

```bash
cd ~/Automated_Watering_System/raspberrypi
python3 usb_sync.py              # finds the node on any USB serial port
```

`main.py` keeps running; while `usb_sync.py` works, it leaves that node's
radio sync alone, and afterwards carries on from wherever the node got to.
Lines are stored exactly as radio sync stores them. Needs `python3-serial`
(installed by `setup.sh`; otherwise `sudo apt install python3-serial`).

The Pico talks over a second USB port that `circuitpython/boot.py` turns
on, so copy `boot.py` onto it and reset it (unplug, or the reset button)
once before the first USB sync. The M0 needs nothing extra, but must be
awake (not in its overnight sleep), since standby disconnects its USB.

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
2. **Nightly `sensors.db` backups to a USB stick.** Format a stick as
   exFAT with the volume label `GARDENBAK` (on Windows: right-click the
   drive → Format) and leave it plugged into the Pi. `install.sh` sets up
   `garden-backup.timer`, which at 03:30 each night mounts the stick,
   writes a checked, compressed snapshot to `garden_db/sensors_<date>.db.gz`
   and unmounts it again. It keeps 14 nightly copies plus the 1st of each
   month for a year. Run one now, or check the last run:
   ```bash
   sudo systemctl start garden-backup
   sudo journalctl -u garden-backup -n 20 --no-pager
   systemctl list-timers garden-backup     # when the next run is due
   ```
   To restore one: stop `garden-sensor`, then
   `gunzip -c sensors_<date>.db.gz > ~/Automated_Watering_System/raspberrypi/sensors.db`,
   delete any `sensors.db-wal`/`-shm` next to it, and start the service.
   (For a one-off copy by hand, use
   `sqlite3 sensors.db ".backup /path/copy.db"`. Never copy `sensors.db`
   directly while `garden-sensor` is running: it's a live WAL-mode
   database, and a raw copy can catch it half-written.)
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
