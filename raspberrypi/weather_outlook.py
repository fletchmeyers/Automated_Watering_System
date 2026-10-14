'''
Python 3 running on Raspberry Pi 3B

Will today be a dark day? The forecasts' cloud cover, for the low-battery
day sleep (communication_indoor.SleepScheduler): a node with a low battery
only sleeps through the day when every forecast that answered agrees the
hours it would sleep through will be dark, so a node that could be charging
in the sun stays awake.

garden-api (flask_api.py) holds the API keys, so it fetches every source
every REFRESH_MINUTES and writes OUTLOOK_FILE: each source's hourly cloud
cover in the Pi's local time, {"2026-10-14T13:00": 85, ...}. The radio loop
and garden-wifi only read that file (dark_day()), so they need no keys and
no network calls of their own.

The sources are the ones the dashboard's weather row offers: Open-Meteo and
the NWS (no key), and WeatherAPI, OpenWeatherMap and Tomorrow.io (keys in
/etc/garden-api.env; one without a key is just left out).

Written by Fletcher Meyers
October 2026
'''

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTLOOK_FILE    = Path("/tmp/garden_weather_outlook.json")
REFRESH_MINUTES = 30
DARK_CLOUD      = 75   # percent: average cloud cover at or above this is a dark day
MIN_SOURCES     = 2    # forecasts that must answer (and all agree) before it counts
MAX_AGE_HOURS   = 3    # a source's forecast older than this is ignored
HOUR            = "%Y-%m-%dT%H:00"


def _local_hour(dt):
    '''An aware datetime as the Pi's local hour, "YYYY-MM-DDTHH:00".'''
    return dt.astimezone().strftime(HOUR)


# ── Each source's answer as {local hour: cloud %} ────────────────────────────

def parse_open_meteo(data):
    '''hourly=cloud_cover with timezone=auto: times are already local.'''
    hourly = data["hourly"]
    return {t[:13] + ":00": c for t, c in zip(hourly["time"], hourly["cloud_cover"]) if c is not None}


def _iso_duration_hours(text):
    '''"PT3H" -> 3, "P1DT6H" -> 30 (the NWS's interval lengths).'''
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", text)
    if not m:
        return 1
    days, hours = int(m.group(1) or 0), int(m.group(2) or 0)
    return max(1, days * 24 + hours)


def parse_nws(data):
    '''Gridpoint skyCover: values over intervals like "2026-10-14T12:00:00+00:00/PT3H".'''
    cloud = {}
    for v in data["properties"]["skyCover"]["values"]:
        if v.get("value") is None:
            continue
        start, _, length = v["validTime"].partition("/")
        start = datetime.fromisoformat(start)
        for h in range(_iso_duration_hours(length)):
            cloud[_local_hour(start + timedelta(hours=h))] = v["value"]
    return cloud


def parse_weatherapi(data):
    '''forecastday[].hour[]: "time" in the garden's local time, "cloud" %.'''
    return {h["time"].replace(" ", "T")[:13] + ":00": h["cloud"]
            for day in data["forecast"]["forecastday"] for h in day["hour"]}


def parse_openweathermap(data):
    '''The 3-hourly forecast: "dt" (UTC seconds) and "clouds.all" %, each
    covering the three hours from dt.'''
    cloud = {}
    for entry in data["forecast"]["list"]:
        start = datetime.fromtimestamp(entry["dt"], timezone.utc)
        for h in range(3):
            cloud[_local_hour(start + timedelta(hours=h))] = entry["clouds"]["all"]
    return cloud


def parse_tomorrowio(data):
    '''timelines.hourly[]: "time" (UTC ISO) and values.cloudCover %.'''
    cloud = {}
    for entry in data["timelines"]["hourly"]:
        value = entry["values"].get("cloudCover")
        if value is not None:
            cloud[_local_hour(datetime.fromisoformat(entry["time"].replace("Z", "+00:00")))] = value
    return cloud


# ── Fetching (garden-api only) ───────────────────────────────────────────────

