'use strict';

const PLANE_PATH = 'M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z';
const $ = (selector) => document.querySelector(selector);

const ui = {
  tab: 'dashboard',
  status: null,
  settings: null,
  militaryOnly: false,
  mapFitted: false,
  logSince: 0,
};

// ---------- helpers ----------

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(path, options = {}) {
  const headers = { 'X-Requested-With': 'FlightAlerter' };
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    location.replace('/login');
    throw new Error('Signed out');
  }
  let data = null;
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    const error = new Error((data && data.error) || response.statusText);
    error.data = data;
    throw error;
  }
  return data;
}

function ago(timestamp, now = Date.now() / 1000) {
  if (!timestamp) return 'never';
  const seconds = Math.max(0, Math.round(now - timestamp));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return seconds + 's ago';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
  return Math.floor(seconds / 86400) + 'd ago';
}

function clock(timestamp) {
  return new Date(timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function dateTime(timestamp) {
  return new Date(timestamp * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function formatAltitude(alt) {
  if (alt === 'ground') return 'Ground';
  if (typeof alt !== 'number') return '–';
  return alt.toLocaleString() + ' ft';
}

function callsign(ac) {
  return ac.flight || ac.registration || (ac.hex || '').toUpperCase() || 'Unknown';
}

let toastTimer;
function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast ' + kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

// ---------- tabs ----------

function showTab(name) {
  ui.tab = name;
  document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  document.querySelectorAll('.tab-panel').forEach((p) => { p.hidden = p.id !== 'tab-' + name; });
  try { localStorage.setItem('flightAlerter.tab', name); } catch (_) { /* storage blocked */ }
  if (name === 'dashboard') { map.invalidateSize(); refreshAircraft(); }
  if (name === 'alerts') refreshAlerts();
  if (name === 'settings') loadSettings();
  if (name === 'log') refreshLog();
}

document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));

// ---------- map ----------

const map = L.map('map', { zoomControl: true, attributionControl: true, preferCanvas: false }).setView([39, -98], 4);
// Standard OSM tiles need no API key; app.css darkens them to fit the theme.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 18,
  className: 'map-tiles',
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const homeMarker = L.marker([0, 0], {
  icon: L.divIcon({ className: '', html: '<div class="home-marker"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }),
  interactive: false,
});
const radiusCircle = L.circle([0, 0], {
  radius: 1, color: '#ff7a45', weight: 1.5, dashArray: '6 6', fillColor: '#ff7a45', fillOpacity: 0.05, interactive: false,
});
const aircraftMarkers = new Map();

function isWatched(ac) {
  return ac.watched && ac.watched.length > 0;
}

function planeIcon(ac) {
  const kind = isWatched(ac) ? 'watch' : ac.military ? 'mil' : 'civ';
  const rotation = typeof ac.track === 'number' ? ac.track : 0;
  const text = isWatched(ac) ? ac.watched[0].label : callsign(ac);
  const label = kind !== 'civ' ? '<span class="plane-label">' + escapeHtml(text) + '</span>' : '';
  return L.divIcon({
    className: '',
    html: '<div class="plane plane-' + kind + '"><svg viewBox="0 0 24 24" style="transform: rotate(' + rotation + 'deg)"><path d="' + PLANE_PATH + '"/></svg>' + label + '</div>',
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

function popupHtml(ac) {
  const rows = [
    ['Type', ac.type],
    ['Registration', ac.registration || '–'],
    ['Hex', (ac.hex || '').toUpperCase()],
    ['Distance', ac.distance + ' mi'],
    ['Altitude', formatAltitude(ac.altitude)],
    ['Speed', typeof ac.speed === 'number' ? Math.round(ac.speed) + ' kt' : '–'],
    ['Heading', typeof ac.track === 'number' ? Math.round(ac.track) + '°' : '–'],
    ['Squawk', ac.squawk || '–'],
  ];
  const badges = (ac.military ? '<span class="badge">Military</span>' : '') +
    (isWatched(ac) ? '<span class="badge badge-watch">' + escapeHtml(ac.watched.map((w) => w.label).join(', ')) + '</span>' : '');
  return '<div class="popup-title">' + escapeHtml(callsign(ac)) + badges + '</div>' +
    '<dl class="popup-grid">' + rows.map(([k, v]) => '<dt>' + k + '</dt><dd>' + escapeHtml(v) + '</dd>').join('') + '</dl>';
}

function updateMap(payload) {
  const home = [payload.home.lat, payload.home.lon];
  homeMarker.setLatLng(home).addTo(map);
  radiusCircle.setLatLng(home).setRadius(payload.radius_miles * 1609.344).addTo(map);
  if (!ui.mapFitted) {
    map.fitBounds(radiusCircle.getBounds().pad(0.6));
    ui.mapFitted = true;
  }

  const visible = payload.aircraft.filter((ac) => ac.military || isWatched(ac) || !ui.militaryOnly);
  const seen = new Set();
  for (const ac of visible) {
    seen.add(ac.hex);
    let marker = aircraftMarkers.get(ac.hex);
    if (!marker) {
      marker = L.marker([ac.lat, ac.lon]).bindPopup('');
      marker.addTo(map);
      aircraftMarkers.set(ac.hex, marker);
    }
    marker.setLatLng([ac.lat, ac.lon]);
    // Rebuilding the icon resets its DOM, so only do it when something visible changed.
    const iconKey = ac.military + '|' + (ac.watched || []).map((w) => w.label).join(',') + '|' + Math.round(ac.track || 0) + '|' + callsign(ac);
    if (marker.iconKey !== iconKey) {
      marker.setIcon(planeIcon(ac));
      marker.setZIndexOffset(isWatched(ac) ? 2000 : ac.military ? 1000 : 0);
      marker.iconKey = iconKey;
    }
    marker.aircraft = ac;
    if (marker.isPopupOpen()) marker.setPopupContent(popupHtml(ac));
    else marker.getPopup().setContent(popupHtml(ac));
  }
  for (const [hex, marker] of aircraftMarkers) {
    if (!seen.has(hex)) { marker.remove(); aircraftMarkers.delete(hex); }
  }
}

function focusAircraft(hex) {
  const marker = aircraftMarkers.get(hex);
  if (!marker) return;
  map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), 10), { duration: 0.8 });
  marker.openPopup();
}

$('#military-only').addEventListener('change', (event) => {
  ui.militaryOnly = event.target.checked;
  try { localStorage.setItem('flightAlerter.militaryOnly', ui.militaryOnly ? '1' : '0'); } catch (_) { /* storage blocked */ }
  refreshAircraft();
});

// ---------- live aircraft ----------

async function refreshAircraft() {
  if (ui.tab !== 'dashboard') return;
  try {
    const payload = await api('/api/aircraft');
    $('#map-error').hidden = true;
    updateMap(payload);
    renderMilitaryList(payload);
    renderWatchStatus(payload);
    $('#stat-tracked').textContent = payload.aircraft.length;
    $('#stat-tracked-sub').textContent = 'with a position fix';
  } catch (error) {
    $('#map-error').textContent = error.message;
    $('#map-error').hidden = false;
  }
}

function renderMilitaryList(payload) {
  const military = payload.aircraft.filter((ac) => ac.military).sort((a, b) => a.distance - b.distance);
  const inRange = military.filter((ac) => ac.distance <= payload.radius_miles);
  $('#stat-military').textContent = military.length;
  $('#stat-inrange').textContent = inRange.length;
  $('#stat-radius').textContent = 'within ' + payload.radius_miles + ' mi of home';
  $('#military-count').textContent = military.length ? military.length + ' visible' : '';

  const list = $('#military-list');
  if (!military.length) {
    list.innerHTML = '<li class="empty">No military aircraft in view right now.</li>';
    return;
  }
  list.innerHTML = military.map((ac) => {
    const near = ac.distance <= payload.radius_miles;
    return '<li class="ac-item' + (near ? ' in-range' : '') + '" data-hex="' + escapeHtml(ac.hex) + '" tabindex="0">' +
      '<span class="ac-call">' + escapeHtml(callsign(ac)) + '</span>' +
      '<span class="ac-dist">' + ac.distance + ' mi</span>' +
      '<span class="ac-meta">' + escapeHtml(ac.type) + (ac.registration ? ' · ' + escapeHtml(ac.registration) : '') + '</span>' +
      '<span class="ac-meta-r">' + escapeHtml(formatAltitude(ac.altitude)) + '</span>' +
      '</li>';
  }).join('');
}

function renderWatchStatus(payload) {
  const list = $('#watch-status');
  if (!payload.watchlist.length) {
    list.innerHTML = '<li class="empty">Nothing on the watchlist. Use Edit to add a tail number.</li>';
    return;
  }
  list.innerHTML = payload.watchlist.map((entry) => {
    const matches = payload.aircraft.filter((ac) => (ac.watched || []).some((w) => w.id === entry.id))
      .sort((a, b) => a.distance - b.distance);
    const ac = matches[0];
    const inRange = ac && ac.distance <= entry.radius_miles;
    const classes = ['ac-item', 'watch-item', ac ? 'in-view' : '', inRange ? 'in-range' : '', entry.enabled ? '' : 'off'].join(' ');
    return '<li class="' + classes + '"' + (ac ? ' data-hex="' + escapeHtml(ac.hex) + '" tabindex="0"' : '') + '>' +
      '<span class="ac-call">' + escapeHtml(entry.label) + '</span>' +
      '<span class="ac-dist">' + (ac ? ac.distance + ' mi' : '–') + '</span>' +
      '<span class="ac-meta">' + (entry.label !== entry.match ? escapeHtml(entry.match) + ' · ' : '') +
        (entry.enabled ? 'alerts within ' + entry.radius_miles + ' mi' : 'alerts off') + '</span>' +
      '<span class="ac-meta-r">' + (ac ? escapeHtml(formatAltitude(ac.altitude)) : 'not in view') + '</span>' +
      '</li>';
  }).join('');
}

$('#watch-status').addEventListener('click', (event) => {
  const item = event.target.closest('.ac-item[data-hex]');
  if (item) focusAircraft(item.dataset.hex);
});
$('#edit-watchlist').addEventListener('click', () => {
  showTab('settings');
  setTimeout(() => $('#watchlist-card').scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
});

$('#military-list').addEventListener('click', (event) => {
  const item = event.target.closest('.ac-item');
  if (item) focusAircraft(item.dataset.hex);
});
$('#military-list').addEventListener('keydown', (event) => {
  const item = event.target.closest('.ac-item');
  if (item && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); focusAircraft(item.dataset.hex); }
});

// ---------- status ----------

async function refreshStatus() {
  try {
    ui.status = await api('/api/status');
    $('#banner').hidden = true;
    renderStatus();
    renderTrend(ui.status.history);
  } catch (error) {
    setPill('bad', 'Offline');
    $('#status-timing').textContent = '';
    $('#banner').textContent = 'Lost connection to the Flight Alerter service. Retrying…';
    $('#banner').hidden = false;
  }
}

function setPill(kind, text) {
  $('#status-pill').className = 'pill pill-' + kind;
  $('#status-text').textContent = text;
}

function renderStatus() {
  const s = ui.status;
  if (!s) return;
  const now = Date.now() / 1000;
  const result = s.last_result;

  if (s.running) setPill('warn pill-busy', 'Checking…');
  else if (!s.enabled) setPill('muted', 'Paused');
  else if (result && !result.ok) setPill('bad', 'Error');
  else if (result) setPill('good', 'Monitoring');
  else setPill('muted', 'Starting…');

  let timing = '';
  if (result) timing = 'Checked ' + ago(result.finished_at, now);
  if (s.enabled && s.next_run_at && !s.running) {
    const next = Math.max(0, Math.round(s.next_run_at - now));
    timing += (timing ? ' · ' : '') + 'next in ' + next + 's';
  }
  $('#status-timing').textContent = timing;

  const quick = $('#quick-enabled');
  if (document.activeElement !== quick) quick.checked = s.enabled;

  if (result && !result.ok) {
    $('#banner').textContent = 'Last check failed: ' + result.error;
    $('#banner').hidden = false;
  }

  if (!s.send_emails) {
    $('#stat-email').textContent = 'Emails off';
    $('#stat-email-sub').textContent = 'turn on in Settings';
  } else if (!s.smtp.configured) {
    $('#stat-email').textContent = 'Not set up';
    $('#stat-email-sub').textContent = 'add the email server in Settings';
  } else {
    $('#stat-email').textContent = s.last_email_at ? ago(s.last_email_at, now) : 'None yet';
    $('#stat-email-sub').textContent = s.last_email_at ? dateTime(s.last_email_at) : 'since the service started';
  }
}

$('#run-now').addEventListener('click', async () => {
  const button = $('#run-now');
  button.disabled = true;
  setPill('warn pill-busy', 'Checking…');
  try {
    const result = await api('/api/run', { method: 'POST' });
    if (!result.ok) toast('Check failed: ' + result.error, 'bad');
    else if (result.email_sent) toast('Sent ' + result.emails_sent + ' email(s) about ' + result.notified.length + ' aircraft.', 'good');
    else toast('Check complete: ' + result.military_in_range.length + ' military and ' + result.watch_in_range.length + ' watched aircraft in range.', 'good');
  } catch (error) {
    toast('Check failed: ' + error.message, 'bad');
  } finally {
    button.disabled = false;
    refreshStatus();
    refreshAlerts();
  }
});

$('#quick-enabled').addEventListener('change', async (event) => {
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ enabled: event.target.checked }) });
    toast(event.target.checked ? 'Monitoring resumed.' : 'Monitoring paused.');
    if (ui.settings) ui.settings.enabled = event.target.checked;
    // Keep the settings form in step without discarding other unsaved edits.
    form.elements.enabled.checked = event.target.checked;
    if (savedSnapshot) savedSnapshot = JSON.stringify({ ...JSON.parse(savedSnapshot), enabled: event.target.checked });
    updateDirty();
  } catch (error) {
    event.target.checked = !event.target.checked;
    toast('Could not change monitoring: ' + error.message, 'bad');
  }
  refreshStatus();
});

