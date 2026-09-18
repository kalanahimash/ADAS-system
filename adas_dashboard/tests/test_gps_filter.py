import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as dashboard
from gps_filter import GPS_JUMP_CONFIRM_SAMPLES, GPS_MOVEMENT_CONFIRM_SAMPLES
from serial_reader import VehicleMonitor


class GPSAndManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'potholes.json'
        self.monitor = VehicleMonitor(self.path, simulation=False)
        self.packet = dict(gpsConnected=True, gpsValid=True, lat=6.495421, lon=79.986075,
                           speed=.52, satellites=5, ax=-.045, ay=-.088, az=1.027,
                           acceleration=1.032, vibration=.032, road='SMOOTH', confidence=95)
        self.tick = 1000.0

    def tearDown(self):
        self.monitor.stop()
        self.temp.cleanup()

    def send(self, **changes):
        self.tick += .5
        with patch('serial_reader.time.monotonic', return_value=self.tick):
            self.assertTrue(self.monitor.ingest(json.dumps(self.packet | changes)))
            return self.monitor.status()

    def create_pothole(self):
        for _ in range(GPS_MOVEMENT_CONFIRM_SAMPLES):
            self.send(speed=20)
        state = self.send(speed=20, road='POTHOLE', vibration=.6)
        self.assertTrue(state['pothole_recorded'])
        return self.monitor.potholes()[0]

    def test_stationary_drift_for_several_minutes(self):
        initial = self.send()
        for i in range(600):
            state = self.send(lat=self.packet['lat'] + (i % 7 - 3) * .000002,
                              lon=self.packet['lon'] + (i % 5 - 2) * .000002,
                              speed=.3 + (i % 8) * .1)
            self.assertEqual((state['lat'], state['lon']), (initial['lat'], initial['lon']))
            self.assertEqual(state['vehicle_state'], 'STATIONARY')
            self.assertEqual(state['speed'], 0)
            self.assertEqual(state['display_lat'], state['lat'])
        self.assertNotEqual(state['raw_lat'], state['lat'])
        self.assertGreater(state['raw_speed'], 0)

    def test_movement_confirmation_and_stop_confirmation(self):
        self.send()
        spike = self.send(speed=8, lat=self.packet['lat'] + .00001)
        self.assertEqual(spike['speed'], 0)
        self.send()  # interrupts movement confirmation
        for index in range(GPS_MOVEMENT_CONFIRM_SAMPLES):
            state = self.send(speed=10, lat=self.packet['lat'] + (index + 1) * .00001)
            self.assertEqual(state['vehicle_state'], 'MOVING' if index == 2 else 'STATIONARY')
        self.assertEqual(state['speed'], 10)
        location = state['lat']
        stopped = self.send(lat=location, speed=.8)
        self.assertEqual(stopped['speed'], 0)
        self.assertEqual(stopped['vehicle_state'], 'MOVING')
        self.send(lat=location)
        self.assertEqual(self.send(lat=location)['vehicle_state'], 'STATIONARY')

    def test_slow_consistent_displacement_is_accepted(self):
        self.send()
        for _ in range(GPS_MOVEMENT_CONFIRM_SAMPLES):
            state = self.send(lat=self.packet['lat'] + .00006, speed=1)
        self.assertEqual(state['lat'], self.packet['lat'] + .00006)
        self.assertEqual(state['vehicle_state'], 'MOVING')
        self.assertEqual(state['speed'], 0)

    def test_poor_gps_jump_is_held_and_persistent_relocation_recovers(self):
        initial = self.send(satellites=3)
        self.assertTrue(initial['gps_fix'])  # satellite count alone does not reject a fix
        self.assertEqual(initial['gps_quality'], 'POOR')
        for _ in range(GPS_JUMP_CONFIRM_SAMPLES - 1):
            state = self.send(lat=self.packet['lat'] + .01, satellites=2)
            self.assertEqual(state['lat'], initial['lat'])
            self.assertEqual(state['gps_warning'], 'GPS SIGNAL WEAK')
        state = self.send(lat=self.packet['lat'] + .01, satellites=2)
        self.assertEqual(state['lat'], self.packet['lat'] + .01)
        self.assertEqual(state['speed'], 0)

    def test_scattered_large_outliers_never_accumulate_confirmation(self):
        initial = self.send()
        for i in range(20):
            state = self.send(lat=self.packet['lat'] + (.01 if i % 2 else -.01), satellites=2)
            self.assertEqual(state['lat'], initial['lat'])

    def test_satellite_quality_and_invalid_fix(self):
        for satellites, quality in ((0, 'POOR'), (3, 'POOR'), (4, 'FAIR'), (5, 'FAIR'), (6, 'GOOD')):
            self.assertEqual(self.send(satellites=satellites)['gps_quality'], quality)
        self.create_pothole()
        location = self.monitor.state['lat']
        state = self.send(gpsValid=False, lat=0, lon=0, speed=20, road='POTHOLE')
        self.assertEqual(state['lat'], location)
        self.assertEqual(state['raw_lat'], 0)
        self.assertEqual(state['vehicle_state'], 'STATIONARY')
        self.assertEqual(state['speed'], 0)
        self.assertFalse(state['pothole_recorded'])
        self.assertEqual(self.send(speed=20)['speed'], 0)  # reconfirm after no-fix

    def test_stationary_shaking_and_minimum_speed_gate(self):
        for _ in range(20):
            self.send(road='POTHOLE', vibration=2, az=3)
        self.assertEqual(self.monitor.potholes(), [])
        for speed in (3, 5):
            for _ in range(5):
                self.send(speed=speed, road='POTHOLE', vibration=2)
        self.assertEqual(self.monitor.potholes(), [])
        self.send(speed=6, road='POTHOLE', vibration=.5)
        record = self.monitor.potholes()[0]
        self.assertEqual(record['lat'], self.monitor.state['display_lat'])
        self.assertEqual(record['status'], 'active')

    def test_disconnect_preserves_anchor_and_requires_new_movement_confirmation(self):
        self.create_pothole()
        anchor = self.monitor.state['lat']
        self.tick += 3
        with patch('serial_reader.time.monotonic', return_value=self.tick):
            state = self.monitor.status()
        self.assertFalse(state['connected'])
        self.assertEqual(state['speed'], 0)
        state = self.send(speed=20)
        self.assertEqual(state['lat'], anchor)
        self.assertEqual(state['vehicle_state'], 'STATIONARY')

    def test_repaired_and_deleted_history_survives_restart_and_reports(self):
        record = self.create_pothole()
        with patch.object(dashboard, 'monitor', self.monitor):
            client = dashboard.app.test_client()
            response = client.patch('/api/potholes/' + record['id'], json={'status': 'repaired'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('repaired_at', response.json)
            self.assertEqual(client.get('/api/potholes').json, [])
            history = client.get('/api/potholes?include_history=1').json
            self.assertEqual(history[0]['status'], 'repaired')
            self.send(speed=20, road='POTHOLE')
            self.assertEqual(self.monitor.potholes(), [])
            self.assertIsNone(self.send(speed=20)['nearest_pothole'])
            restarted = VehicleMonitor(self.path, simulation=False)
            self.assertEqual(restarted.potholes(), [])
            self.assertEqual(restarted.potholes(True)[0]['status'], 'repaired')
            self.assertEqual(client.delete('/api/potholes/' + record['id']).status_code, 200)
            self.send(speed=20, road='POTHOLE')
            restarted = VehicleMonitor(self.path, simulation=False)
            self.assertEqual(restarted.potholes(), [])
            self.assertEqual(restarted.potholes(True)[0]['status'], 'deleted')
            self.assertEqual(len(restarted.potholes(True)), 1)
            self.assertEqual(client.delete('/api/potholes/missing').status_code, 404)
            self.assertEqual(client.patch('/api/potholes/x', json={'status': 'oops'}).status_code, 400)

    def test_failed_management_save_keeps_marker_active(self):
        record = self.create_pothole()
        original = self.path.read_text()
        with patch.object(dashboard, 'monitor', self.monitor), \
             patch('serial_reader.os.replace', side_effect=OSError('disk full')):
            response = dashboard.app.test_client().delete('/api/potholes/' + record['id'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.monitor.potholes()[0]['status'], 'active')
        self.assertEqual(self.path.read_text(), original)

    def test_legacy_records_default_to_active(self):
        record = self.create_pothole()
        del record['status']
        self.path.write_text(json.dumps([record]))
        restarted = VehicleMonitor(self.path, simulation=False)
        self.assertEqual(restarted.potholes()[0]['status'], 'active')


if __name__ == '__main__':
    unittest.main()
