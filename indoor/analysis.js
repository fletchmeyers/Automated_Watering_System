// ── Analysis panel ───────────────────────────────────────────────────────────
// Separate from the live cards above: no auto-refresh loop, queries /api/data
// directly with sensor_type + start/end (already supported since the DB
// migration's step 4 — no backend changes needed for this). Two modes:
// time-series (one or more sensor+field lines) and scatter (two sensors
// matched by nearest timestamp, same idea as explore.py's hourly resample
// join, just finer-grained and done client-side).

const ANALYSIS_FIELDS = [
  { id: 's0.m',     type: 's0',   key: 'm',   label: 'Soil 0 · Moisture' },
  { id: 's0.tmp',   type: 's0',   key: 'tmp', label: 'Soil 0 · Temp' },
  { id: 's1.m',     type: 's1',   key: 'm',   label: 'Soil 1 · Moisture' },
  { id: 's1.tmp',   type: 's1',   key: 'tmp', label: 'Soil 1 · Temp' },
  { id: 's2.m',     type: 's2',   key: 'm',   label: 'Soil 2 · Moisture' },
  { id: 's2.tmp',   type: 's2',   key: 'tmp', label: 'Soil 2 · Temp' },
  { id: 'sht.tmp',  type: 'sht',  key: 'tmp', label: 'Ambient Temp' },
  { id: 'sht.rh',   type: 'sht',  key: 'rh',  label: 'Ambient Humidity' },
  { id: 'uv.lux',   type: 'uv',   key: 'lux', label: 'Light (lux)' },
  { id: 'uv.uvi',   type: 'uv',   key: 'uvi', label: 'UV Index' },
  { id: 'voc.voc',  type: 'voc',  key: 'voc', label: 'VOC (raw)' },
  { id: 'batt.soc', type: 'batt', key: 'soc', label: 'Lipo %' },
  { id: 'batt.v',   type: 'batt', key: 'v',   label: 'Lipo Voltage' },
  { id: 'rt.tmp',   type: 'rt',   key: 'tmp', label: 'Radio Temp' },
  { id: 'pw0.ma',   type: 'pw0',  key: 'ma',  label: 'Battery 0 · Current' },
  { id: 'pw0.v',    type: 'pw0',  key: 'v',   label: 'Battery 0 · Voltage' },
  { id: 'pw0.mw',   type: 'pw0',  key: 'mw',  label: 'Battery 0 · Wattage' },
  { id: 'pw1.ma',   type: 'pw1',  key: 'ma',  label: 'Battery 1 · Current' },
  { id: 'pw1.v',    type: 'pw1',  key: 'v',   label: 'Battery 1 · Voltage' },
  { id: 'pw1.mw',   type: 'pw1',  key: 'mw',  label: 'Battery 1 · Wattage' },
  { id: 'pw2.ma',   type: 'pw2',  key: 'ma',  label: 'Battery 2 · Current' },
  { id: 'pw2.v',    type: 'pw2',  key: 'v',   label: 'Battery 2 · Voltage' },
  { id: 'pw2.mw',   type: 'pw2',  key: 'mw',  label: 'Battery 2 · Wattage' },
  { id: 'pw3.ma',   type: 'pw3',  key: 'ma',  label: 'Battery 3 · Current' },
  { id: 'pw3.v',    type: 'pw3',  key: 'v',   label: 'Battery 3 · Voltage' },
  { id: 'pw3.mw',   type: 'pw3',  key: 'mw',  label: 'Battery 3 · Wattage' },
];

const FIELD_COLORS = ['#39d0c4', '#58a6ff', '#bc8cff', '#d29922', '#3fb950', '#f85149', '#8b949e', '#56d364'];

function fieldById(id) { return ANALYSIS_FIELDS.find(f => f.id === id); }
function fieldColor(f) { return FIELD_COLORS[ANALYSIS_FIELDS.indexOf(f) % FIELD_COLORS.length]; }
// Color is keyed off position in the *full* ANALYSIS_FIELDS list (not the
// filtered/available one) so a field's color stays stable regardless of
// which other fields currently have data.

// Which (sensor_type, key) pairs have ever actually logged a row — used to
// hide checkboxes/dropdown options for sensors that were never wired up
// (e.g. pw3). null = not yet fetched, or the fetch failed; in that case we
// fail open and show every field rather than hiding things incorrectly.
let availableFieldKeys = null;

