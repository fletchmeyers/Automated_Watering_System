const DATA_WINDOW_MINUTES = 360;  // how much history /api/data returns (6 hours)
const WINDOW = 2000;              // cap on packets kept in memory (used when triggerRefresh merges a manual poll in)
const REFRESH_MS = 10000;

// Cloudflare Tunnel + domain — dashboard reads live from sensors.db via /api/data.
const API_BASE = "https://api.fletchermeyers.com";

// ── Nodes ─────────────────────────────────────────────────────────────────────
// Display name, short name (used in plot labels) and color for each radio
// node ID, read from nodes.json at the repo root by loadNodes() — add a node
// there, not here. The list below is only used if that file can't be loaded.
// A node that isn't listed still shows up, as "Node N" in one of the
// fallback colors. Colors stay clear of the green/amber/red used for status
// so a node never reads as a warning. Shared with analysis.js (loaded after
// this file, same page scope).
//
// `battery` says which sensor measures the node's supply, for the Nodes
// card: a MAX17048 fuel gauge ("batt": charge % and cell voltage) unless
// set otherwise, e.g. an INA238 power monitor ("pw0": voltage and current).

const NODES = {
  1: { name: 'Pico (CircuitPython)', short: 'Pico', color: '#79c0ff',
       battery: { type: 'pw0', label: 'CAR BATTERY' } },
  2: { name: 'M0 (Arduino)',         short: 'M0',   color: '#f778ba' },
};
const NODES_URL = '../nodes.json';   // the repo root, next to dashboard/ on GitHub Pages

// Replace NODES with what nodes.json says. Never throws: on any problem the
// built-in list above stays, so the dashboard still starts.
async function loadNodes(timeoutMs = 4000) {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const res = await fetch(NODES_URL, { cache: 'no-cache', signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const listed = (await res.json()).nodes;
    if (!listed || typeof listed !== 'object' || !Object.keys(listed).length) {
      throw new Error('no "nodes" in it');
    }
    for (const id of Object.keys(NODES)) delete NODES[id];
    for (const [id, n] of Object.entries(listed)) {
      NODES[id] = {
        name: n.name || `Node ${id}`,
        short: n.short || `N${id}`,
        color: n.color || nodeInfo.fallbackColor(id),
        ...(n.battery ? { battery: n.battery } : {}),
      };
    }
  } catch (e) {
    console.warn(`Couldn't load ${NODES_URL}, using the built-in node list:`, e);
  }
}
const DEFAULT_BATTERY = { type: 'batt', label: 'BATTERY' };
const NODE_FALLBACK_COLORS = ['#d2a8ff', '#ffa657', '#a5d6ff', '#7ee787'];

function nodeInfo(n) {
  const known = NODES[n];
  if (known) return known;
  return { name: `Node ${n}`, short: `N${n}`, color: nodeInfo.fallbackColor(n) };
}
nodeInfo.fallbackColor = n => NODE_FALLBACK_COLORS[(Number(n) || 0) % NODE_FALLBACK_COLORS.length];

// Configured nodes plus any others that turn up in the data, in ID order.
function knownNodeIds(extra = []) {
  const ids = new Set([...Object.keys(NODES).map(Number), ...extra.map(Number)]);
  return [...ids].filter(n => !Number.isNaN(n)).sort((a, b) => a - b);
}

// ── Unit preference (°C/°F) ─────────────────────────────────────────────────
// Single global flag, shared with analysis.js (loaded right after this file,
// same page scope — no module system here, so a plain global is simplest).
// Sensors always report Celsius; this only affects display. Conversion
// happens at render/trace-build time on already-fetched data, never at the
// query layer, so it's cheap arithmetic regardless of range length.
// Persisted the same way card layout prefs are (plain localStorage — this is
// the real deployed site, not an Artifact).

const UNIT_PREF_KEY = 'gardenDashboardUnitPref';

let useFahrenheit = (function () {
  try { return localStorage.getItem(UNIT_PREF_KEY) === 'F'; } catch (e) { return false; }
})();

function celsiusToFahrenheit(c) { return c * 9 / 5 + 32; }

// Always format for display through this — never read `useFahrenheit`
// directly when showing a value, so a missed spot can't silently show raw
// Celsius under an °F label.
function formatTemp(c) { return useFahrenheit ? celsiusToFahrenheit(c) : c; }

function tempUnitLabel() { return useFahrenheit ? '°F' : '°C'; }

function setUnitPref(pref) {
  useFahrenheit = pref === 'F';
  try { localStorage.setItem(UNIT_PREF_KEY, pref); } catch (e) { /* non-fatal */ }

  document.getElementById('unit-c-btn')?.classList.toggle('active', !useFahrenheit);
  document.getElementById('unit-f-btn')?.classList.toggle('active', useFahrenheit);

  // Re-render everything that shows a temperature under the new unit.
  if (currentPackets.length) renderAll(currentPackets);
  // analysis.js and weather.js, loaded right after this file — guarded in
  // case either hasn't initialized yet.
  if (typeof buildFieldPicker === 'function') buildFieldPicker();
  if (typeof runAnalysisPlot === 'function') runAnalysisPlot();
  if (typeof renderWeather === 'function' && lastWeatherData) renderWeather(lastWeatherData);
}

function initUnitToggle() {
  document.getElementById('unit-c-btn')?.classList.toggle('active', !useFahrenheit);
  document.getElementById('unit-f-btn')?.classList.toggle('active', useFahrenheit);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function soilPct(raw) {
  return Math.round(clamp((raw - 200) / 8.23, 0, 100));
}

// VOC: SGP40 raw resistance. Higher = cleaner. Typical range ~15000–50000.
// We invert to a 0-100 "pollution" scale for the gauge.
function vocPollution(raw) {
  const lo = 15000, hi = 50000;
  const pct = (raw - lo) / (hi - lo);
  return clamp(Math.round((1 - pct) * 100), 0, 100);
}

// Always called with the raw Celsius reading, never the display-converted
// value — the thresholds encode real thermal state, which doesn't change
// with the unit toggle.
function tempColor(t) {
  if (t < 10) return '#58a6ff';
  if (t < 25) return '#3fb950';
  if (t < 35) return '#d29922';
  return '#f85149';
}

function rhColor(rh) {
  if (rh < 30) return '#d29922';
  if (rh < 70) return '#3fb950';
  return '#58a6ff';
}

function battColor(soc) {
  if (soc > 50) return '#3fb950';
  if (soc > 20) return '#d29922';
  return '#f85149';
}

function luxLabel(lux) {
  if (lux < 10)   return 'dark';
  if (lux < 200)  return 'dim';
  if (lux < 1000) return 'indoor';
  if (lux < 5000) return 'bright';
  return 'direct sun';
}

function uviLabel(uvi) {
  if (uvi < 3) return { label: 'low', cls: 'badge-green' };
  if (uvi < 6) return { label: 'moderate', cls: 'badge-amber' };
  return { label: 'high', cls: 'badge-red' };
}

function vocLabel(poll) {
  if (poll < 30) return { label: 'good', cls: 'badge-green' };
  if (poll < 60) return { label: 'moderate', cls: 'badge-amber' };
  return { label: 'poor', cls: 'badge-red' };
}

function minutesAgo(isoTs) {
  if (!isoTs) return null;
  try {
    const then = new Date(isoTs.replace('T', ' '));
    const diff = (Date.now() - then.getTime()) / 1000;
    if (diff < 90)   return Math.round(diff) + 's ago';
    if (diff < 3600) return Math.round(diff / 60) + 'm ago';
    return Math.round(diff / 3600) + 'h ago';
  } catch (e) { return null; }
}

// ── Sparkline via Chart.js ────────────────────────────────────────────────────

const sparkCharts = {};

function renderSparkline(canvasId, values, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  if (sparkCharts[canvasId]) { sparkCharts[canvasId].destroy(); }
  const labels = values.map((_, i) => i);
  sparkCharts[canvasId] = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: color,
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.4,
        fill: true,
        backgroundColor: color + '18',
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { display: false },
        y: { display: false, grace: '10%' }
      },
      elements: { line: { borderCapStyle: 'round' } }
    }
  });
}

