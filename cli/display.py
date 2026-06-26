"""Rich-based live TUI for DL24 real-time monitoring."""

import asyncio
import logging
import time
from datetime import datetime

from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.console import Console

from dl24_ble import DL24Device, DL24Config, Measurement

logger = logging.getLogger(__name__)


def build_layout(measurement=None, connected=False, elapsed=0, sample_count=0):
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="main", ratio=2),
        Layout(name="secondary", ratio=1),
    )

    status = "[green]● CONNECTED[/green]" if connected else "[red]● DISCONNECTED[/red]"
    layout["header"].update(
        Panel(
            f"[bold white]DL24 Electronic Load[/bold white]  {status}  "
            f"Samples: {sample_count}  Elapsed: {elapsed:.0f}s",
            style="bold blue",
        )
    )

    if measurement:
        m = measurement
        main_table = Table(show_header=False, padding=(0, 2), expand=True)
        main_table.add_column(style="dim")
        main_table.add_column(style="bold cyan", justify="right")
        main_table.add_column(style="dim")
        main_table.add_column(style="bold green", justify="right")
        main_table.add_row("Voltage", f"{m.voltage:.2f} V", "   Current", f"{m.current:.3f} A")
        main_table.add_row("Power", f"{m.power:.1f} W", "   Resistance", f"{m.resistance:.1f} Ω")
        main_table.add_row("", "", "", "")

        temp_color = "red" if m.temperature > 60 else "yellow" if m.temperature > 40 else "green"
        main_table.add_row(
            "Temperature", f"[{temp_color}]{m.temperature}°C[/{temp_color}]",
            "", "",
        )
        layout["main"].update(Panel(main_table, title="Measurements", border_style="cyan"))

        sec_table = Table(show_header=False, padding=(0, 2), expand=True)
        sec_table.add_column(style="dim")
        sec_table.add_column(style="bold white", justify="right")
        sec_table.add_row("Energy", f"{m.energy_wh:.1f} Wh")
        sec_table.add_row("Capacity", f"{m.capacity_ah:.2f} Ah")
        sec_table.add_row("Runtime", m.runtime_str)
        layout["secondary"].update(Panel(sec_table, title="Accumulated", border_style="cyan"))
    else:
        layout["main"].update(Panel("Waiting for data...", title="Measurements"))
        layout["secondary"].update(Panel("—", title="Accumulated"))

    layout["footer"].update(
        Panel("Press [bold]Ctrl+C[/bold] to exit", style="dim")
    )
    return layout


async def display_live(device_address=None, refresh=1.0):
    device = DL24Device()
    if device_address:
        device.config.device_address = device_address

    latest = None
    connected = False
    start_time = time.time()
    sample_count = 0

    def on_meas(m: Measurement):
        nonlocal latest, sample_count
        latest = m
        sample_count += 1

    device.on_measurement(on_meas)

    console = Console()
    with Live(build_layout(), console=console, refresh_per_second=1/refresh, screen=True) as live:

        async def update_loop():
            nonlocal connected
            while True:
                elapsed = time.time() - start_time
                live.update(build_layout(latest, connected, elapsed, sample_count))
                await asyncio.sleep(refresh)

        update_task = asyncio.create_task(update_loop())

        try:
            connected = await device.connect(device_address)
            await device.run_forever()
        except asyncio.CancelledError:
            pass
        finally:
            connected = False
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass
            await device.disconnect()