async function fetchAvailableFields() {
  try {
    const resp = await fetch(`${API_BASE}/api/available_fields`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.status !== 'ok') throw new Error(data.error || 'unknown API error');
    availableFieldKeys = new Set(data.fields.map(f => `${f.sensor_type}.${f.key}`));
  } catch (e) {
    console.warn('[analysis] /api/available_fields failed, showing all fields', e);
    availableFieldKeys = null;
  }
}

function visibleAnalysisFields() {
  if (!availableFieldKeys) return ANALYSIS_FIELDS; // fail open
  return ANALYSIS_FIELDS.filter(f => availableFieldKeys.has(`${f.type}.${f.key}`));
}

let analysisMode = 'timeseries';
// Default selection chosen for the exact soil s0/s2 gap this panel exists to help chase.
const tsSelectedFields = new Set(['s0.m', 's2.m']);

function setAnalysisMode(mode) {
  analysisMode = mode;
  document.getElementById('mode-timeseries-btn').classList.toggle('active', mode === 'timeseries');
  document.getElementById('mode-scatter-btn').classList.toggle('active', mode === 'scatter');
  document.getElementById('ts-field-picker').style.display = mode === 'timeseries' ? 'flex' : 'none';
  document.getElementById('scatter-field-picker').style.display = mode === 'scatter' ? 'flex' : 'none';
}

function toggleField(id, checked) {
  if (checked) tsSelectedFields.add(id); else tsSelectedFields.delete(id);
  const chip = document.querySelector(`.field-chip[data-field="${id}"]`);
  if (chip) chip.classList.toggle('checked', checked);
}

function buildFieldPicker() {
  const fields = visibleAnalysisFields();
  // A field that was checked (e.g. from a saved default) but turns out to have
  // no data shouldn't silently stay "selected" with no checkbox to uncheck it.
  [...tsSelectedFields].forEach(id => { if (!fields.some(f => f.id === id)) tsSelectedFields.delete(id); });

  const wrap = document.getElementById('ts-field-picker');
  wrap.innerHTML = fields.map(f => {
    const checked = tsSelectedFields.has(f.id);
    return `<label class="field-chip${checked ? ' checked' : ''}" style="--field-color:${fieldColor(f)}" data-field="${f.id}">
      <input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleField('${f.id}', this.checked)">
      <span class="swatch"></span>${f.label}
    </label>`;
  }).join('');

  const opts = fields.map(f => `<option value="${f.id}">${f.label}</option>`).join('');
  const xSel = document.getElementById('scatter-x-select');
  const ySel = document.getElementById('scatter-y-select');
  xSel.innerHTML = opts;
  ySel.innerHTML = opts;
  // Defaults toward the "soil moisture vs UV" comparison that motivated
  // scatter mode in the first place — fall back to whatever's first/second
  // available if either of those specific fields has no data.
  xSel.value = fields.some(f => f.id === 's2.m') ? 's2.m' : (fields[0]?.id ?? '');
  ySel.value = fields.some(f => f.id === 'uv.lux') ? 'uv.lux' : (fields[1]?.id ?? fields[0]?.id ?? '');
}

// ── Time range: presets + custom, same fetch either way ───────────────────────

const PRESETS = [
  { label: '1h',  minutes: 60 },
  { label: '6h',  minutes: 360 },
  { label: '24h', minutes: 1440 },
  { label: '3d',  minutes: 4320 },
  { label: '7d',  minutes: 10080 },
  { label: '30d', minutes: 43200 },
];

function toLocalInputValue(date) {
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function applyPreset(minutes, btn) {
  const end = new Date();
  const start = new Date(end.getTime() - minutes * 60000);
  document.getElementById('range-start').value = toLocalInputValue(start);
  document.getElementById('range-end').value = toLocalInputValue(end);
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

function buildPresetButtons() {
  const wrap = document.getElementById('preset-btns');
  wrap.innerHTML = PRESETS.map(p =>
    `<button type="button" class="preset-btn" data-minutes="${p.minutes}">${p.label}</button>`
  ).join('');
  wrap.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => applyPreset(parseInt(btn.dataset.minutes, 10), btn));
  });
}

// datetime-local inputs give "YYYY-MM-DDTHH:MM" with no seconds/timezone —
// treated as local time throughout, matching the Pi's ts strings (already
// local per the earlier `datetime()` + 'localtime' DB fix).
function inputToIso(value) {
  if (!value) return null;
  return value.length === 16 ? `${value}:00` : value;
}