// ── SVG donut arc ─────────────────────────────────────────────────────────────

function donutArc(pct, color, bg) {
  const r = 34, cx = 40, cy = 40;
  const circ = 2 * Math.PI * r;
  const dash = (pct / 100) * circ;
  // The gap length must be exactly (circ - dash), not the full circumference.
  // With a nonzero stroke-dashoffset (used below to rotate the start point to
  // 12 o'clock), an oversized gap makes the dash+gap pattern longer than the
  // circle's own path length — the offset then "eats into" the dash segment,
  // so the visible fill comes out shorter than pct (e.g. only ~75% shown at
  // pct=99). Matching the gap to the dash's complement keeps the pattern's
  // period exactly equal to the circle's circumference, so the offset only
  // rotates where the arc starts, without truncating how much of it shows.
  return `<svg viewBox="0 0 80 80" width="80" height="80">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${bg}" stroke-width="7"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="7"
      stroke-dasharray="${dash.toFixed(1)} ${(circ - dash).toFixed(1)}"
      stroke-dashoffset="${(circ/4).toFixed(1)}"
      stroke-linecap="round" transform="rotate(-90 ${cx} ${cy})"/>
  </svg>`;
}

// ── Render functions ─────────────────────────────────────────────────────────
// Each takes the card element it draws into (one card per sensor per node —
// see ensureSensorCard below), so the same function renders every node's copy.

function cardBody(el)  { return el.querySelector('.card-body'); }
function cardBadge(el) { return el.querySelector('.card-badge'); }
function setBadge(el, cls, text) {
  const b = cardBadge(el);
  if (!b) return;
  b.className = 'card-badge ' + cls;
  b.textContent = text;
}

function renderBattery(el, latest, history) {
  const soc = latest.soc ?? 0;
  const v   = latest.v ?? 0;
  const color = battColor(soc);
  setBadge(el, soc > 50 ? 'badge-green' : soc > 20 ? 'badge-amber' : 'badge-red',
           soc > 50 ? 'good' : soc > 20 ? 'low' : 'critical');

  cardBody(el).innerHTML = `
    <div class="big-value" style="color:${color}">${soc.toFixed(1)}<span class="big-unit">%</span></div>
    <div class="sub-value">${v.toFixed(2)} V</div>
    <div class="bar-track">
      <div class="bar-fill" style="width:${soc}%;background:${color}"></div>
    </div>
    <div class="sparkline-row">
      <span class="sparkline-label">SoC history</span>
      <div style="position:relative;height:30px;flex:1"><canvas id="${el.id}-spark"></canvas></div>
    </div>`;

  if (history.length > 1) renderSparkline(`${el.id}-spark`, history, color);
}

