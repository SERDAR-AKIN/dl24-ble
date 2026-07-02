"""FastAPI server with WebSocket, recording, report download, and reference template."""

import asyncio
import csv
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dl24_ble import DL24Device, DL24Config, Measurement, Command

logger = logging.getLogger(__name__)

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)

# ── Reference template (healthy battery baseline) ──────────────────────
REFERENCE_CSV_FILE = "dl24_20260629_123707.csv"

_reference_template_cache: Optional[dict] = None


def _parse_runtime_to_seconds(runtime_str: str) -> int:
    try:
        parts = runtime_str.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return 0
    except (ValueError, AttributeError):
        return 0


def build_reference_template() -> dict:
    global _reference_template_cache
    if _reference_template_cache is not None:
        return _reference_template_cache

    fpath = LOG_DIR / REFERENCE_CSV_FILE
    if not fpath.exists():
        logger.warning(f"Reference CSV not found: {fpath}")
        return {"time_sec": [], "voltage_min": [], "voltage_max": [],
                "capacity_min": [], "capacity_max": [],
                "source_files": [], "max_runtime_sec": 0}

    times, volts, caps = [], [], []
    with open(fpath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = _parse_runtime_to_seconds(row.get("runtime", "0"))
            v = float(row.get("voltage_V", 0))
            c = float(row.get("capacity_Ah", 0))
            times.append(t)
            volts.append(v)
            caps.append(c)

    if not times:
        return {"time_sec": [], "voltage_min": [], "voltage_max": [],
                "capacity_min": [], "capacity_max": [],
                "source_files": [], "max_runtime_sec": 0}

    # Normalize capacity to start from 0
    cap_offset = caps[0]
    caps = [c - cap_offset for c in caps]

    voltage_min = [round(v * 0.95, 3) for v in volts]
    voltage_max = [round(v * 1.05, 3) for v in volts]
    capacity_min = [round(c * 0.95, 4) for c in caps]
    capacity_max = [round(c * 1.05, 4) for c in caps]

    name = REFERENCE_CSV_FILE.replace("dl24_", "").replace(".csv", "")
    _reference_template_cache = {
        "time_sec": times,
        "voltage_min": voltage_min,
        "voltage_max": voltage_max,
        "capacity_min": capacity_min,
        "capacity_max": capacity_max,
        "source_files": [name],
        "max_runtime_sec": times[-1],
    }
    return _reference_template_cache


device: Optional[DL24Device] = None
device_lock = asyncio.Lock()
clients: set[WebSocket] = set()
latest_measurement: Optional[Measurement] = None
recording: bool = False
csv_path: Optional[Path] = None
csv_file = None
csv_writer = None
test_start_v: float = 0.0
test_start_ts: Optional[datetime] = None


async def get_or_create_device() -> DL24Device:
    global device
    async with device_lock:
        if device is None or not device.is_connected:
            if device:
                try: await device.disconnect()
                except: pass

            await broadcast({"type": "status_log", "message": "Bluetooth cihaz taranıyor..."})
            device = DL24Device()
            device.on_measurement(on_device_measurement)
            ok = await device.connect()
            if not ok:
                await broadcast({"type": "status_log", "message": "Cihaz bulunamadı. Tekrar denemek için 🔗 Cihaza Bağlan butonuna tıklayın."})
                raise RuntimeError("Could not connect to DL24 device")

            addr = device.address
            await broadcast({"type": "status_log", "message": f"Cihaza bağlandı — {addr}"})
    return device


def _auto_start_recording():
    global csv_path, csv_file, csv_writer, recording, test_start_ts, test_start_v
    if recording:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = LOG_DIR / f"dl24_{ts}.csv"
    csv_file = open(csv_path, "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow([
        "timestamp", "voltage_V", "current_A", "power_W",
        "resistance_ohm", "capacity_Ah", "energy_Wh",
        "temperature_C", "runtime",
    ])
    test_start_ts = datetime.now()
    test_start_v = 0.0
    recording = True
    logger.info(f"Auto-recording: {csv_path}")


