"""Battery capacity test — CC discharge with automatic cutoff."""

import asyncio
import csv
import logging
import time
from datetime import datetime
from pathlib import Path

from dl24_ble import DL24Device, DL24Config, Measurement, TestResult

logger = logging.getLogger(__name__)


async def run_battery_test(
    device_address=None,
    discharge_current=1.0,
    cutoff_voltage=3.0,
    max_time=3600,
    output_prefix="battery_test",
    sample_interval=1.0,
):
    device = DL24Device()
    if device_address:
        device.config.device_address = device_address

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"./{output_prefix}_{timestamp}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    out_file = open(out_path, "w", newline="")
    writer = csv.writer(out_file)
    writer.writerow([
        "elapsed_s", "timestamp", "voltage_V", "current_A", "power_W",
        "capacity_Ah", "energy_Wh", "temperature_C",
    ])

    latest = None
    start_time = 0.0
    test_complete = asyncio.Event()
    temperatures = []
    sample_count = 0
    reason = ""

    def on_meas(m: Measurement):
        nonlocal latest, sample_count
        latest = m
        sample_count += 1
        temperatures.append(m.temperature)

    device.on_measurement(on_meas)

    logger.info("=" * 50)
    logger.info("BATTERY CAPACITY TEST")
    logger.info(f"  Discharge Current: {discharge_current:.1f}A")
    logger.info(f"  Cutoff Voltage:    {cutoff_voltage:.2f}V")
    logger.info(f"  Max Duration:      {max_time}s")
    logger.info(f"  Output:            {out_path}")
    logger.info("=" * 50)

    if not await device.connect():
        logger.error("Connection failed")
        return

    await device.reset_all()
    await asyncio.sleep(1.0)
    start_time = time.time()
    start_voltage = None

    async def monitor_loop():
        nonlocal reason
        while not test_complete.is_set():
            if latest is not None:
                elapsed = time.time() - start_time
                m = latest
                if start_voltage is None and m.voltage > 0:
                    nonlocal start_voltage
                    start_voltage = m.voltage

                writer.writerow([
                    f"{elapsed:.1f}",
                    datetime.now().isoformat(),
                    f"{m.voltage:.3f}",
                    f"{m.current:.3f}",
                    f"{m.power:.2f}",
                    f"{m.capacity_ah:.3f}",
                    f"{m.energy_wh:.2f}",
                    str(m.temperature),
                ])
                out_file.flush()

                if elapsed >= max_time:
                    reason = "timeout"
                    test_complete.set()
                elif m.voltage > 0.1 and m.voltage <= cutoff_voltage:
                    reason = "cutoff_reached"
                    test_complete.set()

            await asyncio.sleep(sample_interval)

    try:
        await monitor_loop()
    except asyncio.CancelledError:
        reason = "user_interrupt"
    finally:
        test_complete.set()

    end_voltage = latest.voltage if latest else 0
    elapsed = time.time() - start_time

    if temperatures:
        avg_temp = sum(temperatures) / len(temperatures)
        max_temp = max(temperatures)
    else:
        avg_temp = max_temp = 0

    result = TestResult(
        start_voltage=start_voltage or 0,
        end_voltage=end_voltage,
        total_capacity_ah=latest.capacity_ah if latest else 0,
        total_energy_wh=latest.energy_wh if latest else 0,
        duration_seconds=int(elapsed),
        avg_temperature=avg_temp,
        max_temperature=max_temp,
        measurement_count=sample_count,
        completed=(reason == "cutoff_reached"),
        reason=reason,
    )

    logger.info("")
    logger.info("=" * 50)
    logger.info("TEST COMPLETE")
    logger.info(f"  Reason:           {reason}")
    logger.info(f"  Start Voltage:    {result.start_voltage:.2f}V")
    logger.info(f"  End Voltage:      {result.end_voltage:.2f}V")
    logger.info(f"  Total Capacity:   {result.total_capacity_ah:.3f}Ah")
    logger.info(f"  Total Energy:     {result.total_energy_wh:.1f}Wh")
    logger.info(f"  Duration:         {result.duration_seconds}s")
    logger.info(f"  Avg Temperature:  {result.avg_temperature:.1f}°C")
    logger.info(f"  Max Temperature:  {result.max_temperature:.0f}°C")
    logger.info(f"  Samples:          {result.measurement_count}")
    logger.info(f"  Data saved to:    {out_path}")
    logger.info("=" * 50)

    out_file.close()
    await device.reset_all()
    await device.disconnect()