function renderSHT(el, latest, tempHist, rhHist) {
  const tmpC = latest.tmp ?? 0;   // always Celsius as read from the sensor
  const rh   = latest.rh ?? 0;

  cardBody(el).innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div>
        <div class="mini-label">TEMP</div>
        <div class="big-value" style="color:${tempColor(tmpC)};font-size:30px">${formatTemp(tmpC).toFixed(1)}<span class="big-unit">${tempUnitLabel()}</span></div>
      </div>
      <div>
        <div class="mini-label">HUMIDITY</div>
        <div class="big-value" style="color:${rhColor(rh)};font-size:30px">${rh.toFixed(1)}<span class="big-unit">%</span></div>
      </div>
    </div>
    <div class="sparkline-row" style="margin-top:12px">
      <span class="sparkline-label">Temp</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="${el.id}-spark-tmp"></canvas></div>
    </div>
    <div class="sparkline-row">
      <span class="sparkline-label">RH</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="${el.id}-spark-rh"></canvas></div>
    </div>`;

  // tempColor is keyed off real thermal state, so it always takes the raw
  // Celsius reading — never the display-converted value — regardless of
  // which unit is currently shown. The sparkline is a shape-only visual (no
  // axis), so an affine unit conversion wouldn't change it; converting
  // anyway keeps the underlying values consistent with what's displayed.
  if (tempHist.length > 1) renderSparkline(`${el.id}-spark-tmp`, tempHist.map(formatTemp), tempColor(tmpC));
  if (rhHist.length > 1)   renderSparkline(`${el.id}-spark-rh`,  rhHist,  rhColor(rh));
}

function renderVOC(el, latest, history) {
  const raw  = latest.voc ?? 0;
  const poll = vocPollution(raw);
  const info = vocLabel(poll);
  setBadge(el, info.cls, info.label);

  const pct = (100 - poll); // pointer: 0% = poor (left), 100% = good (right)

  cardBody(el).innerHTML = `
    <div class="big-value">${raw.toLocaleString()}<span class="big-unit" style="font-size:13px"> raw</span></div>
    <div class="sub-value">higher = cleaner air</div>
    <div class="voc-gauge">
      <div class="voc-labels"><span>poor</span><span>moderate</span><span>good</span></div>
      <div class="voc-gradient-bar">
        <div class="voc-pointer" style="left:${pct}%"></div>
      </div>
    </div>
    <div class="sparkline-row" style="margin-top:8px">
      <span class="sparkline-label">VOC history</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="${el.id}-spark"></canvas></div>
    </div>`;

  if (history.length > 1) renderSparkline(`${el.id}-spark`, history, '#39d0c4');
}

function renderUV(el, latest, luxHist) {
  const lux    = latest.lux ?? 0;
  const uvi    = latest.uvi ?? 0;
  const uvRaw  = latest.uv ?? 0;
  const uvInfo = uviLabel(uvi);

  cardBody(el).innerHTML = `
    <div class="big-value" style="color:#d29922">${lux.toFixed(0)}<span class="big-unit">lux</span></div>
    <div class="sub-value">${luxLabel(lux)}</div>
    <div class="row2" style="margin-top:12px">
      <div class="mini-metric">
        <div class="mini-label">UV INDEX</div>
        <div class="mini-value" style="color:${uvi < 3 ? '#3fb950' : uvi < 6 ? '#d29922' : '#f85149'}">${uvi.toFixed(2)}</div>
      </div>
      <div class="mini-metric">
        <div class="mini-label">UV RAW</div>
        <div class="mini-value">${uvRaw}</div>
      </div>
    </div>
    <div class="sparkline-row" style="margin-top:12px">
      <span class="sparkline-label">Lux trend</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="${el.id}-spark"></canvas></div>
    </div>`;

  if (luxHist.length > 1) renderSparkline(`${el.id}-spark`, luxHist, '#d29922');
}

// Toggles whether a card spans 2 outer-grid columns (`.card-wide`) or just
// one — called from renderSoil/renderPower with the actual connected-sensor
// count so a card that's only using 1 of its possible slots doesn't keep
// reserving the same width it would need for a full set.
function setCardWide(el, wide) {
  el.classList.toggle('card-wide', wide);
}

function renderSoil(el, soilData) {
  const present = [0, 1, 2].filter(id => soilData[id]);

  // Wide only once all 3 slots are actually in use — 1 or 2 arcs fit
  // comfortably in a single-width card, matching how many columns the
  // inner grid is given below.
  setCardWide(el, present.length >= 3);

  if (!present.length) {
    cardBody(el).innerHTML = '<div class="no-data">No soil sensor data</div>';
    return;
  }

  let html = `<div class="soil-grid" style="grid-template-columns:repeat(${present.length}, 1fr)">`;
  for (const id of present) {
    const d = soilData[id];
    const pct = soilPct(d.m);
    const color = pct < 20 ? '#f85149' : pct < 40 ? '#d29922' : '#3fb950';
    html += `<div class="soil-card">
      <div class="soil-arc-wrap">
        ${donutArc(pct, color, '#1a2530')}
        <div class="soil-pct" style="color:${color}">${pct}%</div>
      </div>
      <div class="soil-label">SENSOR ${id}</div>
      <div class="soil-temp">${formatTemp(d.tmp).toFixed(1)}${tempUnitLabel()}</div>
    </div>`;
  }
  html += '</div>';

  cardBody(el).innerHTML = html;
}

// Power monitor node labels — edit these to match what each INA238 is actually measuring
const POWER_NODE_LABELS = {
  0: 'pw0 · monitor 0',
  1: 'pw1 · monitor 1',
  2: 'pw2 · monitor 2',
  3: 'pw3 · monitor 3',
};

function renderPower(el, powerData) {
  const present = [0, 1, 2, 3].filter(id => powerData[id]);

  if (!present.length) setBadge(el, 'badge-gray', 'no data');
  else setBadge(el, 'badge-blue', `${present.length} active`);

  // Wide as soon as there are 2+ nodes to show side by side — each node's
  // 3-metric row (V / mA / mW) needs more width than a single-column card
  // can spare once there's more than one of them.
  setCardWide(el, present.length >= 2);

  if (!present.length) {
    cardBody(el).innerHTML = '<div class="no-data">No power monitor data</div>';
    return;
  }

  // 1 node gets the full card width; 2+ split into 2 columns (wrapping to
  // additional rows for 3-4) rather than growing wider still.
  const cols = Math.min(present.length, 2);
  let html = `<div class="power-grid" style="grid-template-columns:repeat(${cols}, 1fr)">`;
  for (const id of present) {
    const d = powerData[id];
    const v  = (d.v  ?? 0).toFixed(2);
    const ma = (d.ma ?? 0).toFixed(0);
    const mw = (d.mw ?? 0).toFixed(0);
    // Color current: blue normally, amber if over 5A, red if over 9A
    const maNum = d.ma ?? 0;
    const maColor = maNum > 9000 ? '#f85149' : maNum > 5000 ? '#d29922' : '#58a6ff';
    html += `<div class="power-node">
      <div class="power-node-label">${POWER_NODE_LABELS[id]}</div>
      <div class="power-metrics">
        <div class="power-metric-inner">
          <div class="power-metric-val" style="color:#58a6ff">${v}</div>
          <div class="power-metric-unit">VOLTS</div>
        </div>
        <div class="power-metric-inner">
          <div class="power-metric-val" style="color:${maColor}">${ma}</div>
          <div class="power-metric-unit">mA</div>
        </div>
        <div class="power-metric-inner">
          <div class="power-metric-val" style="color:#bc8cff">${mw}</div>
          <div class="power-metric-unit">mW</div>
        </div>
      </div>
    </div>`;
  }
  html += '</div>';
  cardBody(el).innerHTML = html;
}

function renderHealth(el, packets) {
  const NON_SENSOR = new Set(['ts', 'sync_ack', 'sync']);
  const sensorPkts = packets.filter(p => !NON_SENSOR.has(p.t));
  const counts = {};
  for (const p of sensorPkts) counts[p.t] = (counts[p.t] || 0) + 1;

  const all = Object.keys(counts);
  const recentCutoff = Math.max(1, Math.floor(sensorPkts.length * 3 / 4));
  const recentTypes = new Set(sensorPkts.slice(recentCutoff).map(p => p.t));
  const missing = all.filter(t => !recentTypes.has(t));

  const ok = missing.length === 0 && all.length > 0;
  setBadge(el, ok ? 'badge-green' : all.length === 0 ? 'badge-gray' : 'badge-amber',
           ok ? 'all online' : all.length === 0 ? 'no data' : `${missing.length} absent`);

  if (all.length === 0) {
    cardBody(el).innerHTML = '<div class="no-data">No sensor packets found</div>';
    return;
  }

  const rows = all.sort().map(t => {
    const gone = missing.includes(t);
    const dot  = gone ? '#d29922' : '#3fb950';
    return `<div class="health-row">
      <div class="health-dot" style="background:${dot}"></div>
      <span class="health-name">${t}</span>
      <span class="health-count">${counts[t]}</span>
      ${gone ? '<span class="card-badge badge-amber" style="font-size:9px">absent</span>' : ''}
    </div>`;
  }).join('');

  cardBody(el).innerHTML = rows;
}

function renderSystem(el, rtLatest, lastTs, seqLatest) {
  let html = '';
  if (rtLatest) {
    html += `<div class="mini-metric" style="margin-bottom:8px">
      <div class="mini-label">RADIO MODULE TEMP</div>
      <div class="mini-value" style="color:${tempColor(rtLatest.tmp)}">${formatTemp(rtLatest.tmp).toFixed(1)} ${tempUnitLabel()}</div>
    </div>`;
  }
  if (lastTs) {
    const ago = minutesAgo(lastTs);
    html += `<div class="mini-metric" style="margin-bottom:8px">
      <div class="mini-label">LAST PACKET TIMESTAMP</div>
      <div class="mini-value" style="font-size:13px">${lastTs}</div>
      ${ago ? `<div class="sub-value" style="margin-top:4px">${ago}</div>` : ''}
    </div>`;
  }
  if (seqLatest !== null) {
    html += `<div class="mini-metric">
      <div class="mini-label">SEQUENCE NUMBER</div>
      <div class="mini-value">${seqLatest}</div>
    </div>`;
  }
  if (!html) html = '<div class="no-data">No data</div>';
  cardBody(el).innerHTML = html;
}

// ── Load + parse ──────────────────────────────────────────────────────────────

function withKnownTs(packets) {
  return packets.filter(p => p.ts && p.ts !== 'unknown');
}

async function loadData() {
  try {
    const resp = await fetch(`${API_BASE}/api/data?minutes=${DATA_WINDOW_MINUTES}`);
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    if (data.status !== 'ok') throw new Error(data.error || 'unknown API error');
    return withKnownTs(data.packets).slice(-WINDOW);
  } catch (e) {
    document.getElementById('node-status').innerHTML =
      '<span class="node-status-error">error: cannot load data</span>';
    return null;
  }
}

// Each node's last storage report from /api/node_info ({"<node>": {ub, fb,
// tb, at}}) — main.py refreshes it hourly. Kept from the last successful
// fetch if a later one fails, since it changes slowly anyway.
let nodeStorage = {};

async function loadNodeInfo() {
  try {
    const resp = await fetch(`${API_BASE}/api/node_info`);
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    if (data.status === 'ok') nodeStorage = data.nodes || {};
  } catch (e) {
    console.warn('[nodes] /api/node_info failed, keeping last storage report', e);
  }
}

function getLatest(packets, type) {
  for (let i = packets.length - 1; i >= 0; i--) {
    if (packets[i].t === type) return packets[i];
  }
  return null;
}

function getHistory(packets, type, key, n = 40) {
  const vals = packets.filter(p => p.t === type && key in p).map(p => p[key]);
  return vals.slice(-n);
}

function getLastTs(packets) {
  for (let i = packets.length - 1; i >= 0; i--) {
    if (packets[i].ts) return packets[i].ts;
  }
  return null;
}

// Every card below works on one node's packets at a time — the same sensor
// type (batt, sht, s0...) exists on more than one node, so mixing them would
// silently blend two different physical sensors into one reading.
function groupByNode(packets) {
  const byNode = {};
  for (const p of packets) {
    if (p.n == null) continue;
    (byNode[p.n] ||= []).push(p);
  }
  return byNode;
}

// ── Freshness ─────────────────────────────────────────────────────────────────
// Nodes are polled about once a minute, so a couple of minutes without a
// packet is still normal; ten minutes means something's wrong.

function freshness(lastTs) {
  if (!lastTs) return 'offline';
  const diff = (Date.now() - new Date(lastTs.replace('T', ' ')).getTime()) / 1000;
  if (Number.isNaN(diff)) return 'offline';
  if (diff < 150) return 'fresh';
  if (diff < 600) return 'stale';
  return 'offline';
}

// If a node is asleep (see SleepScheduler on the Pi), the time it wakes;
// otherwise null. A sleeping node has its radio off, so going quiet is
// expected rather than a fault.
function sleepUntil(n) {
  const s = nodeStorage[String(n)]?.sleep_until;
  return s && new Date(s.replace('T', ' ')).getTime() > Date.now() ? s : null;
}

function statusDot(state) {
  return `<span class="status-dot ${state === 'fresh' ? '' : state}"></span>`;
}

// One chip per node in the header: its own status dot and time since its
// last packet.
function renderNodeStatus(byNode) {
  document.getElementById('node-status').innerHTML = knownNodeIds(Object.keys(byNode)).map(n => {
    const info = nodeInfo(n);
    const lastTs = getLastTs(byNode[n] || []);
    const asleep = sleepUntil(n);
    const ago = lastTs ? (minutesAgo(lastTs) || lastTs) : 'no data';
    const text = asleep ? `asleep · wakes ${asleep.slice(11, 16)}` : ago;
    return `<span class="node-chip" style="--node-color:${info.color}"
                  title="${info.name} — last packet ${lastTs || 'none in the last 6 hours'}">
      ${statusDot(asleep ? 'asleep' : freshness(lastTs))}<span class="node-chip-name">${info.short}</span>${text}
    </span>`;
  }).join('');
}

function formatBytes(b) {
  if (b == null) return '–';
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(1)} GB`;
}