CURRENT_THRESHOLD = 0.05
_was_under_load = False


def on_device_measurement(m: Measurement):
    global latest_measurement, test_start_v, _was_under_load
    latest_measurement = m

    under_load = m.current > CURRENT_THRESHOLD

    if under_load and not recording:
        _auto_start_recording()
        asyncio.create_task(broadcast({
            "type": "record_status", "recording": True,
            "path": str(csv_path) if csv_path else "",
        }))
    elif not under_load and recording and _was_under_load:
        asyncio.create_task(_stop_and_report())

    _was_under_load = under_load

    if recording and csv_writer:
        now = datetime.now().isoformat()
        row = [
            now, f"{m.voltage:.3f}", f"{m.current:.4f}", f"{m.power:.2f}",
            f"{m.resistance:.2f}", f"{m.capacity_ah:.4f}", f"{m.energy_wh:.2f}",
            str(m.temperature), m.runtime_str,
        ]
        csv_writer.writerow(row)
        csv_file.flush()

    if test_start_v == 0 and m.voltage > 0:
        test_start_v = m.voltage

    data = json.dumps(m.to_dict() | {"recording": recording})
    for ws in clients:
        asyncio.create_task(_ws_send(ws, data))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("DL24 Web Server starting...")
    yield
    global device, csv_file
    if csv_file:
        csv_file.close()
    if device:
        await device.disconnect()
        device = None


app = FastAPI(title="DL24 Electronic Load", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML_TEMPLATE)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)

    # Try BLE connect but keep WS alive on failure so user can retry
    try:
        await broadcast({"type": "status_log", "message": "Web sayfası bağlandı, Bluetooth cihaz taranıyor..."})
        d = await get_or_create_device()
        await asyncio.sleep(0.5)
        try:
            await ws.send_text(json.dumps({
                "type": "status", "connected": True, "address": d.address,
                "recording": recording, "path": str(csv_path) if csv_path else "",
            }))
            if latest_measurement:
                await ws.send_text(json.dumps(latest_measurement.to_dict() | {"recording": recording}))
            template = build_reference_template()
            await ws.send_text(json.dumps({"type": "reference_template", "template": template}))
        except (WebSocketDisconnect, RuntimeError):
            return
    except RuntimeError as e:
        await broadcast({"type": "status_log", "message": f"Hata: {e}"})
        try:
            await ws.send_text(json.dumps({
                "type": "status", "connected": False,
                "recording": recording, "path": str(csv_path) if csv_path else "",
                "error": str(e),
            }))
        except (WebSocketDisconnect, RuntimeError):
            pass

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                await handle_ws_message(ws, msg)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except (WebSocketDisconnect, RuntimeError):
                    raise
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        clients.discard(ws)


async def handle_ws_message(ws: WebSocket, msg: str):
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return

    cmd = data.get("command")

    if cmd == "connect":
        try:
            await broadcast({"type": "status_log", "message": "Bluetooth cihaz taranıyor..."})
            d = await get_or_create_device()
            await broadcast({
                "type": "status", "connected": True, "address": d.address,
                "recording": recording, "path": str(csv_path) if csv_path else "",
            })
            if latest_measurement:
                await broadcast(latest_measurement.to_dict() | {"recording": recording})
        except RuntimeError as e:
            try:
                await ws.send_text(json.dumps({
                    "type": "status", "connected": False,
                    "recording": recording, "path": str(csv_path) if csv_path else "",
                    "error": str(e),
                }))
            except (WebSocketDisconnect, RuntimeError):
                pass

    elif cmd == "disconnect":
        global device
        async with device_lock:
            if device:
                await device.disconnect()
                device = None
        await broadcast({"type": "status_log", "message": "Cihaz bağlantısı kesildi."})
        await broadcast({
            "type": "status", "connected": False,
            "recording": recording, "path": str(csv_path) if csv_path else "",
        })

    elif cmd == "record_toggle":
        if recording:
            await _stop_and_report()
        else:
            _auto_start_recording()

    elif cmd == "download_log":
        if csv_path and csv_path.exists():
            await ws.send_text(json.dumps({"type": "download_ready", "filename": csv_path.name}))

    elif cmd in ("reset_all", "reset_ah", "reset_wh", "reset_time"):
        cmd_map = {
            "reset_all": Command.RESET_ALL, "reset_ah": Command.RESET_AH,
            "reset_wh": Command.RESET_WH, "reset_time": Command.RESET_TIME,
        }
        if device:
            await device.send_command(cmd_map[cmd])

    elif cmd == "load_template":
        template = build_reference_template()
        await ws.send_text(json.dumps({"type": "reference_template", "template": template}))