// ---------- trend sparkline ----------

function renderTrend(history) {
  const svg = $('#trend-svg');
  const box = $('#trend');
  const points = history.filter((h) => h.ok);
  if (points.length < 2) {
    svg.innerHTML = '';
    $('#trend-range').textContent = 'collecting data…';
    $('#trend-start').textContent = '';
    return;
  }
  const width = 300;
  const height = 100;
  const t0 = points[0].time;
  const t1 = points[points.length - 1].time;
  const max = Math.max(...points.map((p) => p.positioned), 1);
  const x = (t) => ((t - t0) / Math.max(t1 - t0, 1)) * width;
  const y = (v) => height - (v / max) * (height - 6);
  const line = points.map((p, i) => (i ? 'L' : 'M') + x(p.time).toFixed(1) + ' ' + y(p.positioned).toFixed(1)).join(' ');

  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  svg.innerHTML =
    '<defs><linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="#4cc9f0" stop-opacity=".28"/><stop offset="1" stop-color="#4cc9f0" stop-opacity="0"/></linearGradient></defs>' +
    '<line class="trend-grid" x1="0" x2="' + width + '" y1="' + height + '" y2="' + height + '"/>' +
    '<path class="trend-area" d="' + line + ' L' + width + ' ' + height + ' L0 ' + height + ' Z"/>' +
    '<path class="trend-line" d="' + line + '"/>' +
    '<line class="trend-cross" id="trend-cross" y1="0" y2="' + height + '" visibility="hidden"/>';

  $('#trend-range').textContent = 'peak ' + max;
  $('#trend-start').textContent = clock(t0);

  box.onmousemove = (event) => {
    const rect = box.getBoundingClientRect();
    const t = t0 + ((event.clientX - rect.left) / rect.width) * (t1 - t0);
    const nearest = points.reduce((best, p) => (Math.abs(p.time - t) < Math.abs(best.time - t) ? p : best));
    const px = (x(nearest.time) / width) * rect.width;
    const py = (y(nearest.positioned) / height) * rect.height;
    const cross = $('#trend-cross');
    cross.setAttribute('x1', x(nearest.time));
    cross.setAttribute('x2', x(nearest.time));
    cross.setAttribute('visibility', 'visible');
    let dot = box.querySelector('.trend-dot');
    if (!dot) { dot = document.createElement('div'); dot.className = 'trend-dot'; box.appendChild(dot); }
    dot.style.left = px + 'px';
    dot.style.top = py + 'px';
    const tip = $('#trend-tip');
    tip.innerHTML = clock(nearest.time) + ' · <b>' + nearest.positioned + '</b> aircraft' +
      (nearest.military ? ' · ' + nearest.military + ' military' : '');
    tip.style.left = Math.min(Math.max(px, 70), rect.width - 70) + 'px';
    tip.hidden = false;
  };
  box.onmouseleave = () => {
    $('#trend-tip').hidden = true;
    const cross = $('#trend-cross');
    if (cross) cross.setAttribute('visibility', 'hidden');
    const dot = box.querySelector('.trend-dot');
    if (dot) dot.remove();
  };
}

