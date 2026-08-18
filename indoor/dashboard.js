const DATA_WINDOW_MINUTES = 360;  // how much history /api/data returns (6 hours)
const WINDOW = 2000;              // cap on packets kept in memory (used when triggerRefresh merges a manual poll in)
const REFRESH_MS = 10000;

// Cloudflare Tunnel + domain — dashboard reads live from sensors.db via /api/data.
const API_BASE = "https://api.fletchermeyers.com";

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
  return `<svg viewBox="0 0 80 80" width="80" height="80">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${bg}" stroke-width="7"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="7"
      stroke-dasharray="${dash.toFixed(1)} ${circ.toFixed(1)}"
      stroke-dashoffset="${(circ/4).toFixed(1)}"
      stroke-linecap="round" transform="rotate(-90 ${cx} ${cy})"/>
  </svg>`;
}

// ── Render functions ─────────────────────────────────────────────────────────

function renderBattery(latest, history) {
  const soc = latest.soc ?? 0;
  const v   = latest.v ?? 0;
  const color = battColor(soc);
  const badge = soc > 50 ? 'badge-green' : soc > 20 ? 'badge-amber' : 'badge-red';
  document.getElementById('batt-badge').className = 'card-badge ' + badge;
  document.getElementById('batt-badge').textContent = soc > 50 ? 'good' : soc > 20 ? 'low' : 'critical';

  document.getElementById('batt-body').innerHTML = `
    <div class="big-value" style="color:${color}">${soc.toFixed(1)}<span class="big-unit">%</span></div>
    <div class="sub-value">${v.toFixed(2)} V</div>
    <div class="bar-track">
      <div class="bar-fill" style="width:${soc}%;background:${color}"></div>
    </div>
    <div class="sparkline-row">
      <span class="sparkline-label">SoC history</span>
      <div style="position:relative;height:30px;flex:1"><canvas id="spark-batt"></canvas></div>
    </div>`;

  if (history.length > 1) renderSparkline('spark-batt', history, color);
}

