// ── Weather forecast card ────────────────────────────────────────────────────
// Multi-source: a dropdown in the card header lets you pick which API is on
// display. Each source is an adapter — fetch() resolves to one shared
// normalized shape so renderWeather() only has to know one format:
//
//   {
//     current: { tempC, apparentTempC, humidityPct, windKmh,
//                precipMm|null, precipProbPct|null, icon, label },
//     daily:   [{ date, icon, label, hiC, loC, precipProbPct|null }],
//     attribution: 'Open-Meteo' | 'National Weather Service',
//   }
//
// Adapters normalize to Celsius/km/h/mm regardless of what the upstream API
// natively speaks, so the existing °C/°F toggle (`useFahrenheit`, from
// dashboard.js) and its formatTemp()/tempUnitLabel() helpers keep working
// unchanged across every source.
//
// A note on adding more sources: this is a static GitHub Pages site with no
// backend, so any adapter that needs an API key would ship that key in
// plain text in this file. Fine for a low-stakes hobby key with a generous
// free tier, but worth remembering before wiring one in — it's not private.
// The two sources below (Open-Meteo, NWS) were picked because neither
// needs a key.

// Garden location — defaulted to Pittsburgh, PA. Edit these two if the
// garden itself is elsewhere; everything else (timezone, sunrise/sunset
// framing, NWS gridpoint lookup, etc.) is resolved automatically from the
// coordinates.
const GARDEN_LAT = 40.4406;
const GARDEN_LON = -79.9959;

const WEATHER_REFRESH_MS = 15 * 60 * 1000; // forecasts don't need the 10s sensor cadence

// WMO weather codes, as returned by Open-Meteo's weather_code fields.
// https://open-meteo.com/en/docs — condensed to what's actually
// distinguishable at a glance in a small card.
const WEATHER_CODE_INFO = {
  0:  { icon: '☀️', label: 'Clear sky' },
  1:  { icon: '🌤️', label: 'Mainly clear' },
  2:  { icon: '⛅',  label: 'Partly cloudy' },
  3:  { icon: '☁️', label: 'Overcast' },
  45: { icon: '🌫️', label: 'Fog' },
  48: { icon: '🌫️', label: 'Rime fog' },
  51: { icon: '🌦️', label: 'Light drizzle' },
  53: { icon: '🌦️', label: 'Drizzle' },
  55: { icon: '🌧️', label: 'Dense drizzle' },
  56: { icon: '🌧️', label: 'Freezing drizzle' },
  57: { icon: '🌧️', label: 'Dense freezing drizzle' },
  61: { icon: '🌦️', label: 'Slight rain' },
  63: { icon: '🌧️', label: 'Rain' },
  65: { icon: '🌧️', label: 'Heavy rain' },
  66: { icon: '🌧️', label: 'Freezing rain' },
  67: { icon: '🌧️', label: 'Heavy freezing rain' },
  71: { icon: '🌨️', label: 'Slight snow' },
  73: { icon: '❄️', label: 'Snow' },
  75: { icon: '❄️', label: 'Heavy snow' },
  77: { icon: '❄️', label: 'Snow grains' },
  80: { icon: '🌦️', label: 'Slight showers' },
  81: { icon: '🌧️', label: 'Showers' },
  82: { icon: '⛈️', label: 'Violent showers' },
  85: { icon: '🌨️', label: 'Snow showers' },
  86: { icon: '❄️', label: 'Heavy snow showers' },
  95: { icon: '⛈️', label: 'Thunderstorm' },
  96: { icon: '⛈️', label: 'Thunderstorm, hail' },
  99: { icon: '⛈️', label: 'Thunderstorm, heavy hail' },
};

function weatherCodeInfo(code) {
  return WEATHER_CODE_INFO[code] || { icon: '❓', label: 'Unknown' };
}

function fToC(f) { return (f - 32) * 5 / 9; }
function mphToKmh(mph) { return mph * 1.60934; }

// ── Source: Open-Meteo ───────────────────────────────────────────────────────

