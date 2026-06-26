"""FastAPI server with WebSocket, recording, and report download."""

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
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from dl24_ble import DL24Device, DL24Config, Measurement, Command

logger = logging.getLogger(__name__)

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)

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
            device = DL24Device()
            device.on_measurement(on_device_measurement)
            ok = await device.connect()
            if not ok:
                raise RuntimeError("Could not connect to DL24 device")
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
    dead = set()
    for ws in clients:
        try:
            asyncio.create_task(ws.send_text(data))
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


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

    try:
        d = await get_or_create_device()
        await asyncio.sleep(0.5)
        await ws.send_text(json.dumps({
            "type": "status", "connected": True, "address": d.address,
            "recording": recording, "path": str(csv_path) if csv_path else "",
        }))
        if latest_measurement:
            await ws.send_text(json.dumps(latest_measurement.to_dict() | {"recording": recording}))

        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                await handle_ws_message(ws, msg)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
        except WebSocketDisconnect:
            pass
    finally:
        clients.discard(ws)


async def handle_ws_message(ws: WebSocket, msg: str):
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return

    cmd = data.get("command")

    if cmd == "record_toggle":
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


@app.get("/download/{filename}")
async def download_file(filename: str):
    path = LOG_DIR / filename
    if path.exists():
        return FileResponse(path, media_type="text/csv", filename=filename)
    return {"error": "not found"}


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
    <span id="status-text" style="color:var(--dim)">Disconnected</span>
  </div>
</header>

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

<div class="controls">
  <button class="btn primary" id="btn-record" onclick="toggleRecord()">⏺ Kaydı Başlat</button>
  <button class="btn" id="btn-download" onclick="downloadLog()" disabled>⬇ CSV İndir</button>
  <span style="flex:1"></span>
  <button class="btn danger" onclick="sendCmd('reset_all')">Reset All</button>
  <button class="btn" onclick="sendCmd('reset_ah')">Reset Ah</button>
  <button class="btn" onclick="sendCmd('reset_wh')">Reset Wh</button>
</div>

<script>
const MAX_POINTS = 600;
let timestamps = [], volts = [], amps = [], ahs = [];
let recording = false;
let ws = null, reconnectDelay = 1000;
let csvFilename = '';

function toggleRecord() {
  sendCmd('record_toggle');
}

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

function setConnected(ok, addr) {
  document.getElementById('status-dot').className = 'status-dot ' + (ok ? 'on' : 'off');
  document.getElementById('status-text').textContent = ok ? ('Connected — ' + (addr||'')) : 'Disconnected';
}

function initGraph() {
  Plotly.newPlot('graph', [
    { y: [], x: [], name: 'Voltaj (V)', type: 'scatter', mode: 'lines',
      line: { color: '#4488ff', width: 1.5 }, yaxis: 'y' },
    { y: [], x: [], name: 'Kapasite (Ah)', type: 'scatter', mode: 'lines',
      line: { color: '#ffaa00', width: 1.5, dash: 'dot' }, yaxis: 'y2' },
  ], {
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#8888aa', size: 11 },
    margin: { t: 10, r: 60, b: 40, l: 50 },
    xaxis: { title: 'Zaman', gridcolor: '#1a1a30', zeroline: false },
    yaxis: { title: 'Voltaj (V)', gridcolor: '#1a1a30', side: 'left' },
    yaxis2: { title: 'Kapasite (Ah)', overlaying: 'y', side: 'right', gridcolor: 'rgba(0,0,0,0)' },
    legend: { orientation: 'h', y: 1.12 },
  }, { responsive: true, displayModeBar: false });
}

function updateGraph(m) {
  const now = new Date();
  timestamps.push(now); volts.push(m.voltage||0); ahs.push(m.capacity_ah||0);
  if (timestamps.length > MAX_POINTS) {
    timestamps = timestamps.slice(-MAX_POINTS);
    volts = volts.slice(-MAX_POINTS);
    ahs = ahs.slice(-MAX_POINTS);
  }
  Plotly.update('graph', { x: [timestamps, timestamps], y: [volts, ahs] }, {}, [0, 1]);
}

function connect() {
  ws = new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws');
  ws.onopen = () => { reconnectDelay = 1000; };
  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === 'status') {
        setConnected(d.connected, d.address||'');
        if (d.recording !== undefined) updateRecording(d.recording, d.path);
      } else if (d.type === 'record_status') {
        updateRecording(d.recording, d.path);
        if (d.report) showReport(d.report);
      } else if (d.type === 'error') {
        // silently handled
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
