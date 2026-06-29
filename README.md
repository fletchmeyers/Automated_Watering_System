A wireless garden monitoring system using a Raspberry Pi 3B and Pico 2W communicating over RFM69 radio. The Pi polls sensors on the Pico, logs the data, and pushes it to GitHub Pages where a live [dashboard](https://fletchmeyers.github.io/Automated_Watering_System/) displays readings. 

# Garden Sensor System — Setup Guide

A complete walkthrough for getting the Pi–Pico sensor system running with a live GitHub Pages dashboard. This guide assumes you’ve already got the OS onto the raspberry pi as described [here](https://docs.google.com/document/d/19ZkGzDxjjHFDRIUUecaw_BNRt2LQPFqMh1tjr7Oy-5E/edit?tab=t.0). This guide was written by an LLM, so let me know if something isn’t clear or doesn’t work like it should!

---

## Overview

- **Pico 2W (RP2350)** runs CircuitPython, reads sensors, and responds to commands from the Pi over RFM69 radio
- **Raspberry Pi 3B** polls the Pico on a timer, receives sensor data, writes it to a file, and pushes that file to GitHub
- **GitHub Pages** serves the dashboard HTML, which fetches the data file from the repo

---

## Part 1: Pico Setup (CircuitPython)

### 1.1 Install CircuitPython

Flash CircuitPython 10.0.3 for the RP2350 onto the Pico 2W. The Pico will appear as a USB drive called `CIRCUITPY`.

### 1.2 Install libraries

Copy the following Adafruit libraries into the `lib/` folder on `CIRCUITPY`. Get them from the CircuitPython library bundle matching your version:

- `adafruit_rfm69`
- `adafruit_pcf8523`
- `adafruit_max1704x`
- `adafruit_ltr390`
- `adafruit_seesaw`
- `adafruit_sht4x`
- `adafruit_sgp40`
- `adafruit_ina23x`

### 1.3 Copy code files

Copy these files to the root of `CIRCUITPY`:

- `code.py`
- `hardware_setup_garden.py`
- `communication_garden.py`
- `sync_garden.py`

### 1.4 Configure

In `hardware_setup_garden.py`, set:

```python
NODE_ID = 1              # unique ID for this Pico node
SENSE_INTERVAL = 3       # seconds between sensor reads
RADIO_FREQ_MHZ = 915.0
```

Set the encryption key — this must match exactly on both the Pico and the Pi:

```python
rfm69.encryption_key = b"\x01\x02\x03\x04\x05\x06\x07\x08\x01\x02\x03\x04\x05\x06\x07\x08"
```

### 1.5 SD card

The Pico logs sensor data to an SD card at `/sd/data.txt`. Make sure a FAT-formatted SD card is inserted before powering on.

### 1.6 Verify

Open Thonny or a serial monitor. You should see sensor readings printing every few seconds and `[CMD]` lines when the Pi sends commands. To check available memory:

```python
import gc; gc.collect(); print(gc.mem_free())
```

---

## Part 2: Pi Setup (Python 3)

### 2.1 Create a virtual environment

The Adafruit libraries must be installed in a virtual environment. Create one and install dependencies:

```bash
python3 -m venv ~/env
source ~/env/bin/activate
pip install adafruit-circuitpython-rfm69
```

You'll need to activate this environment (`source ~/env/bin/activate`) any time you run the Pi code manually from a terminal.

### 2.2 Copy code files

Copy these files into a working directory on the Pi (e.g. `~/Documents/aws/`):

- `main.py`
- `hardware_setup_indoor.py`
- `communication_indoor.py`
- `sync_indoor.py`
- `set_interval.py`
- `dashboard.py`
- `garden_dashboard.html`

### 2.3 Configure

In `main.py`, set:

```python
NODE_IDS      = [1]    # must match NODE_ID values on your Pico(s)
POLL_INTERVAL = 60     # seconds between polls
SYNC_INTERVAL = 3600   # seconds between bulk SD syncs (0 = disabled)
```

Set the same encryption key as the Pico in `hardware_setup_indoor.py`:

```python
rfm69.encryption_key = b"\x01\x02\x03\x04\x05\x06\x07\x08\x01\x02\x03\x04\x05\x06\x07\x08"
```

### 2.4 Run the main loop

```bash
source ~/env/bin/activate
cd ~/Documents/aws
python3 main.py
```

Sensor data will be written to `data_from_pico.txt` in the same directory. You should see poll commands being sent every 60 seconds and incoming sensor packets being logged.

### 2.5 Verify with the local dashboard

```bash
python3 -m http.server 8080
```

Open `http://<pi-ip>:8080/garden_dashboard.html` in a browser on your local network. The dashboard should populate with live data.

### 2.6 Optional: CLI tools

While `main.py` is running, use `sync_indoor.py` from a second terminal:

```bash
python3 sync_indoor.py            # request poll + health report
python3 sync_indoor.py sync       # request bulk SD sync
python3 sync_indoor.py health     # health report only
python3 sync_indoor.py interval 5 # change sense interval to 5s
```

---

## Part 3: GitHub Pages Dashboard

### 3.1 Create the repo

Create a new repo on GitHub (e.g. `Automated_Watering_System`). Enable GitHub Pages under Settings → Pages → Deploy from branch → `main` → `/ (root)`.

### 3.2 Clone the repo onto the Pi

```bash
cd ~
git clone https://github.com/<your-username>/<repo-name>.git
```

### 3.3 Create a GitHub personal access token

1. GitHub → profile picture → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token (classic)
3. Check the `repo` scope
4. Copy the token immediately — you won't see it again

Store the token in the remote URL so git doesn't prompt for it on every push:

```bash
cd ~/<repo-name>
git remote set-url origin https://<your-token>@github.com/<your-username>/<repo-name>.git
```

> **Security note**: Never share or commit your token. If it's exposed, revoke it immediately on GitHub (Settings → Developer settings → Personal access tokens) and generate a new one.

### 3.4 Create a branch for data updates

Keeping data commits on a separate branch prevents noise in your main branch history:

```bash
git checkout -b update_dashboard_data
git push -u origin update_dashboard_data
```

### 3.5 Add the dashboard HTML

Copy `garden_dashboard.html` into the repo root and rename it `index.html`. GitHub Pages serves `index.html` automatically — no Jekyll or other tooling needed.

Update the data file URL near the top of the script section in `index.html`. Find the `DATA_FILE` constant and set it to the raw GitHub URL for your data branch:

```javascript
const DATA_FILE = "https://raw.githubusercontent.com/<your-username>/<repo-name>/update_dashboard_data/data_from_pico.txt";
```

To find the exact URL: navigate to the data file on GitHub → click Raw → copy the URL from the address bar.

Commit and push:

```bash
git add index.html
git commit -m "add dashboard"
git push origin main
```

### 3.6 Create the data push script

Create `~/Automated_Watering_System/push_data.sh`:

```bash
#!/bin/bash
exec >> /home/<your-username>/push_data.log 2>&1
echo "--- $(date) ---"
cd ~/Automated_Watering_System
git checkout update_dashboard_data
cp ~/Documents/aws/data_from_pico.txt data_from_pico.txt
git add data_from_pico.txt
git commit -m "data update" --allow-empty
git push origin update_dashboard_data
```

Replace `<your-username>` and adjust the path to `data_from_pico.txt` to match where `main.py` is writing it.

Make it executable:

```bash
chmod +x ~/Automated_Watering_System/push_data.sh
```

Test it manually first:

```bash
bash ~/Automated_Watering_System/push_data.sh
```

Then check the log:

```bash
cat ~/push_data.log
```

You should see git output confirming a successful push.

### 3.7 Set up the cron job

```bash
crontab -e
```

Add this line at the bottom — retype it manually rather than pasting, as cron is sensitive to hidden characters introduced by some text editors:

```
*/5 * * * * /home/<your-username>/Automated_Watering_System/push_data.sh
```

This pushes updated data every 5 minutes. Adjust the interval to taste. The log file (`~/push_data.log`) is created automatically on the first run — you don't need to create it yourself.

Verify cron is running:

```bash
systemctl status cron
```

After a few minutes, check `~/push_data.log` to confirm the job is firing successfully.

---

## Part 4: Run main.py on Boot (systemd)

Rather than keeping an SSH session open to run `main.py`, configure it as a systemd service so it starts automatically on boot and restarts if it crashes.

### 4.1 Create the service file

```bash
sudo nano /etc/systemd/system/garden-sensor.service
```

Paste the following, replacing `<your-username>` with your Pi username and adjusting paths if your files are in a different location:

```ini
[Unit]
Description=Garden Sensor Main Loop
After=network.target

[Service]
Type=simple
User=<your-username>
WorkingDirectory=/home/<your-username>/Documents/aws
ExecStart=/home/<your-username>/env/bin/python3 /home/<your-username>/Documents/aws/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

> **Important**: `ExecStart` must point at the Python interpreter inside your virtual environment, not `/usr/bin/python3`. The system Python won't have the Adafruit libraries. To find the right path:
> ```bash
> find ~ -name "activate" 2>/dev/null
> ```
> Then test each result:
> ```bash
> /home/<your-username>/env/bin/python3 -c "import board; print('ok')"
> ```
> Use whichever path prints `ok`.

### 4.2 Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable garden-sensor
sudo systemctl start garden-sensor
```

`enable` makes it start on every boot. `start` runs it immediately without rebooting.

### 4.3 Verify it's running

```bash
sudo systemctl status garden-sensor
```

Should show `active (running)`. To watch live output:

```bash
journalctl -u garden-sensor -f
```

This is the equivalent of watching `main.py`'s terminal output — you'll see poll commands, incoming packets, and any errors.

### 4.4 Useful service commands

```bash
sudo systemctl stop garden-sensor      # stop the service
sudo systemctl restart garden-sensor   # restart after config changes
sudo systemctl disable garden-sensor   # prevent it starting on boot
```

---

## Part 5: Verifying Everything Works

- `sudo systemctl status garden-sensor` shows `active (running)`
- `journalctl -u garden-sensor -f` shows poll commands and incoming sensor packets
- `push_data.log` shows successful git pushes every 5 minutes
- The raw data URL (`https://raw.githubusercontent.com/...`) returns sensor data when opened in a browser
- The GitHub Pages dashboard at `https://<your-username>.github.io/<repo-name>/` updates every 10 seconds
- The status dot in the dashboard is green (data less than 60 seconds old) or amber (less than 10 minutes)

---

## Notes

- **Radio range**: The RFM69 at 915 MHz has limited range, especially through walls. Keep the Pico reasonably close to the Pi or use an external antenna. If the Pico loses radio contact, `main.py` keeps running and will resume receiving data when contact is restored — but the dashboard will go amber/red until new data arrives.
- **Virtual environment**: The systemd service uses the venv Python interpreter directly, so you don't need to activate the venv for the service to work. You only need to activate it when running scripts manually in a terminal.
- **Data file path**: The path to `data_from_pico.txt` in `push_data.sh` must match where `main.py` is actually writing it. `sync_indoor.py` defines it as `Path(__file__).parent / "data_from_pico.txt"` — i.e. the same directory as `sync_indoor.py` itself.
- **SD card**: If the Pico loses power mid-write, `sending.txt` may be left on the SD card. It will be retried on the next `sync_request`. If it's stale, delete it manually via Thonny.
- **Cron and redirection**: If the cron job fails to save with a "bad command" error when using `>>` for logging, move the logging into the shell script itself using `exec >> /path/to/logfile 2>&1` at the top — this is more reliable than redirecting in crontab.
