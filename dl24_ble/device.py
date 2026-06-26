"""Async BLE device interface for Atorch DL24 using bleak."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic

from .protocol import (
    parse_notification,
    NotificationReassembler,
    build_command,
    DEVICE_TYPE_DC,
    Command,
    hexdump,
)
from .models import ConnectionState, DL24Config

logger = logging.getLogger(__name__)

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
CCCD_HANDLE = 0x000D
ENABLE_NOTIFY = b"\x01\x00"


class DL24Device:
    def __init__(self, config: Optional[DL24Config] = None):
        self.config = config or DL24Config()
        self._client: Optional[BleakClient] = None
        self._device: Optional[BLEDevice] = None
        self._state = ConnectionState.DISCONNECTED
        self._reassembler = NotificationReassembler()
        self._callbacks: list[Callable] = []
        self._should_run = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED and self._client is not None and self._client.is_connected

    @property
    def address(self) -> str:
        return self._device.address if self._device else ""

    def on_measurement(self, callback: Callable):
        self._callbacks.append(callback)

    async def discover(self, timeout: float | None = None) -> Optional[str]:
        timeout = timeout or self.config.scan_timeout
        self._state = ConnectionState.SCANNING
        logger.info(f"Scanning for {self.config.device_name}...")

        if self.config.device_address:
            logger.info(f"Using configured address: {self.config.device_address}")
            self._state = ConnectionState.DISCONNECTED
            return self.config.device_address

        try:
            devices = await BleakScanner.discover(timeout=timeout)
            for d in devices:
                name = d.name or ""
                if self.config.device_name in name:
                    logger.info(f"Found {name} at {d.address}")
                    self._device = d
                    self._state = ConnectionState.DISCONNECTED
                    return d.address
        except Exception as e:
            logger.error(f"Scan failed: {e}")

        self._state = ConnectionState.DISCONNECTED
        logger.warning(f"No device matching '{self.config.device_name}' found")
        return None

    async def connect(self, address: Optional[str] = None) -> bool:
        if not address:
            address = await self.discover()

        if not address:
            return False

        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(0.2)

            await self._kick_other_connections(address)

            self._state = ConnectionState.CONNECTING
            try:
                self._client = BleakClient(
                    address,
                    disconnected_callback=self._on_disconnect,
                    timeout=10.0,
                )
                await self._client.connect()
                self._state = ConnectionState.CONNECTED
                logger.info("Connected")
                await self._enable_notifications()
                return True
            except Exception:
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass

        self._state = ConnectionState.DISCONNECTED
        logger.error("All connection attempts failed")
        return False

    @staticmethod
    async def _kick_other_connections(address: str):
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "info", address,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            if b"Connected: yes" in stdout:
                logger.info(f"Kicking existing connection from {address}")
                await asyncio.create_subprocess_exec(
                    "bluetoothctl", "disconnect", address,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.sleep(0.3)
        except Exception:
            pass

    async def disconnect(self):
        self._state = ConnectionState.DISCONNECTING
        self._should_run = False
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._state = ConnectionState.DISCONNECTED
        logger.info("Disconnected")

    async def _enable_notifications(self):
        if not self._client:
            return

        try:
            await self._client.start_notify(
                CHARACTERISTIC_UUID,
                self._notification_handler,
            )
            await self._client.write_gatt_char(
                CHARACTERISTIC_UUID,
                ENABLE_NOTIFY,
                response=False,
            )
            logger.debug("Notifications enabled")
        except Exception as e:
            logger.warning(f"Notification setup: {e}")

    def _notification_handler(self, characteristic: BleakGATTCharacteristic, data: bytes):
        assembled = self._reassembler.feed(data)
        if not assembled:
            return

        measurement = parse_notification(assembled)
        if measurement is None:
            return

        for cb in self._callbacks:
            try:
                cb(measurement)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _on_disconnect(self, client: BleakClient):
        logger.warning("Device disconnected")
        self._state = ConnectionState.DISCONNECTED
        if self.config.reconnect:
            asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        delay = self.config.reconnect_delay
        for attempt in range(1, 100):
            if self._state == ConnectionState.CONNECTED:
                return
            if not self._should_run:
                return
            logger.info(f"Reconnect attempt {attempt} in {delay}s...")
            await asyncio.sleep(delay)
            address = self._device.address if self._device else self.config.device_address
            if address and await self.connect(address):
                return

    async def send_command(self, cmd: Command, value: int = 0):
        if not self._client or not self._client.is_connected:
            logger.warning("Not connected, cannot send command")
            return

        packet = build_command(DEVICE_TYPE_DC, cmd, value)
        logger.debug(f"Sending: {hexdump(packet)}")
        try:
            await self._client.write_gatt_char(CHARACTERISTIC_UUID, packet, response=False)
        except Exception as e:
            logger.error(f"Send failed: {e}")

    async def set_current(self, amps: float):
        logger.info(f"Setting current to {amps:.3f}A via [+] button")

    async def reset_all(self):
        await self.send_command(Command.RESET_ALL)
        logger.info("Sent RESET ALL")

    async def reset_ah(self):
        await self.send_command(Command.RESET_AH)

    async def reset_wh(self):
        await self.send_command(Command.RESET_WH)

    async def reset_time(self):
        await self.send_command(Command.RESET_TIME)

    async def run_forever(self):
        self._should_run = True
        try:
            while self._should_run:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.disconnect()
