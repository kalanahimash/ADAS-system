"""Local ADAS dashboard. Run: python app.py"""
import logging
import socket
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from serial_reader import VehicleMonitor
from werkzeug.serving import ThreadedWSGIServer

# CONFIGURATION: Set False and select your ESP32's Windows Device Manager COM port.
SIMULATION_MODE = False
SERIAL_PORT = "COM4"
SERIAL_BAUD = 115200
# GPS drift and minimum pothole speed settings are near the top of gps_filter.py.

app = Flask(__name__)
monitor = VehicleMonitor(Path(__file__).with_name("potholes.json"),
                         simulation=SIMULATION_MODE, port=SERIAL_PORT, baud=SERIAL_BAUD)


class LocalDashboardServer(ThreadedWSGIServer):
    # Werkzeug's default SO_REUSEADDR permits duplicate listeners on Windows.
    # Reserve this address exclusively before any telemetry thread is started.
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify(monitor.status())


@app.get("/api/potholes")
def potholes():
    return jsonify(monitor.potholes(include_history=request.args.get('include_history') == '1'))


def change_pothole(pothole_id, status):
    try:
        record = monitor.update_pothole(pothole_id, status)
    except OSError as exc:
        return jsonify(error=f"Could not save change: {exc}"), 503
    if record is None:
        return jsonify(error="Pothole not found"), 404
    return jsonify(record)


@app.delete('/api/potholes/<pothole_id>')
def delete_pothole(pothole_id):
    return change_pothole(pothole_id, 'deleted')


@app.patch('/api/potholes/<pothole_id>')
def repair_pothole(pothole_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get('status') != 'repaired':
        return jsonify(error='Expected {"status":"repaired"}'), 400
    return change_pothole(pothole_id, 'repaired')


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # Bind HTTP BEFORE starting the reader. A second launch cannot start a
    # competing telemetry thread while an older dashboard owns port 5000.
    server = LocalDashboardServer("127.0.0.1", 5000, app)
    logging.info("Dashboard http://127.0.0.1:5000 | source=%s | port=%s | baud=%s | file=%s",
                 monitor.source, SERIAL_PORT, SERIAL_BAUD, Path(__file__).resolve())
    logging.info("Restart this process after changing SIMULATION_MODE. No reloader is running.")
    try:
        monitor.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        server.server_close()