// A fuel gauge reports charge % as well as voltage; a power monitor reports
// voltage and the current flowing, which is all it can say about a battery.
function batteryReading(p) {
  if (!p) return '–';
  const v = `${(p.v ?? 0).toFixed(2)} V`;
  if (p.soc != null) return `${p.soc.toFixed(0)}% · ${v}`;
  if (p.ma != null) return `${v} · ${p.ma.toFixed(0)} mA`;
  return v;
}

// The "Nodes" card: per node, when it last reported, its battery, and how
// much logged data is waiting on it for the next sync.
function renderNodesCard(byNode) {
  const card = document.getElementById('card-nodes');
  if (!card) return;
  cardBody(card).innerHTML = knownNodeIds(Object.keys(byNode)).map(n => {
    const info = nodeInfo(n);
    const np = byNode[n] || [];
    const lastTs = getLastTs(np);
    const battery = info.battery || DEFAULT_BATTERY;
    const store = nodeStorage[String(n)];

    const metrics = [
      ['LAST PACKET', lastTs ? lastTs.replace('T', ' ') : 'none in 6h'],
      [battery.label, batteryReading(getLatest(np, battery.type))],
      ['WAITING TO SYNC', store ? formatBytes(store.ub) : '–'],
      ['STORAGE FREE', store && store.tb ? `${formatBytes(store.fb)} of ${formatBytes(store.tb)}` : store ? 'no storage' : '–'],
    ];
    const reported = store?.at ? `storage reported ${minutesAgo(store.at) || store.at}` : 'no storage report yet';
    const asleep = sleepUntil(n);
    const note = asleep
      ? `asleep until ${asleep.replace('T', ' ').slice(0, 16)} — logging to storage, radio off · ${reported}`
      : reported;

    return `<div class="node-row" style="--node-color:${info.color}">
      <div class="node-row-head">
        ${statusDot(asleep ? 'asleep' : freshness(lastTs))}
        <span class="node-row-name">${info.name}</span>
        <span class="node-row-ago">${lastTs ? minutesAgo(lastTs) || '' : ''}</span>
      </div>
      <div class="node-metrics">
        ${metrics.map(([label, value]) => `<div>
          <div class="mini-label">${label}</div>
          <div class="node-metric-value">${value}</div>
        </div>`).join('')}
      </div>
      <div class="node-row-note">${note}</div>
    </div>`;
  }).join('') || '<div class="no-data">No nodes yet</div>';
}

