"""Thread-safe live dashboard state and telemetry ingestion."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .common import is_ip_address, measurement_timestamp_ms, now_ms
from .device_profiles import (
    DeviceRegistry,
    decode_json_or_value,
    extract_device_name,
    has_valid_position,
    normalize_position,
)
from .models import DeviceRecord
from .network import public_mqtt_config
from .statistics import StatisticsSubscriptionRegistry
from .storage import TelemetryLogger
from .tiles import discover_mbtiles

FIX_MODE_LABELS = {0: "NO FIX", 1: "GNSS FIX", 2: "DGPS", 3: "RTK FLOAT", 4: "RTK FIXED"}
NTRIP_STATUS_LABELS = {0: "DISCONNECTED", 1: "CONNECTED"}


class DashboardState:
    """Owns live devices while transports remain deliberately device-agnostic."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.device_registry = DeviceRegistry(config)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._version = 0
        self._devices: dict[str, DeviceRecord] = {}
        self._peers: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._device_connection_state: dict[str, bool] = {}
        self.started_ms = now_ms()
        self.mbtiles = discover_mbtiles()
        self.public_mqtt = public_mqtt_config(self.config["mqtt"])
        self.telemetry_logger = TelemetryLogger(config, self.device_registry)
        self.statistics_registry = StatisticsSubscriptionRegistry(self.telemetry_logger)
        self.telemetry_logger.start()
        self.start_connection_monitor()

    def start_connection_monitor(self) -> None:
        thread = threading.Thread(target=self._monitor_device_connections, name="device-connection-monitor", daemon=True)
        thread.start()

    def _monitor_device_connections(self) -> None:
        timeout_ms = int(float(self.config.get("devices", {}).get("disconnectedAfterSec", 5)) * 1000)
        while True:
            time.sleep(1)
            current_ms = now_ms()
            disconnected: list[tuple[str, str, int]] = []
            with self._condition:
                for device_id, record in self._devices.items():
                    last_telemetry_ms = int(record.last_telemetry_seen_ms or record.last_position_seen_ms or 0)
                    if not last_telemetry_ms:
                        continue
                    is_connected = current_ms - last_telemetry_ms <= timeout_ms
                    if self._device_connection_state.get(device_id, True) and not is_connected:
                        self._device_connection_state[device_id] = False
                        disconnected.append((device_id, record.display_name or device_id, last_telemetry_ms))
            for device_id, display_name, last_telemetry_ms in disconnected:
                self.telemetry_logger.log_event(
                    "device_disconnected", f"{display_name} telemetry disconnected",
                    {"last_telemetry_seen_ms": last_telemetry_ms}, device_id=device_id, severity="warn",
                )

    def configured_device_name(self, *keys: str) -> str:
        dashboard = self.config.get("dashboard", {})
        names = dashboard.get("deviceNames") or dashboard.get("roverNames") or {}
        if not isinstance(names, dict):
            return ""
        for key in keys:
            value = names.get(str(key)) if key else None
            name = str(value).strip() if value is not None else ""
            if name and not is_ip_address(name):
                return name
        return ""

    # Kept for integrations using the old method name.
    configured_rover_name = configured_device_name

    def publish_event(self, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
        event = {"type": event_type, "message": message, "data": data or {}, "at_ms": now_ms()}
        with self._condition:
            self._events = (self._events + [event])[-80:]
            self._version += 1
            self._condition.notify_all()
        self.telemetry_logger.log_event(event_type, message, data)

    @staticmethod
    def _topic_identity(topic: str) -> tuple[str, str]:
        """Return ``(device_id, device_type)`` encoded in a conventional topic."""
        if topic == "info/mcu":
            return "", ""
        parts = [part for part in topic.strip("/").split("/") if part]
        if len(parts) == 2 and parts[0] in {"batch_ds", "telemetry", "info"}:
            return parts[1], ""
        if len(parts) == 3 and parts[0] == "telemetry":
            return parts[2], parts[1]
        if len(parts) == 3 and parts[0] == "devices" and parts[2] == "telemetry":
            return parts[1], ""
        return "", ""

    def update_from_mqtt(
        self, *, topic: str, raw_payload: bytes, client_id: str, username: str, source_host: str
    ) -> None:
        decoded = decode_json_or_value(raw_payload)
        payload = dict(decoded) if isinstance(decoded, dict) else {}
        topic_device_id, topic_device_type = self._topic_identity(topic)
        info_payload: dict[str, Any] | None = None

        if topic == "info/mcu" and isinstance(decoded, dict):
            info_payload = dict(decoded)
        elif topic.startswith("info/") and isinstance(decoded, dict):
            info_payload = dict(decoded)
        elif topic.startswith("ds/"):
            payload = {topic.split("/", 1)[1]: decoded}
        elif topic.startswith(("batch_ds/", "telemetry/", "devices/")) and isinstance(decoded, dict):
            payload = dict(decoded)
        elif topic != "batch_ds" and not isinstance(decoded, dict):
            payload = {topic.replace("/", "_"): decoded}

        if topic_device_type and not payload.get("device_type") and not payload.get("deviceType"):
            payload["device_type"] = topic_device_type
        explicit_id = payload.get("device_id") or payload.get("deviceId") or topic_device_id
        if explicit_id:
            device_id = str(explicit_id)
        elif client_id and not client_id.startswith("dashboard-auto-"):
            device_id = client_id
        elif username and username != "device":
            device_id = username
        else:
            device_id = source_host

        normalized, profile = self.device_registry.normalize(payload, "mqtt")
        display_name = (
            extract_device_name(info_payload) or extract_device_name(normalized)
            or self.configured_device_name(device_id, client_id, username, source_host)
        )
        seen_ms = now_ms()
        with self._condition:
            record = self._devices.get(device_id)
            if record is None:
                record = DeviceRecord(device_id=device_id, display_name=display_name or device_id)
                self._devices[device_id] = record
            elif display_name:
                record.display_name = display_name

            if info_payload is not None:
                record.info = info_payload
            else:
                record.device_type = profile.key
                record.profile = profile.to_dict()
                measured_ms = measurement_timestamp_ms(normalized)
                if has_valid_position(normalized):
                    record.last_position_seen_ms = seen_ms
                record.last_telemetry_seen_ms = seen_ms
                record.last_measurement_ms = measured_ms or seen_ms
                record.telemetry.update(normalized)
                if "fix_mode" in record.telemetry:
                    record.telemetry["fix_mode_label"] = FIX_MODE_LABELS.get(record.telemetry.get("fix_mode"), "UNKNOWN")
                if "ntrip_status" in record.telemetry:
                    record.telemetry["ntrip_status_label"] = NTRIP_STATUS_LABELS.get(record.telemetry.get("ntrip_status"), "UNKNOWN")

            record.last_seen_ms = seen_ms
            record.mqtt_client_id = client_id
            record.source_host = source_host
            record.username = username
            self._version += 1
            self._condition.notify_all()
            logged_display_name = record.display_name or device_id
            was_disconnected = info_payload is None and bool(normalized) and self._device_connection_state.get(device_id) is False
            if info_payload is None and normalized:
                self._device_connection_state[device_id] = True

        logging.debug("MQTT %s from %s (%s): %s", topic, device_id, profile.key, decoded)
        if info_payload is None:
            if was_disconnected:
                self.telemetry_logger.log_event(
                    "device_reconnected", f"{logged_display_name} telemetry reconnected",
                    {"topic": topic, "source_host": source_host}, device_id=device_id,
                )
            self.telemetry_logger.log_sample(
                device_id=device_id, display_name=logged_display_name, payload=normalized, source_type="mqtt",
                topic=topic, source_host=source_host, mqtt_client_id=client_id, username=username,
                at_ms=measurement_timestamp_ms(normalized),
            )

    def update_from_peer_udp(self, payload: dict[str, Any], source_host: str, max_age_sec: float) -> None:
        if payload.get("schema") != "crane-rover-peer-v1":
            return
        device_id = str(payload.get("device_id") or "")
        if not device_id:
            return
        peer, profile = self.device_registry.normalize(dict(payload), "udp_peer")
        peer.update({"source_host": source_host, "last_seen_ms": now_ms(), "max_age_sec": max_age_sec})
        peer["display_name"] = extract_device_name(peer) or self.configured_device_name(device_id, source_host) or device_id
        peer["last_position_seen_ms"] = peer["last_seen_ms"] if has_valid_position(peer) else 0
        with self._condition:
            self._peers[device_id] = peer
            self._version += 1
            self._condition.notify_all()
        self.telemetry_logger.log_sample(
            device_id=device_id, display_name=str(peer["display_name"]), payload=peer,
            source_type=profile.key, source_host=source_host,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            current_ms = now_ms()
            peer_max_age = float(self.config.get("udpPeers", {}).get("maxAgeSec", 5))
            peers = {}
            for device_id, peer in self._peers.items():
                copy = dict(peer)
                copy["stale"] = current_ms - int(copy.get("last_seen_ms", 0)) > peer_max_age * 1000
                peers[device_id] = copy
            return {
                "version": self._version,
                "server": {
                    "started_ms": self.started_ms, "now_ms": current_ms,
                    "mqtt": self.public_mqtt, "http": self.config["http"],
                    "udpPeers": self.config["udpPeers"], "dashboard": self.config["dashboard"],
                    "device_types": self.device_registry.catalog(), "mbtiles": self.mbtiles,
                },
                "devices": {key: record.to_dict() for key, record in self._devices.items()},
                "peers": peers, "events": list(self._events),
            }

    def wait_for_update(self, version: int, timeout: float = 25.0) -> tuple[bool, dict[str, Any]]:
        with self._condition:
            if self._version <= version:
                self._condition.wait(timeout)
            snapshot = self.snapshot()
            return int(snapshot["version"]) > version, snapshot