// ---------- alerts ----------

async function refreshAlerts() {
  if (ui.tab !== 'alerts') return;
  try {
    const data = await api('/api/alerts');
    const rows = data.alerts;
    $('#alerts-total').textContent = data.total + ' email' + (data.total === 1 ? '' : 's') + ' sent';
    $('#alerts-empty').hidden = rows.length > 0;
    $('#alerts-body').innerHTML = rows.map((row) =>
      '<tr><td class="mono">' + escapeHtml(row.aircraft.toUpperCase()) + '</td>' +
      '<td class="mono">' + escapeHtml(row.flight || '–') + '</td>' +
      '<td>' + (row.kind === 'watch' ? '<span class="tag tag-watch">Watchlist · ' + escapeHtml(row.watch) + '</span>' : '<span class="tag tag-mil">Military</span>') + '</td>' +
      '<td title="' + escapeHtml(new Date(row.sent_at * 1000).toLocaleString()) + '">' + escapeHtml(dateTime(row.sent_at)) + ' <span class="muted">(' + ago(row.sent_at) + ')</span></td>' +
      '<td class="num">' + row.count + '</td>' +
      '<td class="num"><button class="btn btn-danger-ghost" data-reset="' + escapeHtml(row.key) + '" data-name="' + escapeHtml(row.aircraft.toUpperCase()) + '">Reset</button></td></tr>'
    ).join('');
  } catch (error) {
    toast('Could not load alerts: ' + error.message, 'bad');
  }
}