async def _stop_and_report():
    global recording, csv_file, csv_writer
    recording = False
    if csv_file:
        csv_file.close()
        csv_file = None
        csv_writer = None

    report = {}
    if latest_measurement:
        m = latest_measurement
        elapsed = (datetime.now() - test_start_ts).total_seconds() if test_start_ts else 0
        report = {
            "start_voltage": round(test_start_v, 2),
            "end_voltage": round(m.voltage, 2),
            "voltage_drop": round(test_start_v - m.voltage, 2),
            "total_ah": round(m.capacity_ah, 3),
            "total_wh": round(m.energy_wh, 1),
            "duration_s": int(elapsed),
            "duration_str": f"{int(elapsed//3600):02d}:{int((elapsed%3600)//60):02d}:{int(elapsed%60):02d}",
            "avg_temperature": m.temperature,
            "file": str(csv_path) if csv_path else "",
        }

    await broadcast({
        "type": "record_status", "recording": False,
        "report": report, "path": str(csv_path) if csv_path else "",
    })


async def broadcast(payload: dict):
    data = json.dumps(payload)
    dead = set()
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


async def _ws_send(ws: WebSocket, data: str):
    try:
        await ws.send_text(data)
    except Exception:
        pass


@app.get("/download/{filename}")
async def download_file(filename: str):
    path = LOG_DIR / filename
    if path.exists():
        return FileResponse(path, media_type="text/csv", filename=filename)
    return {"error": "not found"}


@app.get("/api/reference-template")
async def get_reference_template():
    template = build_reference_template()
    return JSONResponse(template)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DL24 Electronic Load</title>