function renderSHT(latest, tempHist, rhHist) {
  const tmp = latest.tmp ?? 0;
  const rh  = latest.rh ?? 0;

  document.getElementById('sht-body').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div>
        <div class="mini-label">TEMP</div>
        <div class="big-value" style="color:${tempColor(tmp)};font-size:30px">${tmp.toFixed(1)}<span class="big-unit">°C</span></div>
      </div>
      <div>
        <div class="mini-label">HUMIDITY</div>
        <div class="big-value" style="color:${rhColor(rh)};font-size:30px">${rh.toFixed(1)}<span class="big-unit">%</span></div>
      </div>
    </div>
    <div class="sparkline-row" style="margin-top:12px">
      <span class="sparkline-label">Temp</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="spark-tmp"></canvas></div>
    </div>
    <div class="sparkline-row">
      <span class="sparkline-label">RH</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="spark-rh"></canvas></div>
    </div>`;

  if (tempHist.length > 1) renderSparkline('spark-tmp', tempHist, tempColor(tmp));
  if (rhHist.length > 1)   renderSparkline('spark-rh',  rhHist,  rhColor(rh));
}

function renderVOC(latest, history) {
  const raw  = latest.voc ?? 0;
  const poll = vocPollution(raw);
  const info = vocLabel(poll);
  document.getElementById('voc-badge').className = 'card-badge ' + info.cls;
  document.getElementById('voc-badge').textContent = info.label;

  const pct = (100 - poll); // pointer: 0% = poor (left), 100% = good (right)

  document.getElementById('voc-body').innerHTML = `
    <div class="big-value">${raw.toLocaleString()}<span class="big-unit" style="font-size:13px"> raw</span></div>
    <div class="sub-value">higher = cleaner air</div>
    <div class="voc-gauge">
      <div class="voc-labels"><span>poor</span><span>moderate</span><span>good</span></div>
      <div class="voc-gradient-bar">
        <div class="voc-pointer" id="voc-ptr" style="left:${pct}%"></div>
      </div>
    </div>
    <div class="sparkline-row" style="margin-top:8px">
      <span class="sparkline-label">VOC history</span>
      <div style="position:relative;height:28px;flex:1"><canvas id="spark-voc"></canvas></div>
    </div>`;

  if (history.length > 1) renderSparkline('spark-voc', history, '#39d0c4');
}

function renderUV(latest, luxHist) {
  const lux    = latest.lux ?? 0;
  const uvi    = latest.uvi ?? 0;
  const uvRaw  = latest.uv ?? 0;
  const uvInfo = uviLabel(uvi);

  document.getElementById('uv-body').innerHTML = `
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
      <div style="position:relative;height:28px;flex:1"><canvas id="spark-lux"></canvas></div>
    </div>`;

  if (luxHist.length > 1) renderSparkline('spark-lux', luxHist, '#d29922');
}

function renderSoil(soilData) {
  const ids = [0, 1, 2];
  let html = '<div class="soil-grid">';

  for (const id of ids) {
    const d = soilData[id];
    if (!d) {
      html += `<div class="soil-card">
        <div class="soil-arc-wrap">${donutArc(0, '#30363d', '#21262d')}<div class="soil-pct" style="color:var(--muted)">–</div></div>
        <div class="soil-label">S${id}</div>
        <div class="soil-temp" style="color:var(--border)">no data</div>
      </div>`;
      continue;
    }
    const pct = soilPct(d.m);
    const color = pct < 20 ? '#f85149' : pct < 40 ? '#d29922' : '#3fb950';
    html += `<div class="soil-card">
      <div class="soil-arc-wrap">
        ${donutArc(pct, color, '#1a2530')}
        <div class="soil-pct" style="color:${color}">${pct}%</div>
      </div>
      <div class="soil-label">SENSOR ${id}</div>
      <div class="soil-temp">${d.tmp.toFixed(1)}°C</div>
    </div>`;
  }

  html += '</div>';

  const anyData = ids.some(id => soilData[id]);
  if (!anyData) {
    document.getElementById('soil-body').innerHTML = '<div class="no-data">No soil sensor data</div>';
  } else {
    document.getElementById('soil-body').innerHTML = html;
  }
}

// Power monitor node labels — edit these to match what each INA238 is actually measuring
const POWER_NODE_LABELS = {
  0: 'pw0 · monitor 0',
  1: 'pw1 · monitor 1',
  2: 'pw2 · monitor 2',
  3: 'pw3 · monitor 3',
};

function renderPower(powerData) {
  const ids = [0, 1, 2, 3];
  const anyData = ids.some(id => powerData[id]);

  // Badge: count how many nodes have data
  const activeCount = ids.filter(id => powerData[id]).length;
  const badge = document.getElementById('power-badge');
  if (activeCount === 0) {
    badge.className = 'card-badge badge-gray';
    badge.textContent = 'no data';
  } else {
    badge.className = 'card-badge badge-blue';
    badge.textContent = `${activeCount} active`;
  }

  if (!anyData) {
    document.getElementById('power-body').innerHTML = '<div class="no-data">No power monitor data</div>';
    return;
  }

  let html = '<div class="power-grid">';
  for (const id of ids) {
    const d = powerData[id];
    if (!d) {
      html += `<div class="power-node no-data-node">
        <div class="power-node-label">${POWER_NODE_LABELS[id]}</div>
        <div class="no-data" style="padding:4px 0;font-size:11px">not connected</div>
      </div>`;
      continue;
    }
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
  document.getElementById('power-body').innerHTML = html;
}

function renderHealth(packets) {
  const NON_SENSOR = new Set(['ts', 'sync_ack', 'sync']);
  const sensorPkts = packets.filter(p => !NON_SENSOR.has(p.t));
  const counts = {};
  for (const p of sensorPkts) counts[p.t] = (counts[p.t] || 0) + 1;

  const all = Object.keys(counts);
  const recentCutoff = Math.max(1, Math.floor(sensorPkts.length * 3 / 4));
  const recentTypes = new Set(sensorPkts.slice(recentCutoff).map(p => p.t));
  const missing = all.filter(t => !recentTypes.has(t));

  const ok = missing.length === 0 && all.length > 0;
  document.getElementById('health-badge').className = 'card-badge ' + (ok ? 'badge-green' : all.length === 0 ? 'badge-gray' : 'badge-amber');
  document.getElementById('health-badge').textContent = ok ? 'all online' : all.length === 0 ? 'no data' : `${missing.length} absent`;

  if (all.length === 0) {
    document.getElementById('health-body').innerHTML = '<div class="no-data">No sensor packets found</div>';
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

  document.getElementById('health-body').innerHTML = rows;
}

function renderSystem(rtLatest, lastTs, seqLatest) {
  let html = '';
  if (rtLatest) {
    html += `<div class="mini-metric" style="margin-bottom:8px">
      <div class="mini-label">RADIO MODULE TEMP</div>
      <div class="mini-value" style="color:${tempColor(rtLatest.tmp)}">${rtLatest.tmp.toFixed(1)} °C</div>
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
  document.getElementById('system-body').innerHTML = html;
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
    document.getElementById('last-update').textContent = 'error: cannot load data';
    return null;
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

// ── Freshness check ───────────────────────────────────────────────────────────

function updateStatusDot(lastTs) {
  const dot = document.getElementById('status-dot');
  if (!lastTs) { dot.className = 'status-dot offline'; return; }
  try {
    const diff = (Date.now() - new Date(lastTs.replace('T', ' ')).getTime()) / 1000;
    if (diff < 60)    dot.className = 'status-dot';
    else if (diff < 600) dot.className = 'status-dot stale';
    else dot.className = 'status-dot offline';
  } catch { dot.className = 'status-dot offline'; }
}

// ── Main refresh ─────────────────────────────────────────────────────────────

// currentPackets holds the last-loaded window in memory so a fresh poll
// result (from triggerRefresh) can be merged straight in and re-rendered
// without waiting on a full re-fetch of the static file.
let currentPackets = [];

function getLastTs(packets) {
  for (let i = packets.length - 1; i >= 0; i--) {
    if (packets[i].ts) return packets[i].ts;
  }
  return null;
}

function renderAll(packets) {
  document.getElementById('packets-count').textContent = packets.length;

  // Pull latest timestamp from any packet that has one
  let lastTs = getLastTs(packets);

  updateStatusDot(lastTs);
  document.getElementById('last-update').textContent = lastTs ? (minutesAgo(lastTs) || lastTs) : 'unknown';

  // Battery
  const batt = getLatest(packets, 'batt');
  if (batt) renderBattery(batt, getHistory(packets, 'batt', 'soc'));

  // SHT40
  const sht = getLatest(packets, 'sht');
  if (sht) renderSHT(sht, getHistory(packets, 'sht', 'tmp'), getHistory(packets, 'sht', 'rh'));

  // SGP40 VOC
  const voc = getLatest(packets, 'voc');
  if (voc) renderVOC(voc, getHistory(packets, 'voc', 'voc'));

  // UV
  const uv = getLatest(packets, 'uv');
  if (uv) renderUV(uv, getHistory(packets, 'uv', 'lux'));

  // Soil sensors
  const soilData = {};
  for (const id of [0, 1, 2]) {
    const s = getLatest(packets, `s${id}`);
    if (s) soilData[id] = s;
  }
  renderSoil(soilData);

  // Power monitors
  const powerData = {};
  for (const id of [0, 1, 2, 3]) {
    const p = getLatest(packets, `pw${id}`);
    if (p) powerData[id] = p;
  }
  renderPower(powerData);

  // Health
  renderHealth(packets);

  // System / radio temp
  const rt  = getLatest(packets, 'rt');
  const anySeq = (() => { for (let i = packets.length - 1; i >= 0; i--) if ('q' in packets[i]) return packets[i].q; return null; })();
  renderSystem(rt, lastTs, anySeq);
}

async function refresh() {
  const packets = await loadData();
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

  // If API_BASE hasn't been set up yet, just re-fetch the static file like before.
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
      // Real sensor values are already here — render immediately rather than
      // waiting on push_data.sh -> GitHub -> Pages to publish the static file.
      // push_data.sh still runs (server-side, in the background) so the
      // static file and archive stay current for passive viewers, but this
      // click doesn't wait on any of that.
      currentPackets = withKnownTs(currentPackets.concat(data.packets)).slice(-WINDOW);
      renderAll(currentPackets);
      btn.textContent = original;
      btn.disabled = false;
      return;
    }

    // Fallback: poll reported timeout or came back with no packets — fall
    // back to the old path of re-fetching the static file after a short
    // delay, in case the result just didn't make it back in time.
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
//    the HTTP response, so it doesn't wait on push_data.sh/GitHub Pages at all) ──

async function runPingTest() {
  const btn = document.getElementById('ping-btn');
  const resultEl = document.getElementById('ping-result');
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
    // Poll progress every 300ms while the test runs in the background on
    // the Pi, so a slow/flaky connection shows "x/y pong..." live instead
    // of a blank button for the whole test.
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

    const resp = await fetch(`${API_BASE}/api/ping_test`, { method: 'POST' });
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

const CARD_IDS = ['card-batt', 'card-sht', 'card-voc', 'card-uv', 'card-soil', 'card-power', 'card-health', 'card-system'];
const CARD_LABELS = {
  'card-batt':   'Lipo',
  'card-sht':    'Temperature & Humidity',
  'card-voc':    'Air Quality (VOC)',
  'card-uv':     'UV & Light',
  'card-soil':   'Soil Sensors',
  'card-power':  'Battery',
  'card-health': 'Sensor Health',
  'card-system': 'Radio & System',
};
const CARD_PREFS_KEY = 'gardenDashboardCardPrefs';

function loadCardPrefs() {
  try {
    const raw = localStorage.getItem(CARD_PREFS_KEY);
    if (!raw) return { order: CARD_IDS.slice(), hidden: [] };
    const parsed = JSON.parse(raw);
    const savedOrder = Array.isArray(parsed.order) ? parsed.order.filter(id => CARD_IDS.includes(id)) : [];
    // Any card not in the saved order (e.g. added after prefs were saved) gets appended.
    CARD_IDS.forEach(id => { if (!savedOrder.includes(id)) savedOrder.push(id); });
    return { order: savedOrder, hidden: Array.isArray(parsed.hidden) ? parsed.hidden : [] };
  } catch (e) {
    console.warn('[cards] failed to load saved layout, using default', e);
    return { order: CARD_IDS.slice(), hidden: [] };
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

function applyCardOrder() {
  const grid = document.getElementById('grid');
  cardPrefs.order.forEach(id => {
    const el = document.getElementById(id);
    if (el) grid.appendChild(el); // moves to end in this order, so final DOM order == cardPrefs.order
  });
}

function applyCardVisibility() {
  CARD_IDS.forEach(id => {
    const card = document.getElementById(id);
    if (card) card.style.display = cardPrefs.hidden.includes(id) ? 'none' : '';
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
    <span class="hidden-card-chip">${CARD_LABELS[id] || id}
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

function buildCardControls() {
  CARD_IDS.forEach(id => {
    const card = document.getElementById(id);
    if (!card || card.querySelector('.card-controls')) return; // built once, reused

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
      toggleCardHidden(id);
    });
    card.appendChild(ctrl);

    card.draggable = false;
  });
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
      cardPrefs.order = Array.from(grid.querySelectorAll('.card')).map(el => el.id).filter(id => CARD_IDS.includes(id));
      saveCardPrefs();
    }
    dragSrcId = null;
  });

  // If the mouse is pressed on a handle and released without a drag ever
  // starting (a click, essentially), un-arm draggable so it doesn't linger.
  document.addEventListener('mouseup', () => {
    if (dragSrcId) return; // an actual drag is in progress; dragend will handle it
    CARD_IDS.forEach(id => {
      const card = document.getElementById(id);
      if (card) card.draggable = false;
    });
  });
}

function initCardCustomization() {
  applyCardOrder();
  buildCardControls();
  applyCardVisibility();
  initCardDragAndDrop();
}