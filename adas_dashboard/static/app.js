/* Vanilla JS. Telemetry polls every 500 ms; the map is constructed only once. */
"use strict";
const $ = id => document.getElementById(id);
let map, vehicleMarker, following = true, currentPosition = null;
let mapReady = false, apiError = '', storageError = '', potholeError = '';
const potholeMarkers = new Map();
let latestStatus = null, detectedUntil = 0;
let recordsVersion = 0, managementError = '';

function displayErrors() {
  const text = [apiError, storageError, potholeError, managementError].filter(Boolean).join(' · ');
  $('error-banner').textContent = text;
  $('error-banner').hidden = !text;
}

function initializeMap() {
  if (!window.L) { $('map-error').hidden = false; return; }
  map = L.map('map', { zoomControl: false }).setView([6.495461, 79.986114], 17);
  L.control.zoom({ position: 'topright' }).addTo(map);
  const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  tiles.on('tileerror', () => { $('map-error').hidden = false; });
  tiles.on('tileload', () => { $('map-error').hidden = true; });
  map.on('dragstart', () => { following = false; updateFollowButton(); });
  mapReady = true;
}

function updateFollowButton() {
  $('recenter').replaceChildren();
  const icon = document.createElement('span'); icon.textContent = '⌖';
  $('recenter').append(icon, following ? 'Following vehicle' : 'Recenter vehicle');
  $('recenter').setAttribute('aria-pressed', String(following));
}
$('recenter').addEventListener('click', () => {
  following = true; updateFollowButton();
  if (map && currentPosition) map.setView(currentPosition, map.getZoom(), { animate: false });
});

function setPosition(lat, lon) {
  const destination = [lat, lon];
  if (!mapReady) return;
  if (vehicleMarker && currentPosition[0] === lat && currentPosition[1] === lon) return;
  if (!vehicleMarker) {
    currentPosition = destination;
    vehicleMarker = L.marker(destination, {
      icon: L.divIcon({ className: 'vehicle-marker',
        html: '<svg viewBox="0 0 48 56" aria-hidden="true"><path d="M24 4 44 48 24 39 4 48Z" fill="#16b99a" stroke="white" stroke-width="3" stroke-linejoin="round"/><path d="M24 11V36L9 43Z" fill="#78e0c0"/></svg>',
        iconSize: [42, 49], iconAnchor: [21, 25] }),
      zIndexOffset: 1000, title: 'Current vehicle location'
    }).addTo(map);
    if (following) map.setView(destination, map.getZoom(), { animate: false });
  }
  // Exact API coordinates only: no extrapolation or animation-generated positions.
  currentPosition = destination;
  vehicleMarker.setLatLng(destination);
  if (following) map.setView(destination, map.getZoom(), { animate: false });
}

function popup(p) {
  // Build DOM nodes, never interpolate persisted data into HTML.
  const container = document.createElement('div');
  const title = document.createElement('strong'); title.className = 'popup-title';
  title.textContent = `${p.severity} impact pothole`; container.append(title);
  for (const [label, value] of [['Reports', p.report_count], ['Confidence', `${Math.round(p.confidence)}%`]]) {
    const row = document.createElement('div'); row.className = 'popup-row';
    const caption = document.createElement('span'); caption.textContent = label;
    const data = document.createElement('b'); data.textContent = value;
    row.append(caption, data); container.append(row);
  }
  const timestamp = document.createElement('div'); timestamp.className = 'popup-time';
  timestamp.textContent = `Last seen: ${new Date(p.last_seen).toLocaleString()}`;
  container.append(timestamp);
  const actions = document.createElement('div'); actions.className = 'pothole-actions';
  for (const [label, action] of [['Mark as Repaired', 'repaired'], ['Delete Pothole', 'deleted']]) {
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = label;
    button.addEventListener('click', () => managePothole(p.id, action, button));
    actions.append(button);
  }
  container.append(actions); return container;
}