$('#alerts-body').addEventListener('click', async (event) => {
  const key = event.target.dataset && event.target.dataset.reset;
  if (!key) return;
  const name = event.target.dataset.name;
  if (!confirm('Reset ' + name + '? It will be able to trigger new emails.')) return;
  try {
    await api('/api/alerts/' + encodeURIComponent(key), { method: 'DELETE' });
    toast(name + ' reset.', 'good');
    refreshAlerts();
  } catch (error) {
    toast('Reset failed: ' + error.message, 'bad');
  }
});

// ---------- settings ----------

const form = $('#settings-form');
let recipients = [];
let savedSnapshot = '';

function formValues() {
  const values = { distribution_list: recipients.slice(), watchlist: watchRowsValues() };
  for (const input of form.querySelectorAll('[name]')) {
    if (input.type === 'checkbox') values[input.name] = input.checked;
    else if (input.type === 'number') values[input.name] = input.value === '' ? null : Number(input.value);
    else values[input.name] = input.value.trim();
  }
  return values;
}

function isDirty() {
  return savedSnapshot !== '' && JSON.stringify(formValues()) !== savedSnapshot;
}

function updateDirty() {
  $('#savebar').hidden = !isDirty();
}

function fillForm(settings) {
  for (const input of form.querySelectorAll('[name]')) {
    const value = settings[input.name];
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = value ?? '';
  }
  $('#f-radius-range').value = Math.min(settings.alert_radius_miles, 150);
  recipients = settings.distribution_list.slice();
  renderRecipients();
  renderWatchRows(settings.watchlist);
  $('#smtp-password-hint').textContent = settings.smtp_password_set
    ? 'A password is saved. Leave blank to keep it.'
    : 'No password saved yet.';
  clearErrors();
  savedSnapshot = JSON.stringify(formValues());
  updateDirty();
}

