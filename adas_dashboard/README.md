# STRADA — local ADAS pothole dashboard

Python Flask + pyserial + vanilla JavaScript + Leaflet/OpenStreetMap. No database or build step. Starts in **real ESP32 mode on COM4 at 115200 baud**.

## Run on Windows

Install Python 3.10 or newer. In PowerShell, open this directory:

```powershell
cd "C:\Users\Kalana\Documents\Projects\ADAS system\adas_dashboard"
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**. Stop the server with Ctrl+C.

Optional isolated environment (activation is not required):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

The server binds only to your laptop. Internet is required for Leaflet, OpenStreetMap tiles, and the optional Google fonts. Telemetry and JSON storage run locally; if the map cannot load, the telemetry dashboard still works. System fonts are used if Google fonts are unavailable.

## Connect an ESP32

Edit the configuration near the top of `app.py`:

```python
SIMULATION_MODE = False
SERIAL_PORT = "COM4"  # Windows Device Manager → Ports (COM & LPT)
SERIAL_BAUD = 115200
```

Restart the server after changing configuration: editing `app.py` does not change an already-running process. Close Arduino Serial Monitor, `serial_test.py`, and other programs using COM4. The reader retries every two seconds; unplugging the board does not stop Flask. After two seconds without a valid packet (or immediately on a serial error), `/api/status` returns **ESP32 DISCONNECTED**, `connected: false`, `source: "ESP32"`, zero speed/sensor values/satellites/confidence, false GPS flags, and `road: "DISCONNECTED"`. Last coordinates and the UTC receipt timestamp are retained. The frontend polls every 500 ms, so timeout visibility may take up to about 2.5 seconds. Port errors are available in `/api/status` and on the connection indicator's tooltip.

Send one UTF-8 JSON object per newline, e.g. twice a second:

```json
{"gpsConnected":true,"gpsValid":true,"lat":6.495422,"lon":79.986077,"speed":0.52,"satellites":5,"ax":-0.045,"ay":-0.088,"az":1.027,"acceleration":1.032,"vibration":0.032,"road":"SMOOTH","confidence":95}
```

`speed` is km/h; this UI assumes `az` and `vibration` are in g. `confidence` is 0–100, `satellites` is a nonnegative integer, and `road` is exactly `SMOOTH`, `ROUGH`, or `POTHOLE`. Convert sensor units on the ESP32 if needed. GPS flags must be JSON booleans; coordinates must also be in bounds for map updates/pothole recording. Use `null` for latitude/longitude when no GPS fix exists; `(0,0)` is treated as a no-fix sentinel. Legacy packets without GPS flags infer them from coordinates. Optional `ax`, `ay`, and `acceleration` are preserved (null if absent). Malformed packets are ignored and counted; they never refresh the two-second timeout. Serial buffering is bounded.

`esp32_example.ino` is **synthetic demo firmware**, not your real sensor firmware. Do not upload it over your working firmware: it generates synthetic values over USB regardless of the Python setting. The Python hardware reader reports what the board sends; it cannot determine whether the firmware itself is synthetic. `serial_test.py` is a real COM4 diagnostic, with no simulator; run it only while Flask is stopped.

## Simulation and map behavior

- Optional simulation runs only when the single `SIMULATION_MODE` setting in `app.py` is explicitly changed to `True` and the process is restarted. Serial and simulation threads never run together. Real mode has no fallback simulator.
- Simulation runs a repeating ~400 m route near Kalutara at 22.5–42.5 km/h, mostly smooth, with one rough section and three recurring impact locations.
- The first simulated impact arrives after approximately five seconds. It creates a marker and a detection warning. Subsequent loops demonstrate approach alerts and duplicate reports.
- Simulation uses **`potholes.simulation.json`**; hardware uses **`potholes.json`**. The three legacy demo markers were archived to `potholes.legacy-simulation.backup.json` and removed from the hardware store. Neither simulation file is loaded in hardware mode.
- Dragging the map pauses following. **Recenter vehicle** resumes it. The arrow identifies the vehicle; it does not indicate measured heading.
- Telemetry polls every 500 ms and pothole markers every two seconds. The map is created once; markers update in place. Vehicle coordinates/speed/sensors update directly from API values, with no frontend interpolation, extrapolation, randomization, or fallback movement. Lane animation pauses when stationary or disconnected. On disconnect, only the last position is retained; driving values reset as described above. Browser console logs each API status response.
- A confirmed pothole is one with average confidence ≥70% **or** at least two reports. Nearest confirmed potholes within 100 m trigger `POTHOLE AHEAD` with distance. This version has no measured heading: alerts include hazards in every direction, including behind the vehicle. `POTHOLE DETECTED` takes precedence for 2.2 seconds.

## Persistence and severity

Each detection is compared with all records using Haversine distance. The nearest record within 8 m gains one report, a report-weighted average position/confidence, and an updated UTC timestamp. Otherwise, a new UUID record is created. Repeated `POTHOLE` packets count as repeated reports; the firmware should emit an event once per impact if independent observations are required.

Illustrative severity thresholds: **low** vibration <0.30 g, **medium** 0.30–0.59 g, **high** ≥0.60 g. A record retains its highest observed severity. These are configurable prototype heuristics in `serial_reader.py`, not calibrated road safety ratings.

Writes use a temporary file and atomic replacement. Access is locked across the reader and request threads. A damaged JSON file is preserved and storage is disabled with an on-screen error, rather than overwriting it. Fix/back up that file and restart. A disk write failure keeps reports in memory and displays an error; the next report retries saving. Run one instance of this application against a given JSON file.

## HTTP API

- `GET /` — dashboard.
- `GET /api/status` — last telemetry, connection/fix status, timestamp, packet age, simulation flag, errors, invalid packet count, stored pothole count, and nearest confirmed pothole within 100 m (or `null`).
- `GET /api/potholes` — JSON array of `id`, `lat`, `lon`, `severity`, `report_count`, average `confidence`, and `last_seen` (UTC ISO 8601).

Start with `python app.py`, which binds HTTP port 5000 before starting the background reader exactly once. A second launch fails to bind HTTP before it can open another serial reader. Concurrent calls to start the same monitor are locked. Windows opens COM4 exclusively. Importing `app` for tests does not open a serial port. No Flask reloader is used. Startup logs show the source, port, baud, and exact app path; terminal logs report each valid ESP32 packet, connection transitions, and reconnect errors.

## Verification

```powershell
python -m unittest discover -s tests -v
node --test tests/test_frontend.cjs
```

Tests use temporary JSON files and do not modify your map data. Hardware integration still needs verification with your board, COM port, and sensor firmware.

## Real hardware verification

1. Stop the old Flask process with Ctrl+C. Close `serial_test.py` and Arduino Serial Monitor.
2. Run `python app.py` from this directory. Confirm the startup log says `source=ESP32`, `port=COM4`, `baud=115200`.
3. Open http://127.0.0.1:5000/api/status. With valid COM4 packets, expect `source: "ESP32"`, `simulation: false`, `connected: true`, the real sensor fields, and a changing `updated_at` receipt timestamp.
4. Refresh the dashboard (Ctrl+F5). Its badges must show `DATA SOURCE: ESP32` and `ESP32: CONNECTED`.
5. Unplug ESP32. Within about two seconds the API becomes disconnected, speed/vibration reset to zero, and coordinates stop changing. Refresh the API page manually to view the new JSON; the dashboard polls automatically.
6. Plug it back in. Open failures retry every two seconds. After the board boots and sends a valid packet, connected status and real values resume automatically.

If you ever see `source: "SIMULATION"` after setting `False`, an older process is still serving port 5000: stop that process and restart this app. No browser cache can change the running Python process's configuration.
