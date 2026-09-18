// Node's built-in runner: verifies data behavior without a browser/dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('real telemetry is exact, stationary between responses, and frozen on disconnect', () => {
  const elements = new Map();
  function element() {
    return { textContent: '', style: {}, hidden: false, lastElementChild: { textContent: '' },
      classList: { toggle() {}, add() {} }, replaceChildren() {}, append() {},
      setAttribute() {}, addEventListener() {} };
  }
  const document = { body: element(), createElement: element, getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  } };
  const positions = [];
  const map = { setView() { return this; }, on() {}, getZoom() { return 17; } };
  const marker = { addTo() { return this; }, setLatLng(coords) { positions.push(coords); } };
  const L = { map: () => map, control: { zoom: () => ({ addTo() {} }) },
    tileLayer: () => ({ addTo() { return this; }, on() {} }),
    marker: () => marker, divIcon: x => x };
  const context = vm.createContext({ document, window: { L }, L, console,
    performance: { now: () => 0 }, AbortSignal,
    fetch: () => new Promise(() => {}), setInterval() {}, requestAnimationFrame() {} });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  context.packet = { connected: true, source: 'ESP32', simulation: false, gps_fix: true,
    lat: 6.495422, lon: 79.986077, speed: 0, raw_lat: 6.495425, raw_lon: 79.98608,
    raw_speed: .52, vehicle_state: 'STATIONARY', gps_quality: 'FAIR', az: 1.027, vibration: .032,
    confidence: 95, satellites: 5, road: 'SMOOTH', connection_message: 'ESP32 CONNECTED' };
  vm.runInContext('updateStatus(packet)', context);
  assert.equal(elements.get('speed').textContent, '0.0');
  assert.equal(elements.get('vibration').textContent, '0.032');
  assert.equal(elements.get('mode').textContent, 'DATA SOURCE: ESP32');
  assert.equal(positions.length, 1);
  assert.equal(positions[0][0], context.packet.lat);
  context.packet.raw_lat += .00001;
  vm.runInContext('updateStatus(packet)', context);
  assert.equal(positions.length, 1); // raw drift never calls setLatLng again
  for (let i = 1; i < 30; i++) vm.runInContext(`animate(${i * 500})`, context);
  assert.equal(positions.length, 1); // clocks never move the vehicle
  assert.equal(elements.get('speed').textContent, '0.0');
  Object.assign(context.packet, { connected: false, gps_fix: false, speed: 0,
    az: 0, vibration: 0, satellites: 0, confidence: 0, road: 'DISCONNECTED',
    connection_message: 'ESP32 DISCONNECTED' });
  vm.runInContext('updateStatus(packet); animate(16000)', context);
  assert.equal(positions.length, 1);
  assert.equal(elements.get('speed').textContent, '0.0');
  assert.equal(elements.get('vibration').textContent, '0.000');
  assert.equal(elements.get('connection').lastElementChild.textContent, 'ESP32: DISCONNECTED');
  assert.equal(elements.get('warning').hidden, true);
  Object.assign(context.packet, { connected: true, gps_fix: true, speed: 2.1,
    lat: 6.49545, road: 'SMOOTH', connection_message: 'ESP32 CONNECTED' });
  vm.runInContext('updateStatus(packet)', context);
  assert.equal(positions.length, 2);
  assert.equal(positions[1][0], 6.49545);
  assert.equal(elements.get('speed').textContent, '2.1');
});
