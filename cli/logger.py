"""Data logger for DL24 — CSV and JSON output."""

import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from dl24_ble import DL24Device, DL24Config, Measurement

logger = logging.getLogger(__name__)


async def log_data(
    device_address=None,
    format="csv",
    output=None,
    duration=None,
):
    device = DL24Device()
    if device_address:
        device.config.device_address = device_address

    measurements = []
    stop_event = asyncio.Event()

    def on_meas(m: Measurement):
        measurements.append(m)

    device.on_measurement(on_meas)

    out_path = Path(output) if output else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    out_file = open(out_path, "a", newline="") if out_path else sys.stdout

    try:
        if format == "csv":
            writer = csv.writer(out_file)
            writer.writerow([
                "timestamp", "voltage_V", "current_A", "power_W",
                "resistance_ohm", "capacity_Ah", "energy_Wh",
                "temperature_C", "runtime",
            ])
            out_file.flush()

            write_func = lambda m: writer.writerow([
                datetime.now().isoformat(),
                f"{m.voltage:.3f}",
                f"{m.current:.3f}",
                f"{m.power:.2f}",
                f"{m.resistance:.2f}",
                f"{m.capacity_ah:.3f}",
                f"{m.energy_wh:.2f}",
                str(m.temperature),
                m.runtime_str,
            ])
        else:
            write_func = lambda m: out_file.write(
                json.dumps(m.to_dict() | {"timestamp": datetime.now().isoformat()}) + "\n"
            )

        logger.info(f"Connected. Logging as {format}")
        if not await device.connect():
            logger.error("Connection failed")
            return

        async def monitor():
            last_idx = 0
            while not stop_event.is_set():
                while last_idx < len(measurements):
                    write_func(measurements[last_idx])
                    out_file.flush()
                    last_idx += 1
                await asyncio.sleep(0.1)

        monitor_task = asyncio.create_task(monitor())

        if duration:
            await asyncio.sleep(duration)
            stop_event.set()
        else:
            try:
                await device.run_forever()
            except asyncio.CancelledError:
                pass
            finally:
                stop_event.set()

        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    finally:
        await device.disconnect()
        if out_path:
            out_file.close()
        logger.info(f"Logging stopped. {len(measurements)} samples recorded.")
