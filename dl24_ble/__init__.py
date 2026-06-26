"""DL24 BLE — Atorch DL24/P/M Electronic Load control library for Linux."""

from .protocol import (
    Measurement,
    MessageType,
    Command,
    build_command,
    build_set_current,
    build_reset_all,
    build_reset_ah,
    build_reset_wh,
    build_reset_time,
    parse_dc_report,
    parse_notification,
    NotificationReassembler,
    hexdump,
    packet_info,
    compute_checksum,
    verify_checksum,
    MAGIC_HEADER,
    CHECKSUM_XOR,
    DEVICE_TYPE_DC,
)
from .device import DL24Device
from .models import DL24Config, BatteryTestConfig, TestResult, ConnectionState

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

__all__ = [
    "Measurement",
    "MessageType",
    "Command",
    "DL24Device",
    "DL24Config",
    "BatteryTestConfig",
    "TestResult",
    "ConnectionState",
    "NotificationReassembler",
    "build_command",
    "build_set_current",
    "build_reset_all",
    "build_reset_ah",
    "build_reset_wh",
    "build_reset_time",
    "parse_dc_report",
    "parse_notification",
    "hexdump",
    "packet_info",
    "compute_checksum",
    "verify_checksum",
    "MAGIC_HEADER",
    "CHECKSUM_XOR",
    "DEVICE_TYPE_DC",
    "SERVICE_UUID",
    "CHARACTERISTIC_UUID",
]
__version__ = "1.0.0"
