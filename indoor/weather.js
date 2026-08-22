// ── Weather forecast card ────────────────────────────────────────────────────
// Separate from the sensor cards: no radio/Pi involvement at all, just a
// direct client-side call to Open-Meteo (free, no API key, CORS-friendly —
// unlike the sensor data this doesn't need the Cloudflare Tunnel/Flask API
// in the middle). Loaded after dashboard.js so it can reuse the existing
// unit-preference flag (`useFahrenheit`) and helpers (`formatTemp`,
// `tempUnitLabel`) rather than keeping a second copy of unit-conversion
// logic — flipping the header's °C/°F toggle switches this card between
// metric (°C, km/h, mm) and imperial (°F, mph, in) as a matched pair.

// Garden location — defaulted to Pittsburgh, PA. Edit these two if the
// garden itself is elsewhere; everything else (timezone, sunrise/sunset
// framing, etc.) is resolved automatically from the coordinates.
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

// Last successful fetch, kept so toggling °C/°F can re-render without
// re-fetching — same pattern as currentPackets in dashboard.js.
let lastWeatherData = null;

async function fetchWeather() {
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
  return resp.json();
}

function renderWeather(data) {
  const badge = document.getElementById('weather-badge');
  const cur = data.current;
  const curInfo = weatherCodeInfo(cur.weather_code);

  badge.className = 'card-badge ' + tempBadgeClass(cur.temperature_2m);
  badge.textContent = `${Math.round(formatTemp(cur.temperature_2m))}${tempUnitLabel()}`;

  const days = data.daily.time.map((dateStr, i) => ({
    dateStr,
    code: data.daily.weather_code[i],
    hi: data.daily.temperature_2m_max[i],
    lo: data.daily.temperature_2m_min[i],
    precipProb: data.daily.precipitation_probability_max[i],
  }));

  const dailyHtml = days.map((d, i) => {
    const info = weatherCodeInfo(d.code);
    return `<div class="weather-day">
      <div class="weather-day-label">${dayLabel(d.dateStr, i)}</div>
      <div class="weather-day-icon">${info.icon}</div>
      <div class="weather-day-temps">
        <span class="weather-day-hi">${Math.round(formatTemp(d.hi))}°</span>
        <span class="weather-day-lo">${Math.round(formatTemp(d.lo))}°</span>
      </div>
      <div class="weather-day-precip">${d.precipProb != null ? d.precipProb + '%' : '–'}</div>
    </div>`;
  }).join('');

  document.getElementById('weather-body').innerHTML = `
    <div class="weather-current">
      <div class="weather-current-icon">${curInfo.icon}</div>
      <div class="weather-current-main">
        <div class="big-value">${Math.round(formatTemp(cur.temperature_2m))}<span class="big-unit">${tempUnitLabel()}</span></div>
        <div class="sub-value">Feels like ${Math.round(formatTemp(cur.apparent_temperature))}${tempUnitLabel()} · ${curInfo.label}</div>
      </div>
      <div class="row3 weather-current-extra">
        <div class="mini-metric">
          <div class="mini-label">HUMIDITY</div>
          <div class="mini-value">${Math.round(cur.relative_humidity_2m)}%</div>
        </div>
        <div class="mini-metric">
          <div class="mini-label">WIND</div>
          <div class="mini-value">${formatWind(cur.wind_speed_10m)}</div>
        </div>
        <div class="mini-metric">
          <div class="mini-label">PRECIP NOW</div>
          <div class="mini-value">${formatPrecip(cur.precipitation)}</div>
        </div>
      </div>
    </div>
    <div class="weather-daily">${dailyHtml}</div>`;
}

async function loadWeather() {
  try {
    const data = await fetchWeather();
    lastWeatherData = data;
    renderWeather(data);
  } catch (e) {
    console.error('weather fetch failed', e);
    const badge = document.getElementById('weather-badge');
    badge.className = 'card-badge badge-gray';
    badge.textContent = 'error';
    document.getElementById('weather-body').innerHTML =
      '<div class="no-data">Could not load forecast</div>';
  }
}

function initWeatherPanel() {
  loadWeather();
  setInterval(loadWeather, WEATHER_REFRESH_MS);
}