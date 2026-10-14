# tests/software_tests/test_weather_outlook.py
"""Every forecast's cloud cover, and whether they agree it'll be a dark day."""
import json
from datetime import datetime, timezone

import pytest

import weather_outlook as wo


def local(hour_utc):
    '''A UTC time as the local hour key the parsers produce.'''
    return datetime.fromisoformat(hour_utc).replace(tzinfo=timezone.utc).astimezone().strftime(wo.HOUR)


# ── Each source's answer ─────────────────────────────────────────────────────

def test_open_meteo():
    data = {"hourly": {"time": ["2026-10-14T13:00", "2026-10-14T14:00", "2026-10-14T15:00"],
                       "cloud_cover": [80, None, 95]}}
    assert wo.parse_open_meteo(data) == {"2026-10-14T13:00": 80, "2026-10-14T15:00": 95}


def test_nws_intervals_cover_every_hour_in_them():
    data = {"properties": {"skyCover": {"values": [
        {"validTime": "2026-10-14T12:00:00+00:00/PT3H", "value": 90},
        {"validTime": "2026-10-14T15:00:00+00:00/PT1H", "value": 40},
        {"validTime": "2026-10-14T16:00:00+00:00/PT1H", "value": None}]}}}
    cloud = wo.parse_nws(data)
    assert cloud == {local("2026-10-14T12:00"): 90, local("2026-10-14T13:00"): 90,
                     local("2026-10-14T14:00"): 90, local("2026-10-14T15:00"): 40}


def test_iso_durations():
    assert wo._iso_duration_hours("PT3H") == 3
    assert wo._iso_duration_hours("P1DT6H") == 30
    assert wo._iso_duration_hours("PT30M") == 1


def test_weatherapi():
    data = {"forecast": {"forecastday": [{"hour": [{"time": "2026-10-14 13:00", "cloud": 75},
                                                   {"time": "2026-10-14 14:00", "cloud": 100}]}]}}
    assert wo.parse_weatherapi(data) == {"2026-10-14T13:00": 75, "2026-10-14T14:00": 100}


def test_openweathermap_three_hour_steps():
    dt = int(datetime(2026, 10, 14, 12, tzinfo=timezone.utc).timestamp())
    cloud = wo.parse_openweathermap({"forecast": {"list": [{"dt": dt, "clouds": {"all": 88}}]}})
    assert cloud == {local("2026-10-14T12:00"): 88, local("2026-10-14T13:00"): 88,
                     local("2026-10-14T14:00"): 88}


def test_tomorrowio():
    data = {"timelines": {"hourly": [{"time": "2026-10-14T12:00:00Z", "values": {"cloudCover": 92.5}},
                                     {"time": "2026-10-14T13:00:00Z", "values": {}}]}}
    assert wo.parse_tomorrowio(data) == {local("2026-10-14T12:00"): 92.5}


# ── The verdict ──────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 14, 7, 5)


def outlook(tmp_path, **clouds):
    '''Sources fetched at 07:00 with the same cloud % for every hour of the day.'''
    hours = [f"2026-10-14T{h:02}:00" for h in range(24)]
    path = tmp_path / "outlook.json"
    path.write_text(json.dumps({"sources": {
        name: ({"at": "2026-10-14T07:00:00", "cloud": {h: c for h in hours}} if c is not None
               else {"error": "HTTP 500"})
        for name, c in clouds.items()}}))
    return path


def dark(tmp_path, **clouds):
    return wo.dark_day(NOW, datetime(2026, 10, 14, 10, 5), now=NOW, path=outlook(tmp_path, **clouds))


def test_every_forecast_must_say_dark(tmp_path):
    verdict, why = dark(tmp_path, nws=90, weatherapi=80, tomorrowio=100)
    assert verdict and why.startswith("3 forecasts agree it's dark")
    verdict, why = dark(tmp_path, nws=90, weatherapi=40, tomorrowio=100)
    assert not verdict and "weatherapi 40%" in why


def test_too_few_forecasts_is_not_dark(tmp_path):
    verdict, why = dark(tmp_path, nws=95, weatherapi=None)
    assert not verdict and "only 1 forecast" in why
    assert wo.dark_day(NOW, NOW, path=tmp_path / "missing.json") == (False, "no weather outlook yet")


def test_stale_forecasts_dont_count(tmp_path):
    path = outlook(tmp_path, nws=95, weatherapi=95)
    later = datetime(2026, 10, 14, 11, 0)   # four hours after they were fetched
    verdict, why = wo.dark_day(later, datetime(2026, 10, 14, 14, 0), now=later, path=path)
    assert not verdict and "only 0 forecasts" in why


def test_only_the_hours_being_slept_through_count(tmp_path):
    path = tmp_path / "outlook.json"
    morning_dark = {f"2026-10-14T{h:02}:00": (95 if h < 12 else 10) for h in range(24)}
    path.write_text(json.dumps({"sources": {n: {"at": "2026-10-14T07:00:00", "cloud": morning_dark}
                                            for n in ("nws", "open-meteo")}}))
    assert wo.dark_day(NOW, datetime(2026, 10, 14, 10, 5), now=NOW, path=path)[0]
    assert not wo.dark_day(datetime(2026, 10, 14, 11, 0), datetime(2026, 10, 14, 14, 0),
                           now=datetime(2026, 10, 14, 11, 0), path=path)[0]


# ── Refreshing ───────────────────────────────────────────────────────────────

def test_refresh_asks_the_keyless_sources_and_those_with_keys(tmp_path):
    asked = []

    def fetch(source, lat, lon, key):
        asked.append((source, key))
        return {"2026-10-14T13:00": 50}

    path = tmp_path / "outlook.json"
    wo.refresh(40.4, -80.0, {"weatherapi": "k1", "openweathermap": None, "tomorrowio": ""},
               path=path, fetch=fetch)
    assert asked == [("open-meteo", None), ("nws", None), ("weatherapi", "k1")]
    assert set(json.loads(path.read_text())["sources"]) == {"open-meteo", "nws", "weatherapi"}


def test_a_failed_source_keeps_its_last_forecast(tmp_path):
    path = tmp_path / "outlook.json"
    wo.refresh(0, 0, {}, path=path, fetch=lambda *a: {"2026-10-14T13:00": 90})

    def flaky(source, *a):
        if source == "nws":
            raise RuntimeError("HTTP 503")
        return {"2026-10-14T13:00": 70}

    wo.refresh(0, 0, {}, path=path, fetch=flaky)
    sources = json.loads(path.read_text())["sources"]
    assert sources["nws"]["cloud"] == {"2026-10-14T13:00": 90} and sources["nws"]["error"] == "HTTP 503"
    assert sources["open-meteo"]["cloud"] == {"2026-10-14T13:00": 70}
