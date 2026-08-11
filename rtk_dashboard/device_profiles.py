"""Device type registry, payload normalization, and UI metadata.

Adding a device type belongs here (or in ``devices.types`` in YAML), not in the
transport, database, or HTTP layers. Unknown JSON devices use the generic
profile and retain every raw field.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .common import is_ip_address

NAME_FIELDS = (
    "display_name", "displayName", "rover_name", "roverName", "robot_name", "robotName",
    "device_name", "deviceName", "Device Name", "thing_name", "Thing Name", "hostname",
    "host_name", "hostName", "Name", "name",
)

COMMON_ALIASES = {
    "latitude": ("lat", "gps_latitude"),
    "longitude": ("lon", "lng", "gps_longitude"),
    "battery_percent": ("battery", "battery_pct", "batteryPercent"),
    "battery_voltage_v": ("voltage", "battery_voltage", "batteryVoltage"),
}


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str = ""
    digits: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "unit": self.unit, "digits": self.digits}


@dataclass(frozen=True)
class DeviceProfile:
    key: str
    label: str
    category: str = "sensor"
    match_fields: tuple[str, ...] = ()
    match_aliases: bool = True
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    metrics: tuple[Metric, ...] = ()
    supports_map: bool = False
    supports_peer_safety: bool = False

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        for canonical, aliases in {**COMMON_ALIASES, **self.aliases}.items():
            if normalized.get(canonical) not in (None, ""):
                continue
            for alias in aliases:
                if normalized.get(alias) not in (None, ""):
                    normalized[canonical] = normalized[alias]
                    break
        normalize_position(normalized)
        normalized["device_type"] = self.key
        return normalized

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "supports_map": self.supports_map,
            "supports_peer_safety": self.supports_peer_safety,
            "metrics": [metric.to_dict() for metric in self.metrics],
        }


BUILTIN_PROFILES = (
    DeviceProfile(
        "crane_rover", "Crane Rover", "vehicle",
        ("nearest_peer_distance_m", "nearest_peer_safe_distance_m", "fix_mode"),
        metrics=(
            Metric("fix_mode_label", "Fix"), Metric("ntrip_status_label", "NTRIP"),
            Metric("satellites", "Satellites", "", 0), Metric("hdop", "HDOP", "", 2),
            Metric("battery_percent", "Battery", "%", 1), Metric("rtcm_age_sec", "RTCM Age", " s", 1),
            Metric("local_accuracy_m", "Accuracy", " m", 3),
        ),
        supports_map=True, supports_peer_safety=True,
    ),
    DeviceProfile(
        "tide_sensor", "Tide Logger", "environment",
        ("tide_m", "water_level_m", "tide_level_m", "distance_to_water_mm", "measurement_quality"),
        match_aliases=False,
        aliases={
            "water_level_m": ("tide_m", "tide_level_m", "level_m"),
            "distance_to_water_mm": ("distance_mm", "water_distance_mm"),
            "battery_voltage_v": ("battery_voltage", "batteryVoltage"),
            "solar_voltage_v": ("solar_voltage", "panel_voltage_v"),
            "system_voltage_v": ("system_5v_voltage", "systemVoltage"),
            "system_current_a": ("system_current", "systemCurrent"),
            "temperature_c": ("temperature", "temp_c", "temperatureC"),
            "humidity_percent": ("humidity", "humidity_pct", "relative_humidity"),
            "measurement_quality": ("quality", "measurement_quality_code"),
            "samples_acquired": ("acquired_samples",),
            "samples_used": ("used_samples",),
            "mad_outliers": ("outlier_samples",),
            "distance_mad_mm": ("distance_mad",),
            "acquisition_duration_ms": ("acquisition_ms",),
            "pending_records": ("pending_offline_records",),
            "dropped_records": ("dropped_offline_records",),
            "sequence": ("record_sequence",),
        },
        metrics=(
            Metric("water_level_m", "Water Level", " m", 3),
            Metric("distance_to_water_mm", "Distance to Water", " mm", 0),
            Metric("battery_voltage_v", "Battery", " V", 3),
            Metric("solar_voltage_v", "Solar", " V", 3),
            Metric("system_voltage_v", "System 5V", " V", 3),
            Metric("system_current_a", "System Current", " A", 3),
            Metric("temperature_c", "Temperature", " °C", 2),
            Metric("humidity_percent", "Humidity", "%RH", 2),
            Metric("measurement_quality", "Quality", "", 0),
            Metric("samples_used", "Samples Used", "", 0),
            Metric("distance_mad_mm", "Distance MAD", " mm", 2),
            Metric("pending_records", "Pending Records", "", 0),
        ),
        supports_map=True,
    ),
    DeviceProfile(
        "weather_station", "Weather Station", "environment",
        ("temperature_c", "humidity_percent", "wind_speed_mps"),
        aliases={
            "temperature_c": ("temperature", "temp_c", "temperatureC"),
            "humidity_percent": ("humidity", "humidity_pct", "relative_humidity"),
            "wind_speed_mps": ("wind_speed", "wind_mps", "windSpeed"),
        },
        metrics=(
            Metric("temperature_c", "Temperature", " °C", 1), Metric("humidity_percent", "Humidity", "%", 1),
            Metric("wind_speed_mps", "Wind Speed", " m/s", 1), Metric("rainfall_mm", "Rainfall", " mm", 1),
            Metric("battery_percent", "Battery", "%", 1),
        ),
        supports_map=True,
    ),
    DeviceProfile(
        "truck", "Truck", "vehicle", ("truck_id", "speed_kph", "heading_deg"),
        aliases={"speed_kph": ("speed", "vehicle_speed_kph"), "heading_deg": ("heading", "course_deg")},
        metrics=(
            Metric("speed_kph", "Speed", " km/h", 1), Metric("heading_deg", "Heading", "°", 0),
            Metric("battery_percent", "Battery", "%", 1),
        ),
        supports_map=True,
    ),
    DeviceProfile(
        "device", "Generic Device", "sensor",
        metrics=(Metric("battery_percent", "Battery", "%", 1), Metric("battery_voltage_v", "Voltage", " V", 2)),
        supports_map=True,
    ),
)


class DeviceRegistry:
    def __init__(self, config: dict[str, Any] | None = None):
        self._profiles = {profile.key: profile for profile in BUILTIN_PROFILES}
        custom_types = (config or {}).get("devices", {}).get("types", {})
        if isinstance(custom_types, dict):
            for key, values in custom_types.items():
                if isinstance(values, dict):
                    self._profiles[str(key)] = self._custom_profile(str(key), values)

    def _custom_profile(self, key: str, values: dict[str, Any]) -> DeviceProfile:
        aliases = {
            str(name): tuple(map(str, choices if isinstance(choices, list) else [choices]))
            for name, choices in (values.get("aliases") or {}).items()
        }
        configured_metrics = values.get("metrics", [])
        if isinstance(configured_metrics, dict):
            configured_metrics = [{"key": key, **details} for key, details in configured_metrics.items() if isinstance(details, dict)]
        metrics = tuple(
            Metric(str(item["key"]), str(item.get("label") or item["key"]), str(item.get("unit") or ""), int(item.get("digits", 1)))
            for item in configured_metrics if isinstance(item, dict) and item.get("key")
        )
        return DeviceProfile(
            key=key, label=str(values.get("label") or key.replace("_", " ").title()),
            category=str(values.get("category") or "sensor"),
            match_fields=tuple(map(str, values.get("matchFields") or ())), aliases=aliases, metrics=metrics,
            supports_map=bool(values.get("supportsMap", True)),
            supports_peer_safety=bool(values.get("supportsPeerSafety", False)),
        )

    def profile(self, key: str) -> DeviceProfile:
        profile = self._profiles.get(key)
        if profile is not None:
            return profile
        generic = self._profiles["device"]
        return DeviceProfile(
            key=key or generic.key,
            label=(key or generic.key).replace("_", " ").replace("-", " ").title(),
            category=generic.category,
            aliases=generic.aliases,
            metrics=generic.metrics,
            supports_map=generic.supports_map,
        )

    def infer_type(self, payload: dict[str, Any], source_type: str = "") -> str:
        explicit = str(payload.get("device_type") or payload.get("deviceType") or payload.get("type") or "").strip()
        if explicit:
            return explicit
        keys = set(payload)
        for profile in self._profiles.values():
            alias_fields = {alias for aliases in profile.aliases.values() for alias in aliases} if profile.match_aliases else set()
            if profile.key != "device" and keys.intersection(set(profile.match_fields) | alias_fields):
                return profile.key
        return source_type if source_type in self._profiles else "device"

    def normalize(self, payload: dict[str, Any], source_type: str = "") -> tuple[dict[str, Any], DeviceProfile]:
        profile = self.profile(self.infer_type(payload, source_type))
        return profile.normalize(payload), profile

    def catalog(self) -> dict[str, dict[str, Any]]:
        return {key: profile.to_dict() for key, profile in self._profiles.items()}


DEFAULT_DEVICE_REGISTRY = DeviceRegistry()


def infer_device_type(payload: dict[str, Any], source_type: str = "") -> str:
    """Compatibility helper for persistence and third-party imports."""
    return DEFAULT_DEVICE_REGISTRY.infer_type(payload, source_type)


def normalize_position(payload: dict[str, Any]) -> None:
    position = payload.get("position")
    if isinstance(position, (list, tuple)) and len(position) >= 2:
        payload.setdefault("longitude", position[0])
        payload.setdefault("latitude", position[1])
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        payload["position"] = [payload["longitude"], payload["latitude"]]


def has_valid_position(payload: dict[str, Any]) -> bool:
    try:
        latitude, longitude = float(payload["latitude"]), float(payload["longitude"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(latitude) and math.isfinite(longitude) and (latitude != 0 or longitude != 0)


def extract_device_name(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in NAME_FIELDS:
        value = payload.get(key)
        name = str(value).strip() if value is not None else ""
        if name and not is_ip_address(name):
            return name
    return ""


def coerce_value(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    try:
        return float(stripped) if "." in stripped else int(stripped)
    except ValueError:
        return stripped


def decode_json_or_value(raw_payload: bytes) -> Any:
    text = raw_payload.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return coerce_value(text)