async function managePothole(id, action, button) {
  button.disabled = true;
  ++recordsVersion; // Invalidate any list response fetched before this mutation.
  try {
    const response = await fetch(`/api/potholes/${encodeURIComponent(id)}`, {
      method: action === 'deleted' ? 'DELETE' : 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      ...(action === 'repaired' ? { body: JSON.stringify({ status: 'repaired' }) } : {}),
      signal: AbortSignal.timeout(4000)
    });
    if (!response.ok) throw new Error((await response.json()).error || `HTTP ${response.status}`);
    ++recordsVersion;
    const entry = potholeMarkers.get(id);
    if (entry) { entry.marker.remove(); potholeMarkers.delete(id); }
    managementError = '';
    await pollPotholes();
  } catch (error) {
    managementError = `Pothole update failed: ${error.message}`;
  } finally { button.disabled = false; displayErrors(); }
}

function renderHistory(records) {
  const repaired = records.filter(p => p.status === 'repaired');
  $('repaired-count').textContent = repaired.length;
  const list = $('repaired-list'); list.replaceChildren();
  if (!repaired.length) list.textContent = 'No repaired potholes yet.';
  for (const p of repaired) {
    const row = document.createElement('div'); row.className = 'history-record';
    const title = document.createElement('strong'); title.textContent = `${p.severity} impact · ${p.report_count} reports`;
    const detail = document.createElement('div');
    detail.textContent = `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)} · Repaired ${new Date(p.repaired_at).toLocaleString()}`;
    row.append(title, detail); list.append(row);
  }
}

