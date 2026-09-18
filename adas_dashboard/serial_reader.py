"""Thread-safe telemetry, atomic JSON storage, and reconnecting serial reader."""
import copy
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from gps_filter import GPSFilter, MIN_POTHOLE_SPEED_KMH

LOG = logging.getLogger(__name__)
EARTH_RADIUS = 6371000
DEDUPE_METERS = 8
# A report with >=70% confidence, or two observations, is considered confirmed.
CONFIRM_CONFIDENCE = 70
STALE_SECONDS = 2
RECONNECT_SECONDS = 2


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(min(1, max(0, a))))


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Expected a JSON number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite telemetry")
    return value


def coordinates(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 or lon != 0)


def parse_packet(raw):
    packet = json.loads(raw)
    if not isinstance(packet, dict):
        raise ValueError("Expected an object")
    result = {key: number(packet[key]) for key in
              ("speed", "satellites", "az", "vibration", "confidence")}
    # Preserve the extended ESP32 fields; older packets may omit these sensors.
    for key in ("ax", "ay", "acceleration"):
        result[key] = number(packet[key]) if key in packet else None
    # Null GPS values are accepted while the GPS receiver acquires a fix.
    for key in ("lat", "lon"):
        result[key] = None if packet[key] is None else number(packet[key])
    if not 0 <= result["speed"] <= 300 or not 0 <= result["confidence"] <= 100:
        raise ValueError("Out-of-range telemetry")
    if not 0 <= result["satellites"] <= 100 or not result["satellites"].is_integer():
        raise ValueError("Invalid satellite count")
    if result["vibration"] < 0:
        raise ValueError("Negative vibration")
    result["satellites"] = int(result["satellites"])
    result["road"] = packet["road"]
    if result["road"] not in ("SMOOTH", "ROUGH", "POTHOLE"):
        raise ValueError("Unknown road condition")
    result["gps_fix"] = (result["lat"] is not None and result["lon"] is not None
                         and coordinates(result["lat"], result["lon"]))
    for key in ("gpsConnected", "gpsValid"):
        value = packet.get(key, result["gps_fix"])
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        result[key] = value
    result["gps_fix"] = result["gps_fix"] and result["gpsConnected"] and result["gpsValid"]
    return result