// ── Sensor cards ──────────────────────────────────────────────────────────────
// One card per sensor per node, created the first time that node reports it.
// `types` are the packet types the card covers (null = every node gets one);
// `badge` cards show a status badge, the rest a fixed chip label.

const SENSOR_CARDS = [
  { kind: 'batt',   title: 'Lipo',                   types: ['batt'],                     badge: true },
  { kind: 'sht',    title: 'Temperature & Humidity', types: ['sht'],                      label: 'SHT40' },
  { kind: 'voc',    title: 'Air Quality (VOC)',      types: ['voc'],                      badge: true },
  { kind: 'uv',     title: 'UV & Light',             types: ['uv'],                       label: 'LTR390' },
  { kind: 'soil',   title: 'Soil Sensors',           types: ['s0', 's1', 's2'],           label: 'Seesaw' },
  { kind: 'power',  title: 'Battery',                types: ['pw0', 'pw1', 'pw2', 'pw3'], badge: true },
  { kind: 'health', title: 'Sensor Health',          types: null,                         badge: true },
  { kind: 'system', title: 'Radio & System',         types: null,                         label: 'RFM69' },
];

function sensorCardId(kind, node) { return `card-${kind}-n${node}`; }

function ensureSensorCard(spec, node) {
  const id = sensorCardId(spec.kind, node);
  const existing = document.getElementById(id);
  if (existing) return existing;

  const info = nodeInfo(node);
  const el = document.createElement('div');
  el.className = 'card node-card';
  el.id = id;
  el.style.setProperty('--node-color', info.color);
  el.innerHTML = `
    <div class="card-header">
      <div class="card-heading">
        <span class="card-title">${spec.title}</span>
        <span class="node-tag">${info.name}</span>
      </div>
      ${spec.badge ? '<span class="card-badge badge-gray">–</span>' : `<span class="node-badge">${spec.label}</span>`}
    </div>
    <div class="card-body"><div class="no-data">No data</div></div>`;
  addCardToLayout(el);
  return el;
}

