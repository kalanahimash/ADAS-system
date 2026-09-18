import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from serial_reader import VehicleMonitor, haversine
from gps_filter import GPSFilter, GPS_JUMP_CONFIRM_SAMPLES
import app as dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "potholes.json"
        # Storage/API tests confirm immediately; GPS tests below use production defaults.
        self.monitor = VehicleMonitor(self.path, simulation=False,
                                      gps_filter=GPSFilter(haversine, movement_samples=1))
        self.packet = dict(lat=6.495461, lon=79.986114, speed=32.4, satellites=7,
                           az=1.38, vibration=.38, road="POTHOLE", confidence=86)

    def tearDown(self):
        self.monitor.stop()
        self.directory.cleanup()

    def ingest(self, **changes):
        return self.monitor.ingest(json.dumps(self.packet | changes))

    def test_duplicate_average_and_persistence(self):
        self.ingest()
        self.ingest(lat=self.packet["lat"] + .00004, confidence=96, vibration=.7)
        records = json.loads(self.path.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["report_count"], 2)
        self.assertAlmostEqual(records[0]["lat"], self.packet["lat"] + .00002)
        self.assertEqual(records[0]["confidence"], 91)
        self.assertEqual(records[0]["severity"], "high")
        self.assertEqual(VehicleMonitor(self.path, simulation=False).potholes(), records)
        self.ingest(lat=self.packet["lat"] + .00015)
        self.assertEqual(len(self.monitor.potholes()), 2)

    def test_invalid_and_no_fix_packets(self):
        for raw in ('garbage', '[]', '{}', '{"speed":NaN}', b'\xff'):
            self.assertFalse(self.monitor.ingest(raw))
        for change in (dict(speed=float('inf')), dict(satellites=2.5), dict(road='BAD'), dict(speed=True)):
            self.assertFalse(self.ingest(**change))
        for lat, lon in ((0, 0), (91, 79), (6, 181), (None, None)):
            self.assertTrue(self.ingest(lat=lat, lon=lon))
            self.assertFalse(self.monitor.status()["gps_fix"])
        self.assertEqual(self.monitor.potholes(), [])

    def test_confirmation_proximity_and_stale_connection(self):
        self.ingest(confidence=50)
        self.assertIsNone(self.monitor.status()["nearest_pothole"])
        self.ingest(confidence=50)
        for _ in range(GPS_JUMP_CONFIRM_SAMPLES):
            self.ingest(road="SMOOTH", lat=self.packet["lat"] + .00065)
        self.assertAlmostEqual(self.monitor.status()["nearest_pothole"]["distance_m"], 72, delta=1)
        for _ in range(GPS_JUMP_CONFIRM_SAMPLES):
            self.ingest(road="SMOOTH", lat=self.packet["lat"] + .002)
        self.assertIsNone(self.monitor.status()["nearest_pothole"])
        self.monitor.last_packet = time.monotonic() - 4
        self.assertFalse(self.monitor.status()["connected"])
        self.assertEqual(self.monitor.status()["connection_message"], "ESP32 DISCONNECTED")
        self.assertIsNone(self.monitor.status()["nearest_pothole"])

    def test_corrupt_file_preserved(self):
        self.path.write_text("broken", encoding="utf-8")
        monitor = VehicleMonitor(self.path, simulation=False)
        monitor.ingest(json.dumps(self.packet))
        self.assertEqual(self.path.read_text(), "broken")
        self.assertIn("storage disabled", monitor.status()["storage_error"])

    def test_save_failure_does_not_stop_telemetry(self):
        with patch("serial_reader.os.replace", side_effect=OSError("disk error")):
            self.assertTrue(self.ingest())
        self.assertTrue(self.monitor.status()["connected"])
        self.assertIn("save failed", self.monitor.status()["storage_error"])
        self.ingest()
        self.assertIsNone(self.monitor.status()["storage_error"])
        self.assertEqual(json.loads(self.path.read_text())[0]["report_count"], 2)

    def test_routes(self):
        with patch.object(dashboard, "monitor", self.monitor):
            client = dashboard.app.test_client()
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/api/potholes').json, [])
            self.ingest()
            self.assertEqual(client.get('/api/status').json['speed'], 32.4)
            self.assertEqual(len(client.get('/api/potholes').json), 1)
            with client.get('/static/app.js') as response:
                self.assertEqual(response.status_code, 200)

    def test_serial_unavailable_and_shutdown(self):
        import serial
        with patch('serial.Serial', side_effect=serial.SerialException('port unavailable')):
            self.monitor.start()
            deadline = time.monotonic() + 1
            while not self.monitor.serial_error and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.monitor.status()['connection_message'], 'ESP32 DISCONNECTED')
            self.monitor.stop()
            self.assertFalse(self.monitor.thread.is_alive())

    def test_haversine(self):
        self.assertAlmostEqual(haversine(0, 0, 0, .001), 111.195, places=2)
        self.assertEqual(haversine(6, 79, 6, 79), 0)

    def test_second_server_cannot_share_windows_http_port(self):
        server = dashboard.LocalDashboardServer('127.0.0.1', 0, dashboard.app)
        try:
            with self.assertRaises(SystemExit):
                dashboard.LocalDashboardServer('127.0.0.1', server.server_port, dashboard.app)
        finally:
            server.server_close()

    def test_real_packet_and_exact_two_second_timeout(self):
        real = dict(gpsConnected=True, gpsValid=True, lat=6.495422, lon=79.986077,
                    speed=.52, satellites=5, ax=-.045, ay=-.088, az=1.027,
                    acceleration=1.032, vibration=.032, road="SMOOTH", confidence=95)
        with patch('serial_reader.time.monotonic', return_value=100):
            self.assertTrue(self.monitor.ingest(json.dumps(real)))
            state = self.monitor.status()
            for key, value in real.items():
                self.assertEqual(state['raw_speed' if key == 'speed' else key], value)
            self.assertEqual(state['speed'], 0)
            self.assertEqual(state['source'], 'ESP32')
            received_at = state['updated_at']
            self.assertIsNotNone(received_at)
        with patch('serial_reader.time.monotonic', return_value=101.99):
            self.assertTrue(self.monitor.status()['connected'])
        with patch('serial_reader.time.monotonic', return_value=102.01):
            self.assertFalse(self.monitor.ingest('invalid JSON'))
            state = self.monitor.status()
            self.assertFalse(state['connected'])
            self.assertEqual(state['source'], 'ESP32')
            for key in ('speed', 'satellites', 'vibration', 'confidence'):
                self.assertEqual(state[key], 0)
            for key in ('gpsConnected', 'gpsValid', 'gps_fix'):
                self.assertFalse(state[key])
            self.assertEqual(state['road'], 'DISCONNECTED')
            self.assertEqual(state['lat'], real['lat'])
            self.assertEqual(state['lon'], real['lon'])
            self.assertEqual(state['updated_at'], received_at)
            self.assertEqual(self.monitor.state['raw_speed'], .52)
            self.monitor.ingest(json.dumps(real))
            self.assertTrue(self.monitor.status()['connected'])

    def test_explicit_gps_flags_prevent_false_potholes(self):
        self.ingest(gpsConnected=True, gpsValid=False)
        self.assertTrue(self.monitor.status()['connected'])
        self.assertFalse(self.monitor.status()['gps_fix'])
        self.assertEqual(self.monitor.potholes(), [])
        self.assertFalse(self.ingest(gpsValid='true'))

    def test_concurrent_starts_choose_serial_once_and_never_simulate(self):
        entered = threading.Event()
        def reader():
            entered.set()
            self.monitor.halt.wait(2)
        with patch.object(self.monitor, '_serial', side_effect=reader) as serial_reader, \
             patch.object(self.monitor, '_simulate') as simulator:
            callers = [threading.Thread(target=self.monitor.start) for _ in range(8)]
            for thread in callers:
                thread.start()
            for thread in callers:
                thread.join()
            self.assertTrue(entered.wait(1))
            serial_reader.assert_called_once()
            simulator.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.monitor._simulate()

    def test_simulation_storage_is_separate(self):
        self.ingest()
        original = self.path.read_text()
        demo = VehicleMonitor(self.path, simulation=True)
        self.assertEqual(demo.potholes(), [])
        demo.ingest(json.dumps(self.packet))
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(demo.path.name, 'potholes.simulation.json')
        self.assertEqual(demo.status()['source'], 'SIMULATION')

    def test_fragmented_serial_invalid_lines_unplug_and_reconnect(self):
        import serial
        packet = json.dumps(self.packet | dict(road='SMOOTH')).encode() + b'\n'
        got_first = threading.Event()
        disconnected = threading.Event()
        release_reconnect = threading.Event()
        reconnected = threading.Event()
        owner = self
        class Device:
            def __init__(self, first):
                self.first = first
                self.parts = [b'not json\n', packet[:20], packet[20:]] if first else [packet]
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            @property
            def in_waiting(self):
                return 4096
            def read(self, size):
                if self.parts:
                    return self.parts.pop(0)
                if self.first:
                    got_first.set()
                    raise serial.SerialException('USB unplugged')
                reconnected.set()
                owner.monitor.halt.wait(.01)
                return b''
        calls = 0
        def open_port(*args, **kwargs):
            nonlocal calls
            calls += 1
            self.assertEqual(args[:2], ('COM4', 115200))
            if calls == 2:
                disconnected.set()
                release_reconnect.wait(1)
            return Device(calls == 1)
        with patch('serial.Serial', side_effect=open_port), patch('serial_reader.RECONNECT_SECONDS', .01):
            self.monitor.start()
            self.assertTrue(got_first.wait(1))
            self.assertTrue(disconnected.wait(1))
            self.assertEqual(self.monitor.status()['speed'], 0)
            self.assertFalse(self.monitor.status()['connected'])
            release_reconnect.set()
            self.assertTrue(reconnected.wait(1))
            self.assertTrue(self.monitor.status()['connected'])
            self.assertEqual(self.monitor.status()['speed'], self.packet['speed'])
            self.assertEqual(self.monitor.invalid_packets, 1)
            self.monitor.stop()


if __name__ == '__main__':
    unittest.main()
