'''
Python 3 — run on the Pi (or copy sensors.db to your own machine and run there)

Ad hoc data exploration. This isn't part of the live system — edit and re-run
freely as you investigate different questions. Run with: python3 explore.py

Currently: plots soil moisture (s0, s2) alongside ambient temp and light over
the same window, and prints a quick correlation summary. Extend from here.
'''

import pandas as pd
import matplotlib.pyplot as plt
from load_data import load_query, by_type

# ── Load ──────────────────────────────────────────────────────────────────────
# Quick overview across everything, mostly to confirm there's data and see
# the actual window it spans before pulling specific sensor types below.
overview = load_query()

if overview.empty:
    print("No data yet — run this again once a few batches have flushed.")
    raise SystemExit

print(f"Loaded {len(overview)} total readings across "
      f"{overview['sensor_type'].nunique()} sensor types")
print(f"Window: {overview['ts'].min()} to {overview['ts'].max()}\n")

s0  = by_type("s0")    # soil moisture, sensor 0
s2  = by_type("s2")    # soil moisture, sensor 2
sht = by_type("sht")   # ambient temp/humidity
uv  = by_type("uv")    # light

print(f"s0: {len(s0)} readings, s2: {len(s2)}, sht: {len(sht)}, uv: {len(uv)}\n")

# ── Plot: soil moisture + ambient temp + light on a shared time axis ─────────
fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)

axes[0].plot(s0.index, s0["m"], label="s0", marker="o", markersize=2)
axes[0].plot(s2.index, s2["m"], label="s2", marker="o", markersize=2)
axes[0].set_ylabel("Soil moisture (raw)")
axes[0].legend()
axes[0].set_title("Soil moisture, ambient temp, and light over time")

axes[1].plot(sht.index, sht["tmp"], color="tab:red")
axes[1].set_ylabel("Ambient temp (°C)")

axes[2].plot(uv.index, uv["lux"], color="tab:orange")
axes[2].set_ylabel("Light (lux)")
axes[2].set_xlabel("Time")

fig.tight_layout()
fig.savefig("soil_vs_conditions.png", dpi=150)
print("Saved plot to soil_vs_conditions.png")

# ── Quick correlation check ────────────────────────────────────────────────────
# Different sensors report on different schedules, so align everything onto a
# shared hourly grid before comparing — otherwise timestamps rarely match exactly.
hourly = pd.DataFrame({
    "s0_moisture":  s0["m"].resample("1h").mean(),
    "s2_moisture":  s2["m"].resample("1h").mean(),
    "ambient_temp": sht["tmp"].resample("1h").mean(),
    "light_lux":    uv["lux"].resample("1h").mean(),
})
print("Hourly-averaged values:")
print(hourly)

print("\nCorrelation matrix (needs several hours of real spread to be meaningful):")
print(hourly.corr())