function renderSensorCard(el, kind, np) {
  switch (kind) {
    case 'batt':
      return renderBattery(el, getLatest(np, 'batt'), getHistory(np, 'batt', 'soc'));
    case 'sht':
      return renderSHT(el, getLatest(np, 'sht'), getHistory(np, 'sht', 'tmp'), getHistory(np, 'sht', 'rh'));
    case 'voc':
      return renderVOC(el, getLatest(np, 'voc'), getHistory(np, 'voc', 'voc'));
    case 'uv':
      return renderUV(el, getLatest(np, 'uv'), getHistory(np, 'uv', 'lux'));
    case 'soil': {
      const soilData = {};
      for (const id of [0, 1, 2]) {
        const s = getLatest(np, `s${id}`);
        if (s) soilData[id] = s;
      }
      return renderSoil(el, soilData);
    }
    case 'power': {
      const powerData = {};
      for (const id of [0, 1, 2, 3]) {
        const p = getLatest(np, `pw${id}`);
        if (p) powerData[id] = p;
      }
      return renderPower(el, powerData);
    }
    case 'health':
      return renderHealth(el, np);
    case 'system': {
      let seq = null;
      for (let i = np.length - 1; i >= 0; i--) if ('q' in np[i]) { seq = np[i].q; break; }
      return renderSystem(el, getLatest(np, 'rt'), getLastTs(np), seq);
    }
  }
}

// ── Main refresh ─────────────────────────────────────────────────────────────

// currentPackets holds the last-loaded window in memory so a fresh poll
// result (from triggerRefresh) can be merged straight in and re-rendered
// without waiting for the next /api/data refresh.
let currentPackets = [];

function renderAll(packets) {
  document.getElementById('packets-count').textContent = packets.length;

  const byNode = groupByNode(packets);
  renderNodeStatus(byNode);
  renderNodesCard(byNode);

  for (const node of Object.keys(byNode).map(Number).sort((a, b) => a - b)) {
    const np = byNode[node];
    for (const spec of SENSOR_CARDS) {
      if (spec.types && !np.some(p => spec.types.includes(p.t))) continue;
      renderSensorCard(ensureSensorCard(spec, node), spec.kind, np);
    }
  }
}

async function refresh() {
  const [packets] = await Promise.all([loadData(), loadNodeInfo()]);
  if (!packets) return;

  // /api/data reads sensors.db directly, so unlike the old static-file
  // fetch there's no GitHub/Pages propagation lag to guard against here —
  // whatever comes back is already current as of the query.
  currentPackets = packets;
  renderAll(currentPackets);
}

// ── Manual refresh (live poll via Flask API, falls back to static reload) ────