<script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
<style>
  :root {
    --bg: #0f0f1a; --card: #1a1a30; --border: #2a2a45; --text: #e0e0f0;
    --dim: #8888aa; --accent: #00d4aa; --warn: #ffaa00; --danger: #ff4466; --blue: #4488ff;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); min-height: 100vh;
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 24px; background: var(--card); border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 1.2rem; font-weight: 600; }
  .status { font-size: 0.82rem; display: flex; align-items: center; gap: 12px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .status-dot.on { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
  .status-dot.off { background: var(--danger); }
  .rec-dot { animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  .metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px; padding: 16px 24px;
  }
  .metric-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }
  .metric-card .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--dim); }
  .metric-card .value { font-size: 1.8rem; font-weight: 700; margin-top: 2px; }
  .metric-card .unit { font-size: 0.8rem; color: var(--dim); }

  .graph-section {
    margin: 0 24px 16px; background: var(--card);
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  }
  #graph { width: 100%; height: 420px; }

  .report-panel {
    margin: 0 24px 16px; padding: 16px 20px; background: var(--card);
    border: 1px solid var(--accent); border-radius: 8px; display: none;
  }
  .report-panel h3 { color: var(--accent); margin-bottom: 10px; font-size: 0.95rem; }
  .report-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px;
  }
  .report-item { font-size: 0.82rem; }
  .report-item span { color: var(--dim); }
  .report-item strong { color: var(--accent); font-size: 1.1rem; }

  .controls {
    display: flex; gap: 8px; padding: 0 24px 16px; flex-wrap: wrap; align-items: center;
  }
  .btn {
    background: var(--card); border: 1px solid var(--border);
    color: var(--text); padding: 7px 16px; border-radius: 6px;
    cursor: pointer; font-size: 0.82rem; transition: all 0.15s;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.primary { background: var(--accent); color: #000; border-color: var(--accent); font-weight: 600; }
  .btn.primary:hover { opacity: 0.85; }
  .btn.primary.recording { background: var(--danger); border-color: var(--danger); }
  .btn.danger:hover { border-color: var(--danger); color: var(--danger); }
  .btn:disabled { opacity: 0.4; cursor: default; }

  .filename { font-size: 0.75rem; color: var(--dim); }

  .template-controls {
    display: flex; gap: 10px; padding: 8px 24px; align-items: center; flex-wrap: wrap;
    background: var(--card); border-top: 1px solid var(--border);
  }
  .template-controls .label { font-size: 0.72rem; color: var(--dim); white-space: nowrap; }
  .shift-slider {
    -webkit-appearance: none; appearance: none; height: 4px; border-radius: 2px;
    background: var(--border); outline: none; width: 160px; cursor: pointer;
  }
  .shift-slider::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none; width: 14px; height: 14px;
    border-radius: 50%; background: var(--accent); cursor: pointer;
  }
  .shift-value { font-size: 0.75rem; color: var(--accent); min-width: 55px; text-align: center; font-family: monospace; }
  .template-toggle { font-size: 0.75rem; color: var(--dim); cursor: pointer; user-select: none; }
  .template-toggle.active { color: var(--accent); }
  .template-toggle input { display: none; }

  #log-bar {
    padding: 6px 24px; background: var(--card);
    border-bottom: 1px solid var(--border);
    font-size: 0.75rem; font-family: 'SF Mono', 'Consolas', monospace;
    color: var(--dim); min-height: 28px;
    display: flex; align-items: center; gap: 8px;
    white-space: nowrap; overflow: hidden;
  }
  #log-bar .prefix { color: var(--accent); opacity: 0.6; flex-shrink: 0; }
  #log-text {
    overflow: hidden; text-overflow: ellipsis;
    transition: color 0.3s;
  }
  #log-text.log-error { color: var(--danger); }
  #log-text.log-ok { color: var(--accent); }

  @media (max-width: 700px) {
    .metrics { grid-template-columns: 1fr 1fr; }
    .metric-card .value { font-size: 1.4rem; }
  }
</style>
</head>
<body>

<header>
  <div>
    <h1>DL24 Deşarj Testi</h1>
  </div>
  <div class="status">
    <span class="filename" id="csv-name"></span>
    <span class="rec-dot status-dot" id="rec-dot" style="display:none"></span>
    <span class="status-dot off" id="status-dot"></span>
    <span id="status-text" style="color:var(--dim)">Bağlı değil</span>
    <button class="btn primary" id="btn-connect" onclick="toggleConnect()" style="display:none">🔗 Cihaza Bağlan</button>
  </div>
</header>

<div id="log-bar">
  <span class="prefix">$</span>
  <span id="log-text">Web sayfası yüklendi, Bluetooth bağlantısı bekleniyor...</span>
</div>

<div class="metrics">
  <div class="metric-card">
    <div class="label">Voltaj</div>
    <div class="value" style="color:var(--blue)" id="val-voltage">--</div>
    <div class="unit">V</div>
  </div>
  <div class="metric-card">
    <div class="label">Akım</div>
    <div class="value" style="color:var(--accent)" id="val-current">--</div>
    <div class="unit">A</div>
  </div>
  <div class="metric-card">
    <div class="label">Güç</div>
    <div class="value" id="val-power">--</div>
    <div class="unit">W</div>
  </div>
  <div class="metric-card">
    <div class="label">Sıcaklık</div>
    <div class="value" id="val-temp">--</div>
    <div class="unit">°C</div>
  </div>
  <div class="metric-card">
    <div class="label">Kapasite</div>
    <div class="value" style="color:var(--warn)" id="val-ah">--</div>
    <div class="unit">Ah</div>
  </div>
  <div class="metric-card">
    <div class="label">Enerji</div>
    <div class="value" id="val-wh">--</div>
    <div class="unit">Wh</div>
  </div>
  <div class="metric-card">
    <div class="label">Süre</div>
    <div class="value" style="font-size:1.4rem" id="val-runtime">--</div>
    <div class="unit"></div>
  </div>
  <div class="metric-card">
    <div class="label">Direnç</div>
    <div class="value" style="font-size:1.4rem" id="val-res">--</div>
    <div class="unit">Ω</div>
  </div>