// ── Data fetching ─────────────────────────────────────────────────────────────

async function fetchSeries(sensorType, start, end) {
  const params = new URLSearchParams({ sensor_type: sensorType });
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  const resp = await fetch(`${API_BASE}/api/data?${params.toString()}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  if (data.status !== 'ok') throw new Error(data.error || 'unknown API error');
  return withKnownTs(data.packets); // same "unknown"-ts filter as the live cards
}

function extractPoints(packets, key) {
  // packets already ts-ascending, courtesy of /api/data's ORDER BY ts ASC.
  const xs = [], ys = [];
  for (const p of packets) {
    if (key in p) {
      xs.push(p.ts.replace('T', ' '));
      ys.push(p[key]);
    }
  }
  return { xs, ys };
}

// Nearest-timestamp alignment for scatter mode — same idea as explore.py's
// hourly resample-and-join, just finer-grained and done client-side. Two
// sorted series, walk B's pointer forward only as long as doing so gets
// closer to A's current timestamp; skip pairs beyond a sanity tolerance.
function nearestJoin(aXs, aYs, bXs, bYs, toleranceMs = 15 * 60 * 1000) {
  const bTimes = bXs.map(t => new Date(t).getTime());
  const pairedX = [], pairedY = [];
  let bi = 0;
  for (let i = 0; i < aXs.length; i++) {
    const at = new Date(aXs[i]).getTime();
    while (bi < bTimes.length - 1 && Math.abs(bTimes[bi + 1] - at) <= Math.abs(bTimes[bi] - at)) bi++;
    if (bTimes.length && Math.abs(bTimes[bi] - at) <= toleranceMs) {
      pairedX.push(aYs[i]);
      pairedY.push(bYs[bi]);
    }
  }
  return { pairedX, pairedY };
}

// ── Plotting ──────────────────────────────────────────────────────────────────

const PLOTLY_LAYOUT_BASE = {
  paper_bgcolor: 'transparent',
  plot_bgcolor: 'transparent',
  font: { family: 'IBM Plex Mono, monospace', size: 11, color: '#8b949e' },
  margin: { l: 50, r: 20, t: 20, b: 40 },
  xaxis: { gridcolor: '#21262d', linecolor: '#30363d', zerolinecolor: '#30363d' },
  yaxis: { gridcolor: '#21262d', linecolor: '#30363d', zerolinecolor: '#30363d' },
  legend: { orientation: 'h', y: -0.2 },
};

function showAnalysisStatus(msg, isError = false) {
  const el = document.getElementById('analysis-status');
  if (!msg) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.textContent = msg;
  el.classList.toggle('error', isError);
}

async function runAnalysisPlot() {
  const btn = document.getElementById('plot-btn');
  const start = inputToIso(document.getElementById('range-start').value);
  const end   = inputToIso(document.getElementById('range-end').value);

  btn.disabled = true;
  showAnalysisStatus('loading...');

  try {
    if (analysisMode === 'timeseries') {
      await plotTimeSeries(start, end);
    } else {
      await plotScatter(start, end);
    }
  } catch (e) {
    console.error('analysis plot failed', e);
    showAnalysisStatus(`error: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

// One Y axis per selected field, color-matched to that field's line so the
// axis a value belongs to is visually obvious. First field gets the normal
// left axis; every field after that stacks on the right, each shifted
// further out. Auto-assigned per field for now, uncapped — revisit with a
// cap (or a "share an axis" option) once we've seen what it looks like with
// a lot of fields checked at once.
const EXTRA_AXIS_STEP = 0.07; // paper-coordinate spacing between stacked right-side axes, desktop baseline
const MIN_AXIS_PX = 46;       // minimum real pixels reserved per stacked axis so its title text
                               // (e.g. "Soil 2 Moisture") doesn't run into the neighboring axis line

function buildTimeSeriesLayout(fields) {
  const layout = { ...PLOTLY_LAYOUT_BASE };
  const extraAxes = Math.max(0, fields.length - 1);

  // EXTRA_AXIS_STEP is a *paper-fraction*, so the same value reserves fewer
  // and fewer actual pixels as the plot gets narrower (e.g. on mobile).
  // Read the plot's real rendered width and widen the step when needed so
  // every stacked axis keeps at least MIN_AXIS_PX of room regardless of
  // screen size.
  const plotEl = document.getElementById('analysis-plot');
  const plotWidth = (plotEl && plotEl.clientWidth) || 600;
  const extraAxisStep = Math.max(EXTRA_AXIS_STEP, MIN_AXIS_PX / plotWidth);

  const rightMargin = extraAxes > 1 ? (extraAxes - 1) * extraAxisStep : 0;

  layout.xaxis = { ...PLOTLY_LAYOUT_BASE.xaxis, domain: [0, 1 - rightMargin] };
  layout.margin = { ...PLOTLY_LAYOUT_BASE.margin, r: 20 + rightMargin * plotWidth };

  fields.forEach((f, i) => {
    const color = fieldColor(f);
    const axisStyle = {
      gridcolor: i === 0 ? PLOTLY_LAYOUT_BASE.yaxis.gridcolor : 'transparent',
      linecolor: color,
      zerolinecolor: PLOTLY_LAYOUT_BASE.yaxis.zerolinecolor,
      tickfont: { color },
      title: { text: f.label, font: { color } },
    };
    if (i === 0) {
      layout.yaxis = axisStyle;
    } else {
      layout[`yaxis${i + 1}`] = {
        ...axisStyle,
        overlaying: 'y',
        side: 'right',
        anchor: i === 1 ? 'x' : 'free',
        position: i === 1 ? undefined : (1 - rightMargin) + (i - 1) * extraAxisStep,
      };
    }
  });

  return layout;
}

async function plotTimeSeries(start, end) {
  const fields = visibleAnalysisFields().filter(f => tsSelectedFields.has(f.id));
  if (!fields.length) {
    showAnalysisStatus('select at least one field above', true);
    Plotly.purge('analysis-plot');
    return;
  }

  // One fetch per distinct sensor_type, reused across fields that share it
  // (e.g. s0.m and s0.tmp both come from a single sensor_type=s0 call).
  const typesNeeded = [...new Set(fields.map(f => f.type))];
  const packetsByType = {};
  await Promise.all(typesNeeded.map(async t => { packetsByType[t] = await fetchSeries(t, start, end); }));

  const traces = fields.map((f, i) => {
    const { xs, ys } = extractPoints(packetsByType[f.type], f.key);
    return {
      x: xs, y: ys,
      type: 'scatter', mode: 'lines+markers',
      name: f.label,
      line: { color: fieldColor(f), width: 1.5 },
      marker: { size: 3 },
      yaxis: i === 0 ? 'y' : `y${i + 1}`,
    };
  });

  showAnalysisStatus(traces.some(t => t.x.length) ? '' : 'no data in that range for the selected field(s)', true);

  Plotly.newPlot('analysis-plot', traces, buildTimeSeriesLayout(fields), { responsive: true, displaylogo: false });
}

async function plotScatter(start, end) {
  const xField = fieldById(document.getElementById('scatter-x-select').value);
  const yField = fieldById(document.getElementById('scatter-y-select').value);

  const [xPackets, yPackets] = await Promise.all([
    fetchSeries(xField.type, start, end),
    fetchSeries(yField.type, start, end),
  ]);

  const { xs: xTs, ys: xVals } = extractPoints(xPackets, xField.key);
  const { xs: yTs, ys: yVals } = extractPoints(yPackets, yField.key);
  const { pairedX, pairedY } = nearestJoin(xTs, xVals, yTs, yVals);

  if (!pairedX.length) {
    showAnalysisStatus('no overlapping data to pair in that range', true);
    Plotly.purge('analysis-plot');
    return;
  }

  showAnalysisStatus('');
  Plotly.newPlot('analysis-plot', [{
    x: pairedX, y: pairedY,
    type: 'scatter', mode: 'markers',
    marker: { color: '#39d0c4', size: 6, opacity: 0.7 },
  }], {
    ...PLOTLY_LAYOUT_BASE,
    xaxis: { ...PLOTLY_LAYOUT_BASE.xaxis, title: xField.label },
    yaxis: { ...PLOTLY_LAYOUT_BASE.yaxis, title: yField.label },
  }, { responsive: true, displaylogo: false });
}

async function initAnalysisPanel() {
  await fetchAvailableFields(); // determines which checkboxes/options even show up
  buildFieldPicker();
  buildPresetButtons();
  const defaultBtn = document.querySelector('.preset-btn[data-minutes="1440"]');
  applyPreset(1440, defaultBtn); // default range: last 24h
  runAnalysisPlot(); // draw something on load rather than an empty panel
}