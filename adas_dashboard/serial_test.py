import serial
import json

PORT = "COM4"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=1)

print(f"Listening on {PORT} at {BAUD} baud...")

while True:
    line = ser.readline().decode("utf-8", errors="ignore").strip()

    if not line:
        continue

    print("RAW:", line)

    try:
        data = json.loads(line)

        print("PARSED:")
        print("GPS valid:", data.get("gpsValid"))
        print("Latitude:", data.get("lat"))
        print("Longitude:", data.get("lon"))
        print("Speed:", data.get("speed"))
        print("Vibration:", data.get("vibration"))
        print("Road:", data.get("road"))
        print("-------------------------")

    except json.JSONDecodeError:
        print("Not valid JSON")