</div>

<div class="report-panel" id="report-panel">
  <h3>📊 Test Raporu</h3>
  <div class="report-grid" id="report-grid"></div>
</div>

<div class="graph-section"><div id="graph"></div></div>

<div class="template-controls" id="template-controls" style="display:none">
  <label class="template-toggle active" id="tpl-toggle-label">
    <input type="checkbox" id="tpl-toggle" checked onchange="toggleTemplate()">
    👻 Sağlıklı Referans
  </label>
  <span class="label">Zaman Kaydırma:</span>
  <input type="range" class="shift-slider" id="shift-slider" min="-600" max="600" value="0" step="10"
         oninput="onShiftChange(this.value)">
  <span class="shift-value" id="shift-val">0 sn</span>
  <button class="btn" onclick="resetShift()" style="padding:3px 8px;font-size:0.7rem;">↺ Sıfırla</button>
</div>

<div class="controls">
  <button class="btn primary" id="btn-record" onclick="toggleRecord()">⏺ Kaydı Başlat</button>
  <button class="btn" id="btn-download" onclick="downloadLog()" disabled>⬇ CSV İndir</button>
  <span style="flex:1"></span>
  <button class="btn danger" onclick="sendCmd('reset_all')">Reset All</button>
  <button class="btn" onclick="sendCmd('reset_ah')">Reset Ah</button>
  <button class="btn" onclick="sendCmd('reset_wh')">Reset Wh</button>
</div>

<script>
let timestamps = [], volts = [], ahs = [];
let connected = false;
let recording = false;
let ws = null, reconnectDelay = 1000;
let csvFilename = '';

// ── Reference template state ──
let templateData = null;
let templateVisible = true;
let templateShiftSec = 0;
let testStartWallclock = null;
let templateDirty = false;

function parseRuntime(r) {
  if (!r) return 0;
  const p = r.split(':');
  return parseInt(p[0])*3600 + parseInt(p[1])*60 + parseInt(p[2]);
}

function fmtShift(sec) {
  const m = Math.floor(Math.abs(sec)/60), s = Math.abs(sec)%60;
  const sign = sec < 0 ? '-' : sec > 0 ? '+' : '';
  return sign + m + ':' + String(s).padStart(2,'0') + ' dk';
}

function toggleConnect() {
  if (connected) { sendCmd('disconnect'); } else { sendCmd('connect'); }
}

function toggleRecord() { sendCmd('record_toggle'); }

function downloadLog() {
  if (csvFilename) window.open('/download/' + csvFilename, '_blank');
}

function sendCmd(cmd) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({command: cmd}));
}

function updateUI(m) {
  document.getElementById('val-voltage').textContent = m.voltage?.toFixed(3) ?? '--';
  document.getElementById('val-current').textContent = m.current?.toFixed(4) ?? '--';
  document.getElementById('val-power').textContent = m.power?.toFixed(2) ?? '--';
  document.getElementById('val-temp').textContent = m.temperature ?? '--';
  document.getElementById('val-ah').textContent = m.capacity_ah?.toFixed(4) ?? '--';
  document.getElementById('val-wh').textContent = m.energy_wh?.toFixed(2) ?? '--';
  document.getElementById('val-res').textContent = m.resistance?.toFixed(2) ?? '--';
  document.getElementById('val-runtime').textContent = m.runtime ?? '--';
}