async function getJSON(url) {
  const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(4000) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function pollPotholes() {
  const version = recordsVersion;
  try {
    const allRecords = await getJSON('/api/potholes?include_history=1');
    if (version !== recordsVersion) return;
    renderHistory(allRecords);
    const records = allRecords.filter(p => !p.status || p.status === 'active');
    $('pothole-count').textContent = records.length;
    if (mapReady) {
      const active = new Set();
      for (const p of records) {
        active.add(p.id);
        const severity = ['low', 'medium', 'high'].includes(p.severity) ? p.severity : 'low';
        const icon = L.divIcon({ className: `pothole-marker ${severity}`, html: '!', iconSize: [22, 22], iconAnchor: [11, 11] });
        let entry = potholeMarkers.get(p.id);
        if (!entry) {
          entry = { marker: L.marker([p.lat, p.lon], { icon, title: `${severity} impact pothole` }).addTo(map).bindPopup(popup(p)), severity, signature: JSON.stringify(p) };
          potholeMarkers.set(p.id, entry);
        } else {
          entry.marker.setLatLng([p.lat, p.lon]);
          if (entry.severity !== severity) { entry.marker.setIcon(icon); entry.severity = severity; }
          if (entry.signature !== JSON.stringify(p)) {
            entry.marker.setPopupContent(popup(p)); entry.signature = JSON.stringify(p);
          }
        }
      }
      for (const [id, entry] of potholeMarkers) if (!active.has(id)) { entry.marker.remove(); potholeMarkers.delete(id); }
    }
    potholeError = '';
  } catch (error) { potholeError = 'Pothole map data unavailable; retrying'; }
  displayErrors();
}

function updateStatus(s) {
  latestStatus = s;
  $('connection').classList.toggle('offline', !s.connected);
  $('connection').lastElementChild.textContent = `${s.source}: ${s.connected ? 'CONNECTED' : 'DISCONNECTED'}`;
  $('mode').textContent = `DATA SOURCE: ${s.source}`;
  document.body.classList.toggle('stale', !s.connected);
  document.body.classList.toggle('stationary', !s.connected || s.speed <= 0);
  $('system-label').textContent = s.connection_message;
  $('drive-state').textContent = s.connected ? `${s.vehicle_state} · ${s.gps_filter_reason}` : 'Last received values · stale';
  $('speed').textContent = s.speed === 0 ? '0.0' : String(Number(s.speed.toFixed(2)));
  $('az').textContent = s.az.toFixed(3);
  $('vibration').textContent = s.vibration.toFixed(3);
  $('confidence').textContent = Math.round(s.confidence);
  $('condition').className = `condition ${s.connected ? s.road.toLowerCase() : ''}`;
  $('condition').replaceChildren(document.createElement('i'), ` ${s.road}`);
  $('satellites').textContent = s.satellites;
  $('gps-fix').textContent = !s.connected ? 'STALE' : s.gps_fix ? 'FIX OK' : 'NO FIX';
  $('gps-quality').textContent = `GPS QUALITY: ${s.gps_quality}`;
  $('gps-warning').textContent = s.gps_warning || '';
  $('gps-warning').hidden = !s.connected || !s.gps_warning;
  $('confidence-bar').style.width = `${s.confidence}%`;
  $('location-status').textContent = !s.connected ? 'LAST KNOWN POSITION' : s.gps_fix ? 'GPS FIX ACQUIRED' : 'WAITING FOR GPS';
  if (s.gps_fix) {
    $('latitude').textContent = `${Math.abs(s.lat).toFixed(6)}° ${s.lat >= 0 ? 'N' : 'S'}`;
    $('longitude').textContent = `${Math.abs(s.lon).toFixed(6)}° ${s.lon >= 0 ? 'E' : 'W'}`;
    if (s.connected) setPosition(s.lat, s.lon);
  } else if (s.connected) {
    $('latitude').textContent = '—'; $('longitude').textContent = '—';
  }
  if (!s.connected) {
    detectedUntil = 0;
    $('warning').hidden = true;
  }
  if (s.connected && s.pothole_recorded) detectedUntil = performance.now() + 2200;
  storageError = s.storage_error || '';
  if (s.serial_error && !s.connected) $('connection').title = s.serial_error;
  else $('connection').title = '';
}

function renderWarning(now) {
  const s = latestStatus;
  const detected = s && s.connected && now < detectedUntil;
  const ahead = s && s.connected && s.nearest_pothole;
  $('warning').hidden = !detected && !ahead;
  if (!detected && !ahead) return;
  $('warning').classList.toggle('detected', Boolean(detected));
  const title = detected ? 'POTHOLE DETECTED' : 'POTHOLE AHEAD';
  const distance = detected ? '' : `${ahead.distance_m} m`;
  // Only change live-region text when needed, avoiding repeated announcements.
  if ($('warning-title').textContent !== title) $('warning-title').textContent = title;
  if ($('warning-distance').textContent !== distance) $('warning-distance').textContent = distance;
  $('warning-detail').textContent = detected
    ? (!s.gps_fix ? 'Surface impact detected · no GPS fix' : s.storage_error ? 'Surface impact detected · storage error' : 'Surface impact detected · report recorded')
    : 'Nearby hazard · distance-only alert';
}

async function pollStatus() {
  try {
    const data = await getJSON('/api/status');
    console.log("API status:", data);
    console.log('Raw GPS:', { lat: data.raw_lat, lon: data.raw_lon, speed: data.raw_speed },
                'Filtered GPS:', { lat: data.lat, lon: data.lon, speed: data.speed,
                  state: data.vehicle_state, quality: data.gps_quality, reason: data.gps_filter_reason });
    updateStatus(data);
    apiError = '';
  }
  catch (error) {
    apiError = 'Dashboard server unavailable; reconnecting…';
    if (latestStatus) latestStatus.connected = false;
    $('connection').classList.add('offline');
    $('connection').lastElementChild.textContent = 'Dashboard offline';
    document.body.classList.add('stale');
    document.body.classList.add('stationary');
    $('speed').textContent = '0';
    $('vibration').textContent = '0.000';
    $('az').textContent = '0.000';
    $('confidence').textContent = '0';
    $('confidence-bar').style.width = '0%';
    $('satellites').textContent = '0';
    $('warning').hidden = true;
    detectedUntil = 0;
    $('system-label').textContent = 'SERVER DISCONNECTED';
    $('drive-state').textContent = 'Last received values · stale';
    $('gps-fix').textContent = 'STALE';
    $('condition').className = 'condition';
    $('condition').replaceChildren(document.createElement('i'), ' OFFLINE');
    $('location-status').textContent = 'LAST KNOWN POSITION';
  }
  displayErrors();
}

function animate(now) {
  // Only warning visibility uses a clock; telemetry changes only in pollStatus.
  renderWarning(now);
  requestAnimationFrame(animate);
}

initializeMap(); updateFollowButton();
// Guard requests so a slow connection never creates a growing request queue.
let statusBusy = true, potholesBusy = true;
pollStatus().finally(() => { statusBusy = false; });
pollPotholes().finally(() => { potholesBusy = false; });
setInterval(async () => { if (statusBusy) return; statusBusy = true; try { await pollStatus(); } finally { statusBusy = false; } }, 500);
setInterval(async () => { if (potholesBusy) return; potholesBusy = true; try { await pollPotholes(); } finally { potholesBusy = false; } }, 2000);
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString('en-GB'); }, 1000);
requestAnimationFrame(animate);
