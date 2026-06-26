#!/usr/bin/env python3

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dl24_ble import DL24Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dl24",
        description="Atorch DL24 Electronic Load — BLE control and monitoring",
    )
    p.add_argument("--device", "-d", help="Device MAC address")
    p.add_argument("--config", "-c", default="config.yaml", help="Config file path")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    subs = p.add_subparsers(dest="command", required=True)

    monitor = subs.add_parser("monitor", help="Live Rich TUI monitoring")
    monitor.add_argument("--refresh", "-r", type=float, default=1.0, help="Refresh interval (s)")

    log = subs.add_parser("log", help="Log measurements to file")
    log.add_argument("--format", "-f", choices=["csv", "json"], default="csv")
    log.add_argument("--output", "-o", default=None, help="Output file path")
    log.add_argument("--duration", "-t", type=float, default=None, help="Duration in seconds")

    bat = subs.add_parser("bat-test", help="Battery capacity test")
    bat.add_argument("--current", "-i", type=float, default=1.0, help="Discharge current (A)")
    bat.add_argument("--cutoff", "-u", type=float, default=3.0, help="Cutoff voltage (V)")
    bat.add_argument("--max-time", "-T", type=int, default=3600, help="Max test duration (s)")
    bat.add_argument("--output-prefix", "-p", default="battery_test")
    bat.add_argument("--sample-interval", "-s", type=float, default=1.0)

    subs.add_parser("discover", help="Scan for DL24 BLE devices")

    reset = subs.add_parser("reset", help="Reset counters on device")
    reset.add_argument("--ah", action="store_true", help="Reset Ah only")
    reset.add_argument("--wh", action="store_true", help="Reset Wh only")
    reset.add_argument("--time", action="store_true", help="Reset time only")

    subs.add_parser("raw", help="Output raw hex packets (debug)")

    return p


def load_config(config_path: str) -> DL24Config:
    path = Path(config_path)
    if path.exists():
        return DL24Config.from_yaml(path)
    return DL24Config()


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config(args.config)
    if args.device:
        config.device_address = args.device

    try:
        if args.command == "monitor":
            from cli.display import display_live
            asyncio.run(display_live(
                device_address=config.device_address or None,
                refresh=args.refresh,
            ))
        elif args.command == "log":
            from cli.logger import log_data
            asyncio.run(log_data(
                device_address=config.device_address or None,
                format=args.format,
                output=args.output,
                duration=args.duration,
            ))
        elif args.command == "bat-test":
            from cli.bat_test import run_battery_test
            asyncio.run(run_battery_test(
                device_address=config.device_address or None,
                discharge_current=args.current,
                cutoff_voltage=args.cutoff,
                max_time=args.max_time,
                output_prefix=args.output_prefix,
                sample_interval=args.sample_interval,
            ))
        elif args.command == "discover":
            asyncio.run(cmd_discover(config))
        elif args.command == "reset":
            asyncio.run(cmd_reset(config, args))
        elif args.command == "raw":
            asyncio.run(cmd_raw(config))
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


async def cmd_discover(config: DL24Config):
    from dl24_ble import DL24Device
    device = DL24Device(config)
    addr = await device.discover()
    if addr:
        print(f"Found DL24 device at: {addr}")
    else:
        print("No DL24 device found. Check Bluetooth is enabled and device is powered on.")
    await device.disconnect()


async def cmd_reset(config: DL24Config, args):
    from dl24_ble import DL24Device, Command
    device = DL24Device(config)
    if not await device.connect():
        print("Could not connect to device")
        return
    if args.ah:
        await device.send_command(Command.RESET_AH)
    elif args.wh:
        await device.send_command(Command.RESET_WH)
    elif args.time:
        await device.send_command(Command.RESET_TIME)
    else:
        await device.reset_all()
    print("Reset command sent")
    await asyncio.sleep(0.5)
    await device.disconnect()


async def cmd_raw(config: DL24Config):
    from dl24_ble import DL24Device, packet_info

    def on_meas(m):
        print(packet_info(m.raw))

    device = DL24Device(config)
    device.on_measurement(on_meas)
    if not await device.connect():
        print("Could not connect to device")
        return
    try:
        await device.run_forever()
    except KeyboardInterrupt:
        await device.disconnect()


if __name__ == "__main__":
    main()