function updateRecording(rec, path) {
  const wasRecording = recording;
  recording = rec;
  const btn = document.getElementById('btn-record');
  const dot = document.getElementById('rec-dot');
  const dl = document.getElementById('btn-download');
  if (rec) {
    btn.textContent = '⏹ Kaydı Durdur';
    btn.classList.add('recording');
    dot.style.display = 'inline-block';
    dot.style.background = 'var(--danger)';
    dl.disabled = true;
    if (path) {
      csvFilename = path.split('/').pop();
      document.getElementById('csv-name').textContent = '📁 ' + csvFilename;
    }
    if (!wasRecording) resetGraph();
  } else {
    btn.textContent = '⏺ Kaydı Başlat';
    btn.classList.remove('recording');
    dot.style.display = 'none';
    dl.disabled = false;
    document.getElementById('csv-name').textContent = '';
  }
}

function showReport(report) {
  const panel = document.getElementById('report-panel');
  const grid = document.getElementById('report-grid');
  grid.innerHTML = [
    ['Başlangıç Voltajı', report.start_voltage + ' V'],
    ['Bitiş Voltajı', report.end_voltage + ' V'],
    ['Voltaj Düşüşü', (report.start_voltage - report.end_voltage).toFixed(2) + ' V'],
    ['Toplam Kapasite', '<strong>' + report.total_ah + ' Ah</strong>'],
    ['Toplam Enerji', report.total_wh + ' Wh'],
    ['Test Süresi', report.duration_str],
    ['Ort. Sıcaklık', report.avg_temperature + ' °C'],
  ].map(([label, val]) =>
    '<div class="report-item"><span>' + label + '</span><br>' + val + '</div>'
  ).join('');
  panel.style.display = 'block';
}

function setConnected(ok, addr, err) {
  connected = ok;
  document.getElementById('status-dot').className = 'status-dot ' + (ok ? 'on' : 'off');
  document.getElementById('status-text').textContent = ok ? ('Cihaza bağlı — ' + (addr||'')) : (err || 'Bağlı değil');
  const btn = document.getElementById('btn-connect');
  if (ok) { btn.textContent = '⚡ Bağlantıyı Kes'; btn.className = 'btn danger'; }
  else { btn.textContent = '🔗 Cihaza Bağlan'; btn.className = 'btn primary'; }
  btn.style.display = 'inline-block';
}

function addLog(msg) {
  const el = document.getElementById('log-text');
  el.textContent = msg; el.className = '';
  if (msg.includes('bağlandı') || msg.includes('kesildi')) el.className = 'log-ok';
  else if (msg.includes('Hata') || msg.includes('bulunamadı')) el.className = 'log-error';
}

function initGraph() {
  Plotly.newPlot('graph', [
    { y:[], x:[], name:'Voltaj (V)', type:'scatter', mode:'lines',
      line:{color:'#4488ff', width:1.5}, yaxis:'y' },
    { y:[], x:[], name:'Kapasite (Ah)', type:'scatter', mode:'lines',
      line:{color:'#ffaa00', width:1.5, dash:'dot'}, yaxis:'y2' },
    { y:[], x:[], name:'Sağlıklı Ref V', type:'scatter', mode:'lines',
      line:{color:'rgba(68,136,255,0.35)', width:0.5}, yaxis:'y',
      showlegend:false, hoverinfo:'skip' },
    { y:[], x:[], name:'Ref Voltaj (V)', type:'scatter', mode:'lines',
      line:{color:'rgba(68,136,255,0.35)', width:0.5}, yaxis:'y',
      fill:'tonexty', fillcolor:'rgba(68,136,255,0.10)' },
    { y:[], x:[], name:'Sağlıklı Ref C', type:'scatter', mode:'lines',
      line:{color:'rgba(255,170,0,0.35)', width:0.5}, yaxis:'y2',
      showlegend:false, hoverinfo:'skip' },
    { y:[], x:[], name:'Ref Kapasite (Ah)', type:'scatter', mode:'lines',
      line:{color:'rgba(255,170,0,0.35)', width:0.5}, yaxis:'y2',
      fill:'tonexty', fillcolor:'rgba(255,170,0,0.10)' },
  ], {
    paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)',
    font:{color:'#8888aa', size:11},
    margin:{t:10, r:60, b:40, l:50},
    xaxis:{title:'Zaman', gridcolor:'#1a1a30', zeroline:false},
    yaxis:{title:'Voltaj (V)', gridcolor:'#1a1a30', side:'left', range:[9,14]},
    yaxis2:{title:'Kapasite (Ah)', overlaying:'y', side:'right', gridcolor:'rgba(0,0,0,0)'},
    legend:{orientation:'h', y:1.12},
  }, {responsive:true, displayModeBar:false});
}

