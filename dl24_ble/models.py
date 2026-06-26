"""Core DL24 types and configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import yaml
from pathlib import Path


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"


@dataclass
class DL24Config:
    device_name: str = "DL24_BLE"
    device_address: str = ""
    scan_timeout: float = 5.0
    ble_interface: str = "hci0"
    reconnect: bool = True
    reconnect_delay: float = 2.0
    data_format: str = "csv"
    output_dir: str = "./logs"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> DL24Config:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        dl24 = data.get("dl24", {})
        return cls(**{k: v for k, v in dl24.items() if k in cls.__dataclass_fields__})

    def to_yaml(self, path: str | Path) -> None:
        with open(path, "w") as f:
            yaml.safe_dump({"dl24": self.__dict__}, f, default_flow_style=False)


@dataclass
class BatteryTestConfig:
    discharge_current: float = 1.0
    cutoff_voltage: float = 3.0
    max_duration_minutes: int = 0
    sample_interval: float = 1.0
    output_prefix: str = "battery_test"


@dataclass
class TestResult:
    start_voltage: float = 0.0
    end_voltage: float = 0.0
    total_capacity_ah: float = 0.0
    total_energy_wh: float = 0.0
    duration_seconds: int = 0
    avg_temperature: float = 0.0
    max_temperature: float = 0.0
    measurement_count: int = 0
    completed: bool = False
    reason: str = ""