async function triggerRefresh() {
  const btn = document.getElementById('refresh-btn');
  const original = btn.textContent;
  btn.disabled = true;

  // If API_BASE hasn't been set up, there's nothing to poll — just reload.
  if (!API_BASE || API_BASE.includes('YOURDOMAIN')) {
    btn.textContent = '↺ refreshing...';
    await refresh();
    btn.textContent = original;
    btn.disabled = false;
    return;
  }

  btn.textContent = '↺ polling...';
  try {
    const resp = await fetch(`${API_BASE}/api/poll`, { method: 'POST' });
    const data = await resp.json();

    if (resp.status === 429) {
      const wait = Math.ceil(data.retry_after || 5);
      btn.textContent = `↺ wait ${wait}s`;
      setTimeout(() => { btn.textContent = original; btn.disabled = false; }, wait * 1000);
      return;
    }

    if (data.status === 'ok' && Array.isArray(data.packets) && data.packets.length) {
      // Real sensor values are already here — render them immediately.
      currentPackets = withKnownTs(currentPackets.concat(data.packets)).slice(-WINDOW);
      renderAll(currentPackets);
      btn.textContent = original;
      btn.disabled = false;
      return;
    }

    // Fallback: poll reported timeout or came back with no packets — reload
    // from /api/data after a short delay, in case the result just didn't
    // make it back in time.
    btn.textContent = '↺ syncing...';
    setTimeout(async () => {
      await refresh();
      btn.textContent = original;
      btn.disabled = false;
    }, 4000);
    return;
  } catch (e) {
    console.error('poll request failed', e);
    btn.textContent = '↺ error';
    setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2000);
    return;
  }
}

// ── Radio ping test (independent of refresh — result comes straight back in
//    the HTTP response) ──