def _fetch(source, lat, lon, key):
    import requests   # only garden-api fetches; the services just read the file
    get = lambda url, **params: _json(requests.get(url, params=params, timeout=15,
                                                   headers={"User-Agent": "garden-dashboard"}))
    if source == "open-meteo":
        return parse_open_meteo(get("https://api.open-meteo.com/v1/forecast", latitude=lat, longitude=lon,
                                    hourly="cloud_cover", forecast_days=2, timezone="auto"))
    if source == "nws":
        points = get(f"https://api.weather.gov/points/{lat},{lon}")
        return parse_nws(get(points["properties"]["forecastGridData"]))
    if source == "weatherapi":
        return parse_weatherapi(get("https://api.weatherapi.com/v1/forecast.json",
                                    q=f"{lat},{lon}", days=2, key=key))
    if source == "openweathermap":
        return parse_openweathermap({"forecast": get("https://api.openweathermap.org/data/2.5/forecast",
                                                     lat=lat, lon=lon, appid=key)})
    if source == "tomorrowio":
        return parse_tomorrowio(get("https://api.tomorrow.io/v4/weather/forecast",
                                    location=f"{lat},{lon}", timesteps="1h", apikey=key))
    raise ValueError(f"unknown source {source}")


def _json(resp):
    resp.raise_for_status()
    return resp.json()


def refresh(lat, lon, keys, path=OUTLOOK_FILE, fetch=_fetch):
    '''Fetch every source (keys: {source: key}; the keyless ones need none)
    and write path. A source that fails keeps its last good forecast, with
    the error noted.'''
    try:
        old = json.loads(Path(path).read_text()).get("sources", {})
    except (OSError, ValueError):
        old = {}
    sources = {}
    for source in ("open-meteo", "nws", "weatherapi", "openweathermap", "tomorrowio"):
        key = keys.get(source)
        if source not in ("open-meteo", "nws") and not key:
            continue
        try:
            sources[source] = {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                               "cloud": fetch(source, lat, lon, key)}
        except Exception as e:
            print(f"[WEATHER] Outlook from {source} failed: {e}")
            sources[source] = {**old.get(source, {}), "error": str(e)}
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps({"updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                               "sources": sources}))
    tmp.replace(path)


def keep_refreshed(lat, lon, keys):
    '''garden-api's thread: refresh() every REFRESH_MINUTES.'''
    while True:
        try:
            refresh(lat, lon, keys)
        except Exception as e:
            print(f"[WEATHER] Outlook refresh failed: {e}")
        time.sleep(REFRESH_MINUTES * 60)


# ── The verdict (the radio loop and garden-wifi) ─────────────────────────────

def dark_day(start, end, now=None, path=OUTLOOK_FILE):
    '''Do the forecasts agree the hours from start to end will be dark?
    (True, "...") only if at least MIN_SOURCES fresh forecasts cover those
    hours and every one of them averages DARK_CLOUD% cloud or more; else
    (False, why) — including when too few answered, so a node never sleeps
    away a day the forecasts can't vouch for.'''
    now = now or datetime.now()
    try:
        sources = json.loads(Path(path).read_text()).get("sources", {})
    except (OSError, ValueError):
        return False, "no weather outlook yet"
    hours = []
    t = start.replace(minute=0, second=0, microsecond=0)
    while t < end:
        hours.append(t.strftime(HOUR))
        t += timedelta(hours=1)
    averages = {}
    for name, source in sorted(sources.items()):
        try:
            fresh = now - datetime.strptime(source["at"], "%Y-%m-%dT%H:%M:%S") < timedelta(hours=MAX_AGE_HOURS)
        except (KeyError, ValueError):
            continue
        values = [source["cloud"][h] for h in hours if h in source.get("cloud", {})]
        if fresh and values:
            averages[name] = sum(values) / len(values)
    summary = ", ".join(f"{name} {avg:.0f}%" for name, avg in averages.items())
    if len(averages) < MIN_SOURCES:
        return False, f"only {len(averages)} forecast{'s' * (len(averages) != 1)} to go on"
    bright = [name for name, avg in averages.items() if avg < DARK_CLOUD]
    if bright:
        return False, f"not all forecasts say it's dark ({summary} cloud)"
    return True, f"{len(averages)} forecasts agree it's dark ({summary} cloud)"
