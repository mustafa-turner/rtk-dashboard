"""In-memory domain models returned by the state API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceRecord:
    device_id: str
    display_name: str = ""
    device_type: str = "device"
    profile: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    last_seen_ms: int = 0
    last_telemetry_seen_ms: int = 0
    last_position_seen_ms: int = 0
    last_measurement_ms: int = 0
    mqtt_client_id: str = ""
    source_host: str = ""
    username: str = ""
    info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "display_name": self.display_name or self.device_id,
            "device_type": self.device_type,
            "profile": self.profile,
            "telemetry": self.telemetry,
            "last_seen_ms": self.last_seen_ms,
            "last_telemetry_seen_ms": self.last_telemetry_seen_ms,
            "last_position_seen_ms": self.last_position_seen_ms,
            "last_measurement_ms": self.last_measurement_ms,
            "mqtt_client_id": self.mqtt_client_id,
            "source_host": self.source_host,
            "username": self.username,
            "info": self.info,
        }