function renderRecipients() {
  const box = $('#recipients');
  box.querySelectorAll('.chip').forEach((chip) => chip.remove());
  const input = $('#f-recipient');
  recipients.forEach((address, index) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = escapeHtml(address) + '<button type="button" aria-label="Remove ' + escapeHtml(address) + '" data-index="' + index + '">×</button>';
    box.insertBefore(chip, input);
  });
}

function addRecipient() {
  const input = $('#f-recipient');
  const values = input.value.split(/[\s,;|]+/).map((v) => v.trim()).filter(Boolean);
  for (const value of values) if (!recipients.includes(value)) recipients.push(value);
  input.value = '';
  renderRecipients();
  updateDirty();
}

$('#f-recipient').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); addRecipient(); }
  else if (event.key === 'Backspace' && !event.target.value && recipients.length) { recipients.pop(); renderRecipients(); updateDirty(); }
});
$('#f-recipient').addEventListener('blur', () => { if ($('#f-recipient').value.trim()) addRecipient(); });
$('#recipients').addEventListener('click', (event) => {
  if (event.target.dataset.index !== undefined) {
    recipients.splice(Number(event.target.dataset.index), 1);
    renderRecipients();
    updateDirty();
  } else {
    $('#f-recipient').focus();
  }
});

$('#f-radius-range').addEventListener('input', (event) => { $('#f-radius').value = event.target.value; updateDirty(); });
$('#f-radius').addEventListener('input', (event) => { $('#f-radius-range').value = Math.min(Number(event.target.value) || 1, 150); });
form.addEventListener('input', updateDirty);
form.addEventListener('change', updateDirty);