class VehicleMonitor:
    def __init__(self, path, *, simulation, port="COM4", baud=115200, gps_filter=None):
        self._simulation = simulation
        # Simulation can NEVER load or write the hardware pothole file.
        self.path = path.with_name(path.stem + ".simulation.json") if simulation else path
        path = self.path
        self.port, self.baud = port, baud
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.Lock()
        self.halt = threading.Event()
        self.thread = None
        self.connected = False
        self.last_packet = 0
        self.storage_error = None
        self.serial_error = None
        self.invalid_packets = 0
        self.gps_filter = gps_filter or GPSFilter(haversine)
        self.state = dict(lat=None, lon=None, speed=0, satellites=0, ax=0, ay=0, az=0,
                          acceleration=0, gpsConnected=False, gpsValid=False,
                          vibration=0, road="DISCONNECTED", confidence=0, gps_fix=False,
                          updated_at=None, raw_lat=None, raw_lon=None, raw_speed=0,
                          display_lat=None, display_lon=None, vehicle_state="STATIONARY",
                          gps_quality="POOR", gps_warning="GPS SIGNAL WEAK",
                          gps_filter_reason="waiting for GPS", pothole_recorded=False)
        self.records = []
        try:
            if path.exists():
                records = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("Pothole file must contain an array")
                for r in records:
                    if not isinstance(r, dict):
                        raise ValueError("Invalid stored pothole")
                    if not coordinates(number(r["lat"]), number(r["lon"])):
                        raise ValueError("Invalid stored coordinates")
                    count = number(r["report_count"])
                    if count < 1 or not count.is_integer() or not 0 <= number(r["confidence"]) <= 100:
                        raise ValueError("Invalid stored report")
                    if r["severity"] not in ("low", "medium", "high") or not isinstance(r["id"], str):
                        raise ValueError("Invalid stored marker")
                    datetime.fromisoformat(r["last_seen"])
                    r.setdefault("status", "active")
                    if r["status"] not in ("active", "repaired", "deleted"):
                        raise ValueError("Invalid pothole status")
                if len({r['id'] for r in records}) != len(records):
                    raise ValueError("Duplicate pothole IDs")
                self.records = records
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Preserve the damaged file; never silently overwrite user data.
            self.storage_error = f"Cannot load potholes.json; storage disabled: {exc}"
            LOG.error(self.storage_error)

    @property
    def simulation(self):
        return self._simulation

    @property
    def source(self):
        return "SIMULATION" if self.simulation else "ESP32"

    def start(self):
        with self.lifecycle_lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.halt.clear()
            if self.simulation:
                target = self._simulate
            else:
                target = self._serial
            self.thread = threading.Thread(target=target,
                                           daemon=True, name="adas-telemetry")
            self.thread.start()

    def stop(self):
        with self.lifecycle_lock:
            self.halt.set()
            if self.thread:
                self.thread.join(timeout=3)
            with self.lock:
                self.connected = False
                self.gps_filter.reset_motion()

    def _check_timeout(self):
        with self.lock:
            if self.connected and time.monotonic() - self.last_packet >= STALE_SECONDS:
                self.connected = False
                if not self.simulation:
                    LOG.warning("ESP32 disconnected / data timeout")

    def potholes(self, include_history=False):
        with self.lock:
            return copy.deepcopy([r for r in self.records if include_history or r['status'] == 'active'])

    def update_pothole(self, pothole_id, status):
        """Durable tombstones keep repairs/deletions from reappearing on re-report."""
        if status not in ('repaired', 'deleted'):
            raise ValueError('Unsupported pothole status')
        with self.lock:
            if self.storage_error and 'storage disabled' in self.storage_error:
                raise OSError(self.storage_error)
            record = next((p for p in self.records if p['id'] == pothole_id), None)
            if record is None or record['status'] == 'deleted':
                return None
            before = copy.deepcopy(record)
            record.update(status=status)
            record[status + '_at'] = datetime.now(timezone.utc).isoformat()
            try:
                self._save()
            except OSError:
                record.clear()
                record.update(before)
                raise
            return copy.deepcopy(record)

    def status(self):
        with self.lock:
            self._check_timeout()
            state = self.state.copy()
            age = time.monotonic() - self.last_packet if self.last_packet else None
            live = self.connected and age is not None and age < STALE_SECONDS
            state.update(connected=live, simulation=self.simulation, source=self.source,
                         connection_message=("Simulation active" if self.simulation else "ESP32 CONNECTED")
                         if live else ("Simulation disconnected" if self.simulation else "ESP32 DISCONNECTED"),
                         packet_age_seconds=round(age, 1) if age is not None else None,
                         storage_error=self.storage_error, serial_error=self.serial_error,
                         invalid_packets=self.invalid_packets, nearest_pothole=None,
                         pothole_count=sum(r['status'] == 'active' for r in self.records))
            if not live:
                # Retain coordinates and receive timestamp, never stale driving values.
                state.update(speed=0, satellites=0, ax=0, ay=0, az=0, acceleration=0,
                             vibration=0, road="DISCONNECTED", confidence=0,
                             gpsConnected=False, gpsValid=False, gps_fix=False,
                             vehicle_state="STATIONARY", gps_quality="POOR",
                             gps_warning="GPS SIGNAL WEAK", pothole_recorded=False)
            if live and state["gps_fix"]:
                nearby = [(haversine(state["lat"], state["lon"], p["lat"], p["lon"]), p)
                          for p in self.records
                          if p['status'] == 'active' and
                          (p["confidence"] >= CONFIRM_CONFIDENCE or p["report_count"] >= 2)]
                if nearby:
                    distance, pothole = min(nearby, key=lambda item: item[0])
                    if distance <= 100:
                        state["nearest_pothole"] = dict(id=pothole["id"], distance_m=round(distance),
                                                         severity=pothole["severity"])
            return state

    def ingest(self, raw):
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            packet = parse_packet(raw)
        except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, RecursionError):
            with self.lock:
                self.invalid_packets += 1
            return False
        with self.lock:
            was_connected = self.connected and time.monotonic() - self.last_packet < STALE_SECONDS
            if not was_connected:
                self.gps_filter.reset_motion()
            filtered = self.gps_filter.update(packet)
            self.state.update(packet, updated_at=datetime.now(timezone.utc).isoformat())
            self.state.update(filtered, pothole_recorded=False)
            self.last_packet = time.monotonic()
            self.connected = True
            self.serial_error = None
            if not self.simulation:
                if not was_connected:
                    LOG.info("ESP32 connected on %s", self.port)
                LOG.info("ESP32 packet received: %s", raw.strip())
            if (packet["road"] == "POTHOLE" and packet["gps_fix"]
                    and filtered['vehicle_state'] == 'MOVING'
                    and filtered['speed'] > MIN_POTHOLE_SPEED_KMH):
                self.state['pothole_recorded'] = self._record(self.state)
        return True

    def _record(self, packet):
        if self.storage_error and "storage disabled" in self.storage_error:
            return False
        now = datetime.now(timezone.utc).isoformat()
        severity = "high" if packet["vibration"] >= .6 else "medium" if packet["vibration"] >= .3 else "low"
        nearby = [(haversine(packet["lat"], packet["lon"], p["lat"], p["lon"]), p)
                  for p in self.records]
        distance, existing = min(nearby, key=lambda item: item[0]) if nearby else (math.inf, None)
        if distance <= DEDUPE_METERS:
            if existing['status'] != 'active':
                return False
            count = existing["report_count"]
            for key in ("lat", "lon", "confidence"):
                existing[key] = (existing[key] * count + packet[key]) / (count + 1)
            existing.update(report_count=count + 1, last_seen=now)
            if ["low", "medium", "high"].index(severity) > ["low", "medium", "high"].index(existing["severity"]):
                existing["severity"] = severity
        else:
            self.records.append(dict(id=uuid.uuid4().hex, lat=packet["lat"], lon=packet["lon"],
                                     confidence=packet["confidence"], severity=severity,
                                     report_count=1, last_seen=now, status='active'))
        try:
            self._save()
        except OSError as exc:
            self.storage_error = f"Potholes remain in memory; save failed: {exc}"
            LOG.error(self.storage_error)
            return False
        return True

    def _save(self):
        try:
            temporary = self.path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self.records, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.storage_error = None
        except OSError as exc:
            self.storage_error = f"Pothole save failed: {exc}"
            LOG.error(self.storage_error)
            raise

    def _serial(self):
        if self.simulation:
            raise RuntimeError("Serial input is disabled in simulation mode")
        try:
            import serial
        except ImportError:
            self.serial_error = "pyserial is missing. Run pip install -r requirements.txt"
            return
        while not self.halt.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=.5) as device:
                    buffer = bytearray()
                    discarding = False
                    while not self.halt.is_set():
                        chunk = device.read(min(device.in_waiting or 1, 4096))
                        self._check_timeout()
                        for byte in chunk:
                            if byte == 10:
                                if buffer and not discarding:
                                    self.ingest(bytes(buffer))
                                buffer.clear()
                                discarding = False
                            elif not discarding:
                                buffer.append(byte)
                                if len(buffer) > 4096:
                                    buffer.clear()
                                    discarding = True
                                    with self.lock:
                                        self.invalid_packets += 1
            except (serial.SerialException, OSError) as exc:
                with self.lock:
                    self.connected = False
                    self.serial_error = str(exc)
                LOG.warning("ESP32 disconnected / data timeout: %s; retrying %s in %ss",
                            exc, self.port, RECONNECT_SECONDS)
                self.halt.wait(RECONNECT_SECONDS)

    def _simulate(self):
        if not self.simulation:
            raise RuntimeError("Simulation is disabled in ESP32 mode")
        # Explicit demo only, using its own potholes.simulation.json store.
        lat0, lon0 = 6.495461, 79.986114
        radius = 65
        angle = 0.0
        start = time.monotonic()
        previous = start
        last_zone = None
        while not self.halt.is_set():
            now = time.monotonic()
            elapsed = now - start
            speed = 32.5 + 10 * math.sin(elapsed / 9)
            angle = (angle + speed / 3.6 * (now - previous) / radius) % (2 * math.pi)
            previous = now
            lat = lat0 + radius * math.sin(angle) / 111195
            lon = lon0 + radius * math.cos(angle) / (111195 * math.cos(math.radians(lat0)))
            zone = next((i for i, target in enumerate((.8, 2.8, 4.9))
                         if abs(angle - target) < .07), None)
            detected = zone is not None and zone != last_zone
            last_zone = zone
            road = "POTHOLE" if detected else "ROUGH" if 1.7 < angle < 2.1 else "SMOOTH"
            vibration = (.22, .43, .72)[zone] if detected else .19 if road == "ROUGH" else .04 + .025 * abs(math.sin(elapsed * 3))
            self.ingest(json.dumps(dict(lat=lat, lon=lon, speed=speed, satellites=9,
                                       az=1 + vibration, vibration=vibration, road=road,
                                       confidence=88 + (zone or 0) * 4 if detected else 0)))
            self.halt.wait(.5)