async function fetchOpenMeteo() {
  const params = new URLSearchParams({
    latitude: GARDEN_LAT,
    longitude: GARDEN_LON,
    current: 'temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m',
    daily: 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max',
    forecast_days: 6,
    timezone: 'auto',
  });
  const resp = await fetch(`https://api.open-meteo.com/v1/forecast?${params.toString()}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();

  const cur = data.current;
  const curInfo = weatherCodeInfo(cur.weather_code);

  const daily = data.daily.time.map((dateStr, i) => {
    const info = weatherCodeInfo(data.daily.weather_code[i]);
    return {
      date: dateStr,
      icon: info.icon,
      label: info.label,
      hiC: data.daily.temperature_2m_max[i],
      loC: data.daily.temperature_2m_min[i],
      precipProbPct: data.daily.precipitation_probability_max[i],
    };
  });

  return {
    current: {
      tempC: cur.temperature_2m,
      apparentTempC: cur.apparent_temperature,
      humidityPct: cur.relative_humidity_2m,
      windKmh: cur.wind_speed_10m,
      precipMm: cur.precipitation,
      precipProbPct: null,
      icon: curInfo.icon,
      label: curInfo.label,
    },
    daily,
    attribution: 'Open-Meteo',
  };
}

// ── Source: National Weather Service (api.weather.gov) ──────────────────────
// US-only (fine for Pittsburgh). Three-step lookup: resolve lat/lon to a
// forecast office + gridpoint, then fetch that gridpoint's hourly forecast
// (used as a stand-in for "current conditions" — NWS doesn't expose a
// simple current-observation endpoint keyed by lat/lon the way a station
// lookup would, and the first hourly period is close enough for a garden
// dashboard) and 12-hour day/night forecast (used for the daily strip).

// Shared by every source that describes conditions with free text rather
// than a WMO code (NWS, WeatherAPI.com, OpenWeatherMap) — a small keyword
// match onto the same emoji set Open-Meteo's numeric codes resolve to, so
// every source renders through one renderWeather().
function conditionIcon(text) {
  const t = (text || '').toLowerCase();
  if (t.includes('thunder')) return '⛈️';
  if (t.includes('snow') || t.includes('sleet') || t.includes('ice pellet')) return '❄️';
  if (t.includes('rain') || t.includes('shower')) return '🌧️';
  if (t.includes('drizzle')) return '🌦️';
  if (t.includes('fog') || t.includes('mist') || t.includes('haze')) return '🌫️';
  if (t.includes('overcast')) return '☁️';
  if (t.includes('cloud')) return '⛅';
  if (t.includes('clear') || t.includes('sunny')) return '☀️';
  return '❓';
}

async function fetchNWS() {
  const pointsResp = await fetch(`https://api.weather.gov/points/${GARDEN_LAT},${GARDEN_LON}`);
  if (!pointsResp.ok) throw new Error(`HTTP ${pointsResp.status} (points)`);
  const points = await pointsResp.json();
  const { forecast: forecastUrl, forecastHourly: hourlyUrl } = points.properties;

  const [forecastResp, hourlyResp] = await Promise.all([
    fetch(forecastUrl),
    fetch(hourlyUrl),
  ]);
  if (!forecastResp.ok) throw new Error(`HTTP ${forecastResp.status} (forecast)`);
  if (!hourlyResp.ok) throw new Error(`HTTP ${hourlyResp.status} (hourly)`);
  const forecast = await forecastResp.json();
  const hourly = await hourlyResp.json();

  const now = hourly.properties.periods[0];
  const current = {
    tempC: fToC(now.temperature),
    // NWS's hourly endpoint doesn't expose a separate "feels like" value —
    // fall back to actual temp rather than fabricating one.
    apparentTempC: fToC(now.temperature),
    humidityPct: now.relativeHumidity && now.relativeHumidity.value != null ? now.relativeHumidity.value : null,
    windKmh: mphToKmh(parseFloat(now.windSpeed) || 0),
    precipMm: null,
    precipProbPct: now.probabilityOfPrecipitation && now.probabilityOfPrecipitation.value != null
      ? now.probabilityOfPrecipitation.value : null,
    icon: conditionIcon(now.shortForecast),
    label: now.shortForecast,
  };

  // 12-hour periods alternate day/night (`isDaytime`); pair them up by
  // calendar date to get one hi/lo per day, matching Open-Meteo's shape.
  const byDate = new Map();
  for (const p of forecast.properties.periods) {
    const date = p.startTime.slice(0, 10);
    const entry = byDate.get(date) || { date, hiC: null, loC: null, icon: null, label: null, precipProbPct: null };
    if (p.isDaytime) {
      entry.hiC = fToC(p.temperature);
      entry.icon = conditionIcon(p.shortForecast);
      entry.label = p.shortForecast;
    } else {
      entry.loC = fToC(p.temperature);
    }
    if (entry.precipProbPct == null && p.probabilityOfPrecipitation && p.probabilityOfPrecipitation.value != null) {
      entry.precipProbPct = p.probabilityOfPrecipitation.value;
    }
    byDate.set(date, entry);
  }
  const daily = Array.from(byDate.values()).slice(0, 6);

  return { current, daily, attribution: 'National Weather Service' };
}

// ── Sources: WeatherAPI.com, OpenWeatherMap, Tomorrow.io ────────────────────
// All three need an API key, so none of them are called directly from here —
// they go through this dashboard's own Flask API (`/api/weather/<source>`),
// which holds the real key server-side and forwards the request. See
// flask_api.py's api_weather() for the proxy itself. If a key isn't
// configured yet on the server, that endpoint returns a plain-language 501
// which surfaces in the card instead of a raw fetch error.

async function fetchViaBackend(sourceId) {
  const resp = await fetch(`${API_BASE}/api/weather/${sourceId}`);
  let body = null;
  try { body = await resp.json(); } catch (e) { /* non-JSON error page, ignore */ }
  if (!resp.ok) {
    throw new Error((body && body.error) || `HTTP ${resp.status}`);
  }
  return body;
}

async function fetchWeatherAPI() {
  const data = await fetchViaBackend('weatherapi');
  const cur = data.current;

  const daily = data.forecast.forecastday.map(d => ({
    date: d.date,
    icon: conditionIcon(d.day.condition.text),
    label: d.day.condition.text,
    hiC: d.day.maxtemp_c,
    loC: d.day.mintemp_c,
    precipProbPct: d.day.daily_chance_of_rain,
  }));

  return {
    current: {
      tempC: cur.temp_c,
      apparentTempC: cur.feelslike_c,
      humidityPct: cur.humidity,
      windKmh: cur.wind_kph,
      precipMm: cur.precip_mm,
      precipProbPct: null,
      icon: conditionIcon(cur.condition.text),
      label: cur.condition.text,
    },
    daily,
    attribution: 'WeatherAPI.com',
  };
}

async function fetchOpenWeatherMap() {
  const data = await fetchViaBackend('openweathermap');
  const cur = data.current;
  const curCond = (cur.weather && cur.weather[0]) || {};

  // Free tier only gives a 3-hour-step, 5-day forecast — no true daily
  // summary — so entries are grouped by calendar date here. Hi/lo take the
  // extremes across that day's entries; the icon/label come from whichever
  // entry falls closest to local noon, as a stand-in for "the day's weather"
  // (same idea as NWS's isDaytime period, applied to OWM's flatter shape).
  const byDate = new Map();
  for (const entry of data.forecast.list) {
    const date = entry.dt_txt.slice(0, 10);
    const hour = parseInt(entry.dt_txt.slice(11, 13), 10);
    const cond = (entry.weather && entry.weather[0]) || {};
    const pop = entry.pop != null ? Math.round(entry.pop * 100) : null;

    const e = byDate.get(date) || {
      date, hiC: -Infinity, loC: Infinity, icon: null, label: null,
      precipProbPct: null, _noonDist: Infinity,
    };
    e.hiC = Math.max(e.hiC, entry.main.temp_max);
    e.loC = Math.min(e.loC, entry.main.temp_min);
    if (pop != null) e.precipProbPct = e.precipProbPct == null ? pop : Math.max(e.precipProbPct, pop);
    const dist = Math.abs(hour - 12);
    if (dist < e._noonDist) {
      e._noonDist = dist;
      e.icon = conditionIcon(cond.main);
      e.label = cond.description || cond.main || 'Unknown';
    }
    byDate.set(date, e);
  }
  const daily = Array.from(byDate.values()).map(({ _noonDist, ...rest }) => rest);

  const rainMm = (cur.rain && cur.rain['1h']) || 0;
  const snowMm = (cur.snow && cur.snow['1h']) || 0;

  return {
    current: {
      tempC: cur.main.temp,
      apparentTempC: cur.main.feels_like,
      humidityPct: cur.main.humidity,
      windKmh: cur.wind && cur.wind.speed != null ? cur.wind.speed * 3.6 : null, // m/s -> km/h
      precipMm: rainMm + snowMm,
      precipProbPct: null,
      icon: conditionIcon(curCond.main),
      label: curCond.description || curCond.main || 'Unknown',
    },
    daily,
    attribution: 'OpenWeatherMap',
  };
}

// Tomorrow.io uses its own numeric weather codes rather than free text —
// mapped to a label here, then run through the same conditionIcon()
// keyword match every other source uses (the labels below were chosen to
// contain the right keywords, e.g. "fog" / "rain" / "thunderstorm").
const TOMORROW_CODE_LABELS = {
  1000: 'Clear', 1100: 'Mostly clear', 1101: 'Partly cloudy', 1102: 'Mostly cloudy', 1001: 'Cloudy',
  2000: 'Fog', 2100: 'Light fog',
  4000: 'Drizzle', 4001: 'Rain', 4200: 'Light rain', 4201: 'Heavy rain',
  5000: 'Snow', 5001: 'Flurries', 5100: 'Light snow', 5101: 'Heavy snow',
  6000: 'Freezing drizzle', 6001: 'Freezing rain', 6200: 'Light freezing rain', 6201: 'Heavy freezing rain',
  7000: 'Ice pellets', 7101: 'Heavy ice pellets', 7102: 'Light ice pellets',
  8000: 'Thunderstorm',
};

function tomorrowCodeInfo(code) {
  const label = TOMORROW_CODE_LABELS[code] || 'Unknown';
  return { icon: conditionIcon(label), label };
}

async function fetchTomorrowIO() {
  const data = await fetchViaBackend('tomorrowio');
  const hourly = data.timelines.hourly;
  const daily = data.timelines.daily;

  const now = hourly[0].values;
  const nowInfo = tomorrowCodeInfo(now.weatherCode);

  const dailyOut = daily.map(d => {
    const v = d.values;
    const info = tomorrowCodeInfo(v.weatherCodeMax != null ? v.weatherCodeMax : v.weatherCode);
    const precipProbPct = v.precipitationProbabilityAvg != null
      ? Math.round(v.precipitationProbabilityAvg)
      : (v.precipitationProbabilityMax != null ? Math.round(v.precipitationProbabilityMax) : null);
    return {
      date: d.time.slice(0, 10),
      icon: info.icon,
      label: info.label,
      hiC: v.temperatureMax,
      loC: v.temperatureMin,
      precipProbPct,
    };
  });

  return {
    current: {
      tempC: now.temperature,
      apparentTempC: now.temperatureApparent != null ? now.temperatureApparent : now.temperature,
      humidityPct: now.humidity,
      windKmh: now.windSpeed != null ? now.windSpeed * 3.6 : null, // m/s -> km/h
      precipMm: now.rainIntensity != null ? now.rainIntensity : null,
      precipProbPct: now.precipitationProbability != null ? now.precipitationProbability : null,
      icon: nowInfo.icon,
      label: nowInfo.label,
    },
    daily: dailyOut,
    attribution: 'Tomorrow.io',
  };
}

// ── Source registry ───────────────────────────────────────────────────────────

const WEATHER_SOURCES = [
  { id: 'open-meteo',      label: 'Open-Meteo',      fetch: fetchOpenMeteo },
  { id: 'nws',             label: 'NWS',              fetch: fetchNWS },
  { id: 'weatherapi',      label: 'WeatherAPI.com',   fetch: fetchWeatherAPI },
  { id: 'openweathermap',  label: 'OpenWeatherMap',   fetch: fetchOpenWeatherMap },
  { id: 'tomorrowio',      label: 'Tomorrow.io',      fetch: fetchTomorrowIO },
];

let currentWeatherSourceId = 'open-meteo';
const weatherCache = {}; // sourceId -> { data, fetchedAt }

// Same color bands as tempColor() in dashboard.js, applied to the card
// badge instead of a text color — kept as a separate small function rather
// than reusing tempColor() since a badge needs a class name, not a hex color.
function tempBadgeClass(c) {
  if (c < 10) return 'badge-blue';
  if (c < 25) return 'badge-green';
  if (c < 35) return 'badge-amber';
  return 'badge-red';
}

function kmhToMph(kmh) { return kmh * 0.621371; }
function mmToInches(mm) { return mm * 0.0393701; }

function formatWind(kmh) {
  return useFahrenheit
    ? `${Math.round(kmhToMph(kmh))} mph`
    : `${Math.round(kmh)} km/h`;
}

function formatPrecip(mm) {
  return useFahrenheit
    ? `${mmToInches(mm).toFixed(2)} in`
    : `${mm.toFixed(1)} mm`;
}

function dayLabel(dateStr, index) {
  if (index === 0) return 'Today';
  const d = new Date(`${dateStr}T00:00:00`);
  return d.toLocaleDateString(undefined, { weekday: 'short' });
}

// Last successful fetch for whichever source is currently displayed. Kept
// as a plain global (not per-source) because dashboard.js's setUnitPref()
// re-renders through this exact variable when the °C/°F toggle flips —
// same contract the old single-source version of this file used.
let lastWeatherData = null;

function renderWeather(data) {
  const badge = document.getElementById('weather-badge');
  const cur = data.current;

  badge.className = 'card-badge ' + tempBadgeClass(cur.tempC);
  badge.textContent = `${Math.round(formatTemp(cur.tempC))}${tempUnitLabel()}`;

  const dailyHtml = data.daily.map((d, i) => `<div class="weather-day">
      <div class="weather-day-label">${dayLabel(d.date, i)}</div>
      <div class="weather-day-icon">${d.icon || '❓'}</div>
      <div class="weather-day-temps">
        <span class="weather-day-hi">${d.hiC != null ? Math.round(formatTemp(d.hiC)) : '–'}°</span>
        <span class="weather-day-lo">${d.loC != null ? Math.round(formatTemp(d.loC)) : '–'}°</span>
      </div>
      <div class="weather-day-precip">${d.precipProbPct != null ? d.precipProbPct + '%' : '–'}</div>
    </div>`).join('');

  const precipLabel = cur.precipMm != null ? 'PRECIP NOW' : 'PRECIP CHANCE';
  const precipValue = cur.precipMm != null
    ? formatPrecip(cur.precipMm)
    : (cur.precipProbPct != null ? cur.precipProbPct + '%' : '–');

  document.getElementById('weather-body').innerHTML = `
    <div class="weather-current">
      <div class="weather-current-icon">${cur.icon || '❓'}</div>
      <div class="weather-current-main">
        <div class="big-value">${Math.round(formatTemp(cur.tempC))}<span class="big-unit">${tempUnitLabel()}</span></div>
        <div class="sub-value">Feels like ${Math.round(formatTemp(cur.apparentTempC))}${tempUnitLabel()} · ${cur.label || 'Unknown'}</div>
      </div>
      <div class="row3 weather-current-extra">
        <div class="mini-metric">
          <div class="mini-label">HUMIDITY</div>
          <div class="mini-value">${cur.humidityPct != null ? Math.round(cur.humidityPct) + '%' : '–'}</div>
        </div>
        <div class="mini-metric">
          <div class="mini-label">WIND</div>
          <div class="mini-value">${formatWind(cur.windKmh)}</div>
        </div>
        <div class="mini-metric">
          <div class="mini-label">${precipLabel}</div>
          <div class="mini-value">${precipValue}</div>
        </div>
      </div>
    </div>
    <div class="weather-daily">${dailyHtml}</div>
    <div class="weather-attribution">${data.attribution}</div>`;
}

function showWeatherError(source, err) {
  console.error(`weather fetch failed (${source.id})`, err);
  const badge = document.getElementById('weather-badge');
  badge.className = 'card-badge badge-gray';
  badge.textContent = 'error';
  document.getElementById('weather-body').innerHTML =
    `<div class="no-data">Could not load forecast from ${source.label}</div>`;
}

// Fetches (or reuses a fresh cached copy of) one source's data. Renders it
// only if that source is still the one selected in the dropdown by the time
// the fetch resolves — guards against a slow response from a source the
// user has since switched away from clobbering what's on screen.
async function loadWeather(sourceId, { force = false } = {}) {
  const source = WEATHER_SOURCES.find(s => s.id === sourceId);
  if (!source) return;

  const cached = weatherCache[sourceId];
  if (!force && cached && (Date.now() - cached.fetchedAt) < WEATHER_REFRESH_MS) {
    if (sourceId === currentWeatherSourceId) {
      lastWeatherData = cached.data;
      renderWeather(cached.data);
    }
    return;
  }

  try {
    const data = await source.fetch();
    weatherCache[sourceId] = { data, fetchedAt: Date.now() };
    if (sourceId === currentWeatherSourceId) {
      lastWeatherData = data;
      renderWeather(data);
    }
  } catch (e) {
    if (sourceId === currentWeatherSourceId) showWeatherError(source, e);
  }
}

function onWeatherSourceChange(sourceId) {
  currentWeatherSourceId = sourceId;
  loadWeather(sourceId);
}

function initWeatherPanel() {
  const select = document.getElementById('weather-source-select');
  if (select) {
    select.innerHTML = WEATHER_SOURCES.map(s => `<option value="${s.id}">${s.label}</option>`).join('');
    select.value = currentWeatherSourceId;
  }
  loadWeather(currentWeatherSourceId);
  // Only the currently-displayed source needs to stay fresh on a timer;
  // the rest refetch lazily the next time someone picks them from the
  // dropdown, rather than hitting every API on every 15-minute tick.
  setInterval(() => loadWeather(currentWeatherSourceId, { force: true }), WEATHER_REFRESH_MS);
}