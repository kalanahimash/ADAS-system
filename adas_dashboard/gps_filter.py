"""Display-only GPS filtering. Raw readings are always retained for diagnostics."""
import time

# CONFIGURATION: km/h, meters, and consecutive valid packets respectively.
GPS_STATIONARY_SPEED_KMH = 2.0
GPS_DRIFT_RADIUS_METERS = 5.0
GPS_MOVEMENT_CONFIRM_SAMPLES = 3
GPS_STATIONARY_CONFIRM_SAMPLES = 3
MIN_POTHOLE_SPEED_KMH = 5.0
# Outliers need a consistent new cluster before relocating the display anchor.
GPS_MAX_JUMP_METERS = 30.0
GPS_JUMP_CONFIRM_SAMPLES = 5


class GPSFilter:
    def __init__(self, distance, movement_samples=None):
        self.distance = distance
        self.movement_samples = (GPS_MOVEMENT_CONFIRM_SAMPLES if movement_samples is None
                                 else movement_samples)
        self.lat = self.lon = None
        self.last_time = None
        self.reset_motion()

    def reset_motion(self):
        # A reconnect/no-fix gap must not inherit a previous MOVING decision.
        self.vehicle_state = "STATIONARY"
        self.moving_samples = self.stopped_samples = 0
        self.candidate = None
        self.jump_candidate = None
        self.jump_samples = 0

    def update(self, packet):
        now = time.monotonic()
        dt = min(2.0, max(0.0, now - self.last_time)) if self.last_time is not None else 0
        self.last_time = now
        raw_lat, raw_lon, raw_speed = packet['lat'], packet['lon'], packet['speed']
        quality = 'POOR' if packet['satellites'] <= 3 else 'FAIR' if packet['satellites'] <= 5 else 'GOOD'
        warning = 'GPS SIGNAL WEAK' if quality == 'POOR' else None
        reason = 'stationary drift held'
        display_speed = 0.0
        if not packet['gps_fix']:
            self.reset_motion()
            warning, reason = 'GPS SIGNAL WEAK', 'no valid GPS fix'
        else:
            if self.lat is None:
                self.lat, self.lon = raw_lat, raw_lon
            distance = self.distance(self.lat, self.lon, raw_lat, raw_lon)
            # A generous speed-based allowance prevents rejecting ordinary driving.
            travel_allowance = raw_speed / 3.6 * dt * 3 + GPS_DRIFT_RADIUS_METERS
            jump = distance > max(GPS_MAX_JUMP_METERS, travel_allowance)
            jump_held = False
            if jump:
                consistent = self.jump_candidate is not None and self.distance(
                    *self.jump_candidate, raw_lat, raw_lon) <= max(GPS_DRIFT_RADIUS_METERS, travel_allowance)
                self.jump_samples = self.jump_samples + 1 if consistent else 1
                # Keep the candidate anchored so scattered outliers cannot chain together.
                if not consistent:
                    self.jump_candidate = (raw_lat, raw_lon)
                jump_held = self.jump_samples < GPS_JUMP_CONFIRM_SAMPLES
            else:
                self.jump_candidate, self.jump_samples = None, 0
            if jump_held:
                self.vehicle_state = 'STATIONARY'
                self.moving_samples = self.stopped_samples = 0
                self.candidate = None
                warning, reason = 'GPS SIGNAL WEAK', 'GPS jump awaiting confirmation'
            elif raw_speed >= GPS_STATIONARY_SPEED_KMH or distance >= GPS_DRIFT_RADIUS_METERS:
                self.stopped_samples = 0
                coherent = self.candidate is None or self.distance(
                    *self.candidate, raw_lat, raw_lon) <= max(GPS_DRIFT_RADIUS_METERS, travel_allowance)
                self.moving_samples = self.moving_samples + 1 if coherent else 1
                if jump:
                    # The consistent jump samples already confirmed this relocation.
                    self.moving_samples = max(self.moving_samples, self.movement_samples)
                self.candidate = (raw_lat, raw_lon)
                if self.vehicle_state == 'MOVING' or self.moving_samples >= self.movement_samples:
                    self.vehicle_state = 'MOVING'
                    self.lat, self.lon = raw_lat, raw_lon
                    display_speed = raw_speed if raw_speed >= GPS_STATIONARY_SPEED_KMH else 0.0
                    reason = 'movement confirmed'
                    self.jump_candidate, self.jump_samples = None, 0
                else:
                    reason = 'movement awaiting confirmation'
            else:
                self.moving_samples, self.candidate = 0, None
                self.stopped_samples += 1
                if self.stopped_samples >= GPS_STATIONARY_CONFIRM_SAMPLES:
                    self.vehicle_state = 'STATIONARY'
                # Freeze coordinates and show zero immediately, even during stop confirmation.
        return dict(raw_lat=raw_lat, raw_lon=raw_lon, raw_speed=raw_speed,
                    lat=self.lat, lon=self.lon, display_lat=self.lat, display_lon=self.lon,
                    speed=display_speed, vehicle_state=self.vehicle_state,
                    gps_quality=quality, gps_warning=warning, gps_filter_reason=reason)
