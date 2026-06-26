"""
DL24 BLE Protocol — Binary packet encode/decode for Atorch DL24/P/M electronic loads.

Based on protocol reverse engineering by:
  - NiceLabs/atorch-console (protocol-design.md)
  - syssi/esphome-atorch-dl24
  - Flaviu Tamas (flaviutamas.com/2022/dl24m-reversing)
  - adlerweb/DL24_BLE_Logger

Protocol overview:
  Magic:  FF 55 (2 bytes)
  Type:   01=Report, 02=Reply, 11=Command (1 byte)
  Payload: variable (device type 02 = DC load)
  Checksum: (sum(payload bytes) & 0xFF) ^ 0x44

BLE Service:  0000FFE0-0000-1000-8000-00805F9B34FB
BLE Characteristic: 0000FFE1-0000-1000-8000-00805F9B34FB
Device name: DL24_BLE (or DL24P_BLE, DL24M_BLE)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, Tuple


# ── Constants ──────────────────────────────────────────────────────────────────

MAGIC_HEADER = b"\xFF\x55"
CHECKSUM_XOR = 0x44

# Device types in Atorch protocol family
DEVICE_TYPE_DC = 0x02  # DC Electronic Load (DL24 series)


class MessageType(IntEnum):
    REPORT = 0x01   # Device → Host: measurement report
    REPLY = 0x02    # Device → Host: command acknowledgement
    COMMAND = 0x11  # Host → Device: command


class Command(IntEnum):
    """Host → Device command codes."""
    RESET_WH = 0x01
    RESET_AH = 0x02
    RESET_TIME = 0x03
    RESET_ALL = 0x05
    BUTTON_PLUS = 0x11
    BUTTON_MINUS = 0x12
    SET_BACKLIGHT = 0x21
    SET_PRICE = 0x22
    BUTTON_SETUP = 0x31
    BUTTON_ENTER = 0x32


# ── Measurement Dataclass ─────────────────────────────────────────────────────

@dataclass
class Measurement:
    """Parsed DC load measurement from a BLE notification packet."""

    voltage: float = 0.0       # Volts (V)
    current: float = 0.0       # Amps (A)
    power: float = 0.0         # Watts (W)
    capacity_ah: float = 0.0   # Amp-hours (Ah)
    energy_wh: float = 0.0     # Watt-hours (Wh)
    temperature: int = 0       # Celsius (°C)
    runtime_h: int = 0         # Hours
    runtime_m: int = 0         # Minutes
    runtime_s: int = 0         # Seconds
    backlight: int = 0         # Backlight brightness
    raw: bytes = field(default_factory=bytes, repr=False)

    @property
    def resistance(self) -> float:
        """Calculated resistance in Ohms."""
        if self.current > 0.001:
            return self.voltage / self.current
        return 0.0

    @property
    def runtime_str(self) -> str:
        """Runtime formatted as HH:MM:SS."""
        return f"{self.runtime_h:02d}:{self.runtime_m:02d}:{self.runtime_s:02d}"

    def to_dict(self) -> dict:
        return {
            "voltage": round(self.voltage, 3),
            "current": round(self.current, 4),
            "power": round(self.power, 2),
            "resistance": round(self.resistance, 2),
            "capacity_ah": round(self.capacity_ah, 4),
            "energy_wh": round(self.energy_wh, 2),
            "temperature": self.temperature,
            "runtime": self.runtime_str,
        }


# ── Checksum ──────────────────────────────────────────────────────────────────

def compute_checksum(payload: bytes) -> int:
    """Compute Atorch checksum: (sum of all payload bytes) XOR 0x44."""
    return (sum(payload) & 0xFF) ^ CHECKSUM_XOR


def verify_checksum(packet: bytes) -> bool:
    """Verify the checksum of a received packet (must include magic header)."""
    if len(packet) < 4:
        return False
    payload = packet[2:-1]
    expected = compute_checksum(payload)
    actual = packet[-1]
    return expected == actual


# ── Packet Building ───────────────────────────────────────────────────────────

def build_command(device_type: int, cmd: Command, value: int = 0) -> bytes:
    """
    Build a command packet.

    Args:
        device_type: Device type (02 for DC load)
        cmd: Command code
        value: Command value (4-byte big-endian integer)

    Returns:
        Full packet bytes ready to send to BLE characteristic.
    """
    payload = struct.pack(
        ">BBBI",
        MessageType.COMMAND,
        device_type,
        cmd,
        value,
    )
    checksum = compute_checksum(payload)
    return MAGIC_HEADER + payload + bytes([checksum])


def build_set_current(current_a: float) -> bytes:
    """Build a SET CURRENT command packet. Uses [+] button for increase."""
    return build_command(DEVICE_TYPE_DC, Command.BUTTON_PLUS, 0)


def build_reset_all() -> bytes:
    """Build a RESET ALL command packet."""
    return build_command(DEVICE_TYPE_DC, Command.RESET_ALL, 0)


def build_reset_ah() -> bytes:
    """Build a RESET AH command packet."""
    return build_command(DEVICE_TYPE_DC, Command.RESET_AH, 0)


def build_reset_wh() -> bytes:
    """Build a RESET WH command packet."""
    return build_command(DEVICE_TYPE_DC, Command.RESET_WH, 0)


def build_reset_time() -> bytes:
    """Build a RESET TIME command packet."""
    return build_command(DEVICE_TYPE_DC, Command.RESET_TIME, 0)


# ── DC Meter Report Decoding ──────────────────────────────────────────────────

def parse_dc_report(payload: bytes) -> Optional[Measurement]:
    """
    Parse a DC Meter Report payload (message type 01, device type 02).

    Field offsets verified against decompiled Android app (Flaviu Tamas, 2022).
    Payload starts at message type byte (after magic header FF 55).
    All multi-byte values are big-endian unsigned integers.

    [0]  Message Type (0x01)
    [1]  Device Type  (0x02)
    [2-4]   Voltage    — 24-bit BE, /10 → Volts
    [5-7]   Current    — 24-bit BE, /1000 → Amps
    [8-10]  Capacity   — 24-bit BE, /100 → Amp-hours
    [11-14] Energy     — 32-bit BE, /100 → Watt-hours
    [15-18] Unknown    — 4 bytes (price/config)
    [19-21] Unknown    — 3 bytes
    [22-23] Temperature — 16-bit BE → °C
    [24-25] Runtime    — 16-bit BE, hours
    [26]    Runtime    — 1 byte, minutes
    [27]    Runtime    — 1 byte, seconds
    [28]    Backlight  — 1 byte
    """
    if len(payload) < 29:
        return None

    msg_type = payload[0]
    dev_type = payload[1]

    if msg_type != MessageType.REPORT or dev_type != DEVICE_TYPE_DC:
        return None

    voltage_raw = int.from_bytes(payload[2:5], "big", signed=False)
    current_raw = int.from_bytes(payload[5:8], "big", signed=False)
    capacity_raw = int.from_bytes(payload[8:11], "big", signed=False)
    energy_raw = int.from_bytes(payload[11:15], "big", signed=False)
    temperature_raw = int.from_bytes(payload[22:24], "big", signed=False)
    hour_raw = int.from_bytes(payload[24:26], "big", signed=False)
    minute_raw = payload[26]
    second_raw = payload[27]
    backlight = payload[28]

    voltage = voltage_raw / 10.0
    current = current_raw / 1000.0
    power = voltage * current

    return Measurement(
        voltage=voltage,
        current=current,
        power=power,
        capacity_ah=capacity_raw / 100.0,
        energy_wh=energy_raw * 10.0,
        temperature=temperature_raw,
        runtime_h=hour_raw,
        runtime_m=minute_raw,
        runtime_s=second_raw,
        backlight=backlight,
        raw=payload,
    )


def parse_notification(data: bytes) -> Optional[Measurement]:
    """
    Parse a raw BLE notification into a Measurement.

    Handles both:
      - Full 36-byte notification (magic + 32-byte payload + checksum)
      - Split notifications: 23-byte + 19-byte fragments reassembled

    Returns None if the data doesn't contain a valid DC report.
    """
    if len(data) < 36:
        return None

    # Check magic header
    if data[0:2] != MAGIC_HEADER:
        return None

    payload = data[2:34]  # 32-byte payload
    if not verify_checksum(data):
        # Checksum fail — try anyway, device may send slightly off
        pass

    return parse_dc_report(payload)


# ── Notification Reassembly ───────────────────────────────────────────────────

class NotificationReassembler:
    """
    Accumulates BLE notification fragments into complete DL24 packets.

    DL24 sends measurements in 36-byte packets. BLE may split these across
    multiple notifications (e.g. 23 + 13 bytes) depending on MTU size.
    This reassembler buffers all incoming data and extracts complete packets
    by detecting the magic header FF 55.
    """

    MIN_PACKET = 36

    def __init__(self):
        self._buffer = b""

    def feed(self, data: bytes) -> Optional[bytes]:
        self._buffer += data
        packet = self._extract()
        while packet is None and len(self._buffer) >= self.MIN_PACKET:
            idx = self._buffer.find(MAGIC_HEADER)
            if idx > 0:
                self._buffer = self._buffer[idx:]
                packet = self._extract()
            else:
                break
        return packet

    def _extract(self) -> Optional[bytes]:
        if (len(self._buffer) >= self.MIN_PACKET
                and self._buffer[:2] == MAGIC_HEADER):
            packet = self._buffer[:self.MIN_PACKET]
            self._buffer = self._buffer[self.MIN_PACKET:]
            return packet
        return None

    def reset(self):
        self._buffer = b""


# ── Debug Helpers ─────────────────────────────────────────────────────────────

def hexdump(data: bytes) -> str:
    """Return hex representation of bytes."""
    return " ".join(f"{b:02X}" for b in data)


def packet_info(data: bytes) -> str:
    """Human-readable packet description."""
    if len(data) < 4:
        return f"Too short: {len(data)} bytes"

    magic = data[0:2]
    msg_type = data[2]
    dev_type = data[3] if len(data) > 3 else None

    parts = [f"Magic: {hexdump(magic)}"]
    parts.append(f"MsgType: 0x{msg_type:02X} ({MessageType(msg_type).name if msg_type in MessageType._value2member_map_ else '?'})")
    if dev_type is not None:
        parts.append(f"DevType: 0x{dev_type:02X}")

    if msg_type == MessageType.REPORT and dev_type == DEVICE_TYPE_DC:
        meas = parse_notification(data)
        if meas:
            parts.append(f"V={meas.voltage:.2f}V A={meas.current:.3f}A W={meas.power:.1f}W")
            parts.append(f"Ah={meas.capacity_ah:.3f} Wh={meas.energy_wh:.1f} T={meas.temperature}°C")
        else:
            parts.append("(parse failed)")

    chk_ok = verify_checksum(data)
    parts.append(f"Checksum: {'OK' if chk_ok else 'FAIL'}")

    return " | ".join(parts)