function applyTemplate() {
  if (!templateData || !templateData.time_sec.length) return;
  document.getElementById('template-controls').style.display = 'flex';

  const shift = templateShiftSec;
  const t0 = testStartWallclock ? testStartWallclock.getTime() : Date.now();
  const timeSec = templateData.time_sec;
  const n = timeSec.length;

  const xVals = new Array(n);
  for (let i = 0; i < n; i++) {
    xVals[i] = new Date(t0 + (timeSec[i] + shift) * 1000);
  }

  const vis = templateVisible;

  Plotly.update('graph',
    { x: [null, null, xVals, xVals, xVals, xVals],
      y: [null, null, templateData.voltage_min, templateData.voltage_max,
                  templateData.capacity_min, templateData.capacity_max] },
    {}, [0,1,2,3,4,5]);

  for (let i = 2; i <= 5; i++) {
    Plotly.restyle('graph', {visible: vis ? true : 'legendonly'}, [i]);
  }
}

function onTemplateReceived(data) {
  templateData = data;
  templateDirty = true;
  if (templateData.source_files) {
    document.getElementById('tpl-toggle-label').childNodes[1].textContent =
      ' 👻 Sağlıklı Referans (±%5)';
  }
  applyTemplate();
}

function toggleTemplate() {
  templateVisible = document.getElementById('tpl-toggle').checked;
  const label = document.getElementById('tpl-toggle-label');
  label.className = 'template-toggle' + (templateVisible ? ' active' : '');
  templateDirty = true;
  applyTemplate();
}

function onShiftChange(val) {
  templateShiftSec = parseInt(val);
  document.getElementById('shift-val').textContent = fmtShift(templateShiftSec);
  templateDirty = true;
  applyTemplate();
}

function resetShift() {
  templateShiftSec = 0;
  document.getElementById('shift-slider').value = 0;
  document.getElementById('shift-val').textContent = '0 sn';
  templateDirty = true;
  applyTemplate();
}

function resetGraph() {
  timestamps = []; volts = []; ahs = [];
  testStartWallclock = null;
  templateDirty = true;
  Plotly.update('graph', {x:[[],[]], y:[[],[]]}, {}, [0,1]);
}

function updateGraph(m) {
  const now = new Date();

  if (testStartWallclock === null && m.runtime) {
    const rt = parseRuntime(m.runtime);
    testStartWallclock = new Date(now.getTime() - rt * 1000);
    templateDirty = true;
  }

  timestamps.push(now); volts.push(m.voltage||0); ahs.push(m.capacity_ah||0);

  Plotly.update('graph', {x:[timestamps,timestamps], y:[volts,ahs]}, {}, [0,1]);

  if (templateData && templateDirty) {
    applyTemplate();
    templateDirty = false;
  }
}

function connect() {
  ws = new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws');
  ws.onopen = () => { reconnectDelay = 1000; };
  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'reference_template') {
        onTemplateReceived(d.template);
      } else if (d.type === 'status_log') {
        addLog(d.message);
      } else if (d.type === 'status') {
        setConnected(d.connected, d.address||'', d.error||'');
        if (d.recording !== undefined) updateRecording(d.recording, d.path);
      } else if (d.type === 'record_status') {
        updateRecording(d.recording, d.path);
        if (d.report) showReport(d.report);
      } else if (d.type === 'error') {
        setConnected(false, '', d.message);
      } else if (d.type !== 'ping') {
        updateUI(d);
        updateGraph(d);
        if (d.recording !== undefined) updateRecording(d.recording);
      }
    } catch(_){}
  };
  ws.onclose = () => { setConnected(false); setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(reconnectDelay*1.5, 15000); };
  ws.onerror = () => ws.close();
}

initGraph();
connect();
</script>
</body>
</html>
"""