// ----- watchlist rows -----

function watchRow(entry) {
  const row = $('#watch-row-template').content.firstElementChild.cloneNode(true);
  row.dataset.id = entry.id || '';
  row.querySelector('[data-watch="match"]').value = entry.match || '';
  row.querySelector('[data-watch="label"]').value = entry.label || '';
  row.querySelector('[data-watch="radius_miles"]').value = entry.radius_miles ?? 10;
  row.querySelector('[data-watch="recipients"]').value = (entry.recipients || []).join(', ');
  row.querySelector('[data-watch="subject"]').value = entry.subject || '';
  row.querySelector('[data-watch="enabled"]').checked = entry.enabled !== false;
  row.classList.toggle('disabled', entry.enabled === false);
  return row;
}

function renderWatchRows(entries) {
  const container = $('#watch-rows');
  container.replaceChildren(...entries.map(watchRow));
  $('#watch-empty').hidden = entries.length > 0;
}

function watchRowsValues() {
  return [...document.querySelectorAll('#watch-rows .watch-row')].map((row) => {
    const get = (field) => row.querySelector('[data-watch="' + field + '"]');
    const radius = get('radius_miles').value;
    return {
      id: row.dataset.id,
      match: get('match').value.trim().toUpperCase(),
      label: get('label').value.trim(),
      radius_miles: radius === '' ? null : Number(radius),
      recipients: get('recipients').value.split(/[\s,;|]+/).map((v) => v.trim()).filter(Boolean),
      subject: get('subject').value.trim(),
      enabled: get('enabled').checked,
    };
  });
}

$('#add-watch').addEventListener('click', () => {
  const row = watchRow({ radius_miles: 10, enabled: true });
  $('#watch-rows').appendChild(row);
  $('#watch-empty').hidden = true;
  row.querySelector('[data-watch="match"]').focus();
  updateDirty();
});

$('#watch-rows').addEventListener('click', (event) => {
  const remove = event.target.closest('[data-remove-watch]');
  if (!remove) return;
  remove.closest('.watch-row').remove();
  $('#watch-empty').hidden = $('#watch-rows').childElementCount > 0;
  updateDirty();
});

$('#watch-rows').addEventListener('change', (event) => {
  if (event.target.dataset.watch === 'enabled') event.target.closest('.watch-row').classList.toggle('disabled', !event.target.checked);
});

function clearErrors() {
  form.querySelectorAll('[data-error]').forEach((el) => { el.textContent = ''; });
  form.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
}

function showErrors(errors) {
  const watchError = errors.watchlist && /aircraft #(\d+)/.exec(errors.watchlist);
  if (watchError) {
    const row = document.querySelectorAll('#watch-rows .watch-row')[Number(watchError[1]) - 1];
    if (row) row.querySelector('[data-watch="match"]').setAttribute('aria-invalid', 'true');
  }
  for (const [field, message] of Object.entries(errors)) {
    const el = form.querySelector('[data-error="' + field + '"]');
    if (el) el.textContent = message.charAt(0).toUpperCase() + message.slice(1) + '.';
    const input = form.querySelector('[name="' + field + '"]');
    if (input) input.setAttribute('aria-invalid', 'true');
  }
}

async function loadSettings() {
  if (isDirty()) return;
  try {
    ui.settings = await api('/api/settings');
    fillForm(ui.settings);
  } catch (error) {
    toast('Could not load settings: ' + error.message, 'bad');
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if ($('#f-recipient').value.trim()) addRecipient();
  clearErrors();
  const button = $('#save');
  button.disabled = true;
  try {
    ui.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify(formValues()) });
    ui.mapFitted = false;
    fillForm(ui.settings);
    toast('Settings saved.', 'good');
    refreshAircraft();
    refreshStatus();
  } catch (error) {
    if (error.data && error.data.errors) {
      showErrors(error.data.errors);
      toast('Some settings need fixing.', 'bad');
    } else {
      toast('Save failed: ' + error.message, 'bad');
    }
  } finally {
    button.disabled = false;
  }
});