async function runPingTest() {
  const btn = document.getElementById('ping-btn');
  const resultEl = document.getElementById('ping-result');
  const nodeSelect = document.getElementById('ping-node-select');
  const nodeId = nodeSelect ? parseInt(nodeSelect.value, 10) : 1;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⇄ pinging...';
  resultEl.textContent = '';

  if (!API_BASE || API_BASE.includes('YOURDOMAIN')) {
    resultEl.textContent = 'API not configured';
    resultEl.style.color = 'var(--red)';
    btn.textContent = original;
    btn.disabled = false;
    return;
  }

  let progressTimer = null;
  try {
    progressTimer = setInterval(async () => {
      try {
        const pResp = await fetch(`${API_BASE}/api/ping_progress`);
        const p = await pResp.json();
        if (p.status === 'running') {
          resultEl.style.color = 'var(--muted)';
          resultEl.textContent = `${p.hits}/${p.done} pong so far (of ${p.count})...`;
        }
      } catch (e) {
        // Non-fatal — just skip this tick, the final result below still lands.
      }
    }, 300);

    const resp = await fetch(`${API_BASE}/api/ping_test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_id: nodeId }),
    });
    const data = await resp.json();
    clearInterval(progressTimer);

    if (resp.status === 429) {
      const wait = Math.ceil(data.retry_after || 5);
      resultEl.textContent = `wait ${wait}s`;
      resultEl.style.color = 'var(--amber)';
    } else if (data.status !== 'ok') {
      resultEl.textContent = data.status === 'timeout' ? 'no response' : 'test failed';
      resultEl.style.color = 'var(--red)';
    } else {
      const pct = Math.round((data.hits / data.count) * 100);
      const color = pct === 100 ? 'var(--green)' : pct >= 70 ? 'var(--amber)' : 'var(--red)';
      const rttStr = data.avg_rtt_ms != null ? `${data.avg_rtt_ms}ms avg` : 'no pongs';
      resultEl.style.color = color;
      resultEl.textContent = `${data.hits}/${data.count} pong · ${rttStr}`;
    }
  } catch (e) {
    if (progressTimer) clearInterval(progressTimer);
    console.error('ping test failed', e);
    resultEl.textContent = 'error';
    resultEl.style.color = 'var(--red)';
  }

  btn.textContent = original;
  btn.disabled = false;
}
// ── Live card customization (hide/show + reorder) ──────────────────────────
// Real deployed site, not an Artifact, so localStorage is fair game here —
// persists per-browser across visits. Always on — no separate "customize
// mode" to step into first. Every card gets a drag handle (top-left) and a
// hide button (top-right), both low-opacity until hovered so they don't
// clutter normal viewing. Hidden cards disappear from the grid entirely and
// collapse into a small "hidden: ..." chip bar below it, so there's always
// a way back without needing a mode toggle. Separate from the analysis
// panel's Phase 2 "drag-and-drop multi-card" idea; this is only about the
// live sensor-reading cards at the top of the dashboard.
//
// Sensor cards are created as their node's data arrives (see
// ensureSensorCard), so the layout works from whatever cards are in the grid
// right now rather than a fixed list. A saved order can name cards that
// aren't on the page yet (a node that hasn't reported this session); they
// slot into place when they appear.

const STATIC_CARD_LABELS = { 'card-nodes': 'Nodes', 'card-weather': 'Weather Forecast' };
// v2: cards became per-node, so a layout saved under the old single-card IDs
// doesn't carry over.
const CARD_PREFS_KEY = 'gardenDashboardCardPrefs.v2';

function cardLabel(id) {
  if (STATIC_CARD_LABELS[id]) return STATIC_CARD_LABELS[id];
  const m = /^card-(\w+)-n(\d+)$/.exec(id);
  const spec = m && SENSOR_CARDS.find(s => s.kind === m[1]);
  return spec ? `${spec.title} · ${nodeInfo(m[2]).short}` : id;
}

function gridCards() {
  return Array.from(document.getElementById('grid').querySelectorAll(':scope > .card'));
}

function loadCardPrefs() {
  try {
    const raw = localStorage.getItem(CARD_PREFS_KEY);
    if (!raw) return { order: [], hidden: [] };
    const parsed = JSON.parse(raw);
    return {
      order:  Array.isArray(parsed.order)  ? parsed.order  : [],
      hidden: Array.isArray(parsed.hidden) ? parsed.hidden : [],
    };
  } catch (e) {
    console.warn('[cards] failed to load saved layout, using default', e);
    return { order: [], hidden: [] };
  }
}

function saveCardPrefs() {
  try {
    localStorage.setItem(CARD_PREFS_KEY, JSON.stringify(cardPrefs));
  } catch (e) {
    console.warn('[cards] failed to save layout', e);
  }
}

let cardPrefs = loadCardPrefs();

// Cards in the saved order first; any the saved order doesn't mention keep
// their current relative position after those.
function applyCardOrder() {
  const grid = document.getElementById('grid');
  const rank = id => { const i = cardPrefs.order.indexOf(id); return i < 0 ? Infinity : i; };
  gridCards()
    .map((el, pos) => ({ el, pos }))
    .sort((a, b) => (rank(a.el.id) - rank(b.el.id)) || (a.pos - b.pos))
    .forEach(({ el }) => grid.appendChild(el));
}

function applyCardVisibility() {
  gridCards().forEach(card => {
    card.style.display = cardPrefs.hidden.includes(card.id) ? 'none' : '';
  });
  renderHiddenCardsBar();
}

function renderHiddenCardsBar() {
  const bar = document.getElementById('hidden-cards-bar');
  if (!cardPrefs.hidden.length) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    return;
  }
  bar.style.display = 'flex';
  bar.innerHTML = '<span>hidden:</span>' + cardPrefs.hidden.map(id => `
    <span class="hidden-card-chip">${cardLabel(id)}
      <button type="button" data-restore="${id}">show</button>
    </span>`).join('');
  bar.querySelectorAll('[data-restore]').forEach(btn => {
    btn.addEventListener('click', () => toggleCardHidden(btn.dataset.restore));
  });
}

function toggleCardHidden(id) {
  const idx = cardPrefs.hidden.indexOf(id);
  if (idx >= 0) cardPrefs.hidden.splice(idx, 1); else cardPrefs.hidden.push(id);
  saveCardPrefs();
  applyCardVisibility();
}

function buildCardControls(card) {
  if (card.querySelector('.card-controls')) return; // built once, reused

  const handle = document.createElement('div');
  handle.className = 'card-drag-handle';
  handle.textContent = '⠿';
  handle.title = 'Drag to reorder';
  // Native drag-and-drop drags the whole element it's set on; arming
  // `draggable` only while the handle is actively pressed keeps the rest
  // of the card (text, values) normally selectable the rest of the time.
  handle.addEventListener('mousedown', () => { card.draggable = true; });
  card.appendChild(handle);

  const ctrl = document.createElement('div');
  ctrl.className = 'card-controls';
  ctrl.innerHTML = `<button type="button" class="card-hide-btn" title="Hide this card">hide</button>`;
  ctrl.querySelector('button').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleCardHidden(card.id);
  });
  card.appendChild(ctrl);

  card.draggable = false;
}

// A newly created sensor card goes in just ahead of the weather card by
// default, then picks up its saved position and hidden state, if any.
function addCardToLayout(card) {
  const grid = document.getElementById('grid');
  const weather = document.getElementById('card-weather');
  grid.insertBefore(card, weather && weather.parentNode === grid ? weather : null);
  buildCardControls(card);
  applyCardOrder();
  applyCardVisibility();
}

// Native HTML5 drag-and-drop, scoped to #grid, always active (arming happens
// per-drag via the handle's mousedown above).
let dragSrcId = null;

function initCardDragAndDrop() {
  const grid = document.getElementById('grid');

  grid.addEventListener('dragstart', (e) => {
    const card = e.target.closest('.card');
    if (!card || !card.draggable) return;
    dragSrcId = card.id;
    card.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
  });

  grid.addEventListener('dragover', (e) => {
    if (!dragSrcId) return;
    e.preventDefault();
    const card = e.target.closest('.card');
    if (!card || card.id === dragSrcId) return;
    const dragEl = document.getElementById(dragSrcId);
    if (!dragEl) return;
    const rect = card.getBoundingClientRect();
    const before = (e.clientY - rect.top) < rect.height / 2;
    grid.insertBefore(dragEl, before ? card : card.nextSibling);
  });

  grid.addEventListener('dragend', (e) => {
    const card = e.target.closest('.card');
    if (card) { card.classList.remove('dragging'); card.draggable = false; }
    if (dragSrcId) {
      // Keep saved positions of cards that aren't on the page right now,
      // after the ones that are.
      const onPage = gridCards().map(el => el.id);
      cardPrefs.order = onPage.concat(cardPrefs.order.filter(id => !onPage.includes(id)));
      saveCardPrefs();
    }
    dragSrcId = null;
  });

  // If the mouse is pressed on a handle and released without a drag ever
  // starting (a click, essentially), un-arm draggable so it doesn't linger.
  document.addEventListener('mouseup', () => {
    if (dragSrcId) return; // an actual drag is in progress; dragend will handle it
    gridCards().forEach(card => { card.draggable = false; });
  });
}

function initCardCustomization() {
  gridCards().forEach(buildCardControls);
  applyCardOrder();
  applyCardVisibility();
  initCardDragAndDrop();
}

// The ping test's node picker, built from NODES so a new node shows up there too.
function buildPingNodeSelect() {
  const sel = document.getElementById('ping-node-select');
  if (!sel) return;
  sel.innerHTML = knownNodeIds().map(n => `<option value="${n}">${nodeInfo(n).name}</option>`).join('');
}
