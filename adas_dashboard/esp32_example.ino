/* ESP32 Arduino USB telemetry example, no additional libraries required.
 * Select your ESP32 board/COM port, upload, then CLOSE Serial Monitor before
 * starting Flask with the matching SERIAL_PORT.
 * DEMO FIRMWARE ONLY: this sketch generates synthetic sensor/GPS values even
 * when the Python dashboard is in hardware mode. Do not upload it over your
 * working real-sensor firmware. Python cannot infer whether firmware is synthetic.
 * All measurements below are SYNTHETIC; replace with real sensor readings.
 */
#include <Arduino.h>
#include <math.h>

unsigned long lastPacketMs = 0;
float angle = 0.0f;
int lastZone = -1;

void setup() {
  Serial.begin(115200);
  // For native USB boards, enable USB CDC On Boot in Arduino board settings.
}

void loop() {
  unsigned long now = millis();
  unsigned long delta = now - lastPacketMs;
  if (delta < 500) return;
  lastPacketMs = now;

  // CONFIGURATION: replace this block with GPS/IMU + detector measurements.
  float speed = 32.5f + 10.0f * sinf(now / 9000.0f); // km/h
  angle = fmodf(angle + speed / 3.6f * (delta / 1000.0f) / 65.0f, 2.0f * PI);
  double lat = 6.495461 + 65.0 * sin(angle) / 111195.0;
  double lon = 79.986114 + 65.0 * cos(angle) / (111195.0 * cos(6.495461 * PI / 180.0));
  const float targets[] = {0.8f, 2.8f, 4.9f};
  const float impacts[] = {0.22f, 0.43f, 0.72f};
  int zone = -1;
  for (int i = 0; i < 3; i++) if (fabsf(angle - targets[i]) < 0.07f) zone = i;
  bool impact = zone >= 0 && zone != lastZone;
  lastZone = zone;
  bool rough = angle > 1.7f && angle < 2.1f;
  const char* road = impact ? "POTHOLE" : rough ? "ROUGH" : "SMOOTH";
  float vibration = impact ? impacts[zone] : rough ? 0.19f : 0.05f;
  float az = 1.0f + vibration; // g, includes gravity in this demonstration
  int satellites = 9;
  int confidence = impact ? 88 + zone * 4 : 0;
  bool gpsFix = true;

  // One complete JSON object and one newline per packet. No diagnostic text.
  Serial.print("{\"lat\":");
  if (gpsFix) Serial.print(lat, 6); else Serial.print("null");
  Serial.print(",\"lon\":");
  if (gpsFix) Serial.print(lon, 6); else Serial.print("null");
  Serial.printf(",\"speed\":%.1f,\"satellites\":%d,\"az\":%.3f,\"vibration\":%.3f,\"road\":\"%s\",\"confidence\":%d}\n",
                speed, satellites, az, vibration, road, confidence);
}