$('#discard').addEventListener('click', () => fillForm(ui.settings));

$('#test-receiver').addEventListener('click', async () => {
  const out = $('#receiver-result');
  out.className = 'hint';
  out.textContent = 'Connecting…';
  try {
    const data = await api('/api/receiver?url=' + encodeURIComponent($('#f-url').value.trim()));
    out.className = 'hint ok';
    out.textContent = 'Connected. ' + data.aircraft + ' aircraft reported' + (data.version ? ' (receiver ' + data.version + ')' : '') + '.';
  } catch (error) {
    out.className = 'hint bad';
    out.textContent = 'Could not connect: ' + error.message;
  }
});

$('#use-receiver-location').addEventListener('click', async () => {
  try {
    const data = await api('/api/receiver?url=' + encodeURIComponent($('#f-url').value.trim()));
    if (typeof data.lat !== 'number') throw new Error("the receiver doesn't publish its location");
    $('#f-lat').value = data.lat;
    $('#f-lon').value = data.lon;
    updateDirty();
    toast('Location filled in from the receiver. Save to apply.');
  } catch (error) {
    toast('Could not get receiver location: ' + error.message, 'bad');
  }
});

$('#test-email').addEventListener('click', async () => {
  const button = $('#test-email');
  const out = $('#test-email-result');
  if (isDirty()) { toast('Save your changes first; the test uses the saved recipients.', 'bad'); return; }
  button.disabled = true;
  out.className = 'hint';
  out.textContent = 'Sending…';
  try {
    await api('/api/test-email', { method: 'POST' });
    out.className = 'hint ok';
    out.textContent = 'Sent to ' + recipients.join(', ') + '.';
  } catch (error) {
    out.className = 'hint bad';
    out.textContent = 'Failed: ' + error.message;
  } finally {
    button.disabled = false;
  }
});

window.addEventListener('beforeunload', (event) => {
  if (isDirty()) { event.preventDefault(); event.returnValue = ''; }
});

// ---------- security ----------

$('#change-password').addEventListener('click', async () => {
  const out = $('#pw-result');
  const current = $('#pw-current').value;
  const next = $('#pw-new').value;
  out.className = 'hint bad';
  if (next.length < 8) { out.textContent = 'The new password must be at least 8 characters.'; return; }
  if (next !== $('#pw-confirm').value) { out.textContent = 'The new passwords do not match.'; return; }
  try {
    await api('/api/auth/password', { method: 'POST', body: JSON.stringify({ current, new: next }) });
    ['#pw-current', '#pw-new', '#pw-confirm'].forEach((id) => { $(id).value = ''; });
    out.className = 'hint ok';
    out.textContent = 'Password changed. Other browsers have been signed out.';
  } catch (error) {
    out.textContent = error.message;
  }
});

$('#sign-out').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* going to login anyway */ }
  location.replace('/login');
});

// ---------- log ----------

async function refreshLog() {
  if (ui.tab !== 'log') return;
  try {
    const data = await api('/api/logs?since=' + ui.logSince);
    if (!data.lines.length) return;
    const log = $('#log');
    const html = data.lines.map((line) =>
      '<div class="log-line"><span class="log-time">' + new Date(line.time * 1000).toLocaleTimeString() + '</span>' +
      '<span class="log-level log-' + escapeHtml(line.level) + '">' + escapeHtml(line.level) + '</span>' +
      '<span class="log-msg">' + escapeHtml(line.message) + '</span></div>').join('');
    log.insertAdjacentHTML('beforeend', html);
    ui.logSince = data.lines[data.lines.length - 1].time;
    while (log.childElementCount > 500) log.firstElementChild.remove();
    if ($('#log-follow').checked) log.scrollTop = log.scrollHeight;
  } catch (_) { /* status banner already reports connection loss */ }
}

// ---------- boot ----------

try {
  ui.militaryOnly = localStorage.getItem('flightAlerter.militaryOnly') === '1';
  $('#military-only').checked = ui.militaryOnly;
  const savedTab = localStorage.getItem('flightAlerter.tab');
  if (savedTab && document.getElementById('tab-' + savedTab)) ui.tab = savedTab;
} catch (_) { /* storage blocked */ }

showTab(ui.tab);
refreshStatus();
setInterval(refreshStatus, 5000);
setInterval(renderStatus, 1000);
setInterval(refreshAircraft, 3000);
setInterval(refreshLog, 3000);
setInterval(refreshAlerts, 30000);
