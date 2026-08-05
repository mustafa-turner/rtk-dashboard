from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import sqlite3
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import yaml
except ImportError:
    yaml = None


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
MBTILES_ROOT = ROOT / "mbtiles"

FIX_MODE_LABELS = {
    0: "NO FIX",
    1: "GNSS FIX",
    2: "DGPS",
    3: "RTK FLOAT",
    4: "RTK FIXED",
}

NTRIP_STATUS_LABELS = {
    0: "DISCONNECTED",
    1: "CONNECTED",
}

ROVER_NAME_FIELDS = (
    "display_name",
    "displayName",
    "rover_name",
    "roverName",
    "robot_name",
    "robotName",
    "device_name",
    "deviceName",
    "Device Name",
    "thing_name",
    "Thing Name",
    "hostname",
    "host_name",
    "hostName",
    "Name",
    "name",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_simple_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if lower in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_simple_scalar(value)

    return root


def load_config(path: Path) -> dict[str, Any]:
    defaults = {
        "mqtt": {"host": "0.0.0.0", "port": 1883},
        "http": {"host": "0.0.0.0", "port": 8080},
        "udpPeers": {"enabled": True, "host": "0.0.0.0", "port": 5005, "maxAgeSec": 5},
        "logging": {
            "enabled": True,
            "databasePath": "data/rtk-dashboard.sqlite",
            "rawRetentionDays": 30,
            "summaryRetentionDays": 0,
            "sampleMinIntervalSec": 2,
            "rollupIntervalSec": 300,
        },
        "dashboard": {
            "title": "Crane Rover Dashboard",
            "defaultCenter": {"latitude": -2.5489, "longitude": 118.0149, "zoom": 5},
            "roverAntennaOffset": {"x": 0, "y": 0},
        },
    }

    if not path.exists():
        return defaults
    with path.open("r", encoding="utf-8") as fh:
        text = fh.read()

    loaded = yaml.safe_load(text) if yaml is not None else parse_simple_yaml(text)
    loaded = loaded or {}

    for section, values in defaults.items():
        if not isinstance(loaded.get(section), dict):
            loaded[section] = {}
        merged = dict(values)
        merged.update(loaded[section])
        loaded[section] = merged
    return loaded


def open_mbtiles(path: Path) -> sqlite3.Connection:
    uri_path = quote(str(path.resolve()), safe="/")
    return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)


def parse_mbtiles_bounds(value: Any) -> list[float] | None:
    try:
        bounds = [float(part.strip()) for part in str(value).split(",")]
    except (TypeError, ValueError):
        return None
    if len(bounds) != 4 or not all(math.isfinite(part) for part in bounds):
        return None
    west, south, east, north = bounds
    if west >= east or south >= north:
        return None
    return [west, south, east, north]


def normalize_tile_format(value: Any) -> str:
    tile_format = str(value or "png").strip().lower().lstrip(".")
    if tile_format == "jpeg":
        return "jpg"
    if tile_format in {"jpg", "png", "webp", "pbf"}:
        return tile_format
    return "png"


def tile_content_type(tile_format: str) -> str:
    return {
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "pbf": "application/x-protobuf",
    }.get(normalize_tile_format(tile_format), "application/octet-stream")


def read_mbtiles_info(path: Path) -> dict[str, Any] | None:
    try:
        with open_mbtiles(path) as con:
            metadata = {str(name): value for name, value in con.execute("select name, value from metadata")}
            min_zoom, max_zoom = con.execute("select min(zoom_level), max(zoom_level) from tiles").fetchone()
    except sqlite3.Error as exc:
        logging.warning("MBTiles metadata skipped for %s: %s", path.name, exc)
        return None

    if min_zoom is None or max_zoom is None:
        return None

    tile_format = normalize_tile_format(metadata.get("format"))
    tileset_id = path.stem
    return {
        "id": tileset_id,
        "name": str(metadata.get("name") or tileset_id),
        "description": str(metadata.get("description") or ""),
        "type": str(metadata.get("type") or "overlay"),
        "format": tile_format,
        "bounds": parse_mbtiles_bounds(metadata.get("bounds")),
        "minZoom": int(min_zoom),
        "maxZoom": int(max_zoom),
        "tileUrl": f"/tiles/{quote(tileset_id, safe='')}/{{z}}/{{x}}/{{y}}.{tile_format}",
    }


def discover_mbtiles() -> list[dict[str, Any]]:
    if not MBTILES_ROOT.exists():
        return []
    tilesets: list[dict[str, Any]] = []
    for path in sorted(MBTILES_ROOT.glob("*.mbtiles")):
        if path.stem.lower() in {"old", "backup", "archive"} or path.stem.lower().startswith(("old_", "backup_")):
            continue
        info = read_mbtiles_info(path)
        if info is not None:
            tilesets.append(info)
    return tilesets


def coerce_value(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if stripped == "":
        return ""
    lower = stripped.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    try:
        if "." in stripped:
            return float(stripped)
        return int(stripped)
    except ValueError:
        return stripped


def normalize_position(payload: dict[str, Any]) -> None:
    if "position" in payload and isinstance(payload["position"], list) and len(payload["position"]) >= 2:
        lon, lat = payload["position"][0], payload["position"][1]
        payload.setdefault("longitude", lon)
        payload.setdefault("latitude", lat)
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        payload["position"] = [payload["longitude"], payload["latitude"]]


def has_valid_position(payload: dict[str, Any]) -> bool:
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    if lat is None or lon is None:
        return False
    try:
        lat_num = float(lat)
        lon_num = float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat_num) and math.isfinite(lon_num) and (lat_num != 0 or lon_num != 0)


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def finite_int(value: Any) -> int | None:
    number = finite_float(value)
    if number is None:
        return None
    return int(number)


def first_numeric(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = finite_float(payload.get(key))
        if value is not None:
            return value
    return None


def infer_device_type(payload: dict[str, Any], source_type: str = "") -> str:
    explicit = str(payload.get("device_type") or payload.get("deviceType") or payload.get("type") or "").strip()
    if explicit:
        return explicit
    keys = set(payload)
    if {"nearest_peer_distance_m", "nearest_peer_safe_distance_m", "fix_mode"} & keys:
        return "crane_rover"
    if {"tide_m", "water_level_m", "tide_level_m"} & keys:
        return "tide_sensor"
    if {"truck_id", "speed_kph", "heading_deg"} & keys:
        return "truck"
    return source_type or "device"


def parse_range_ms(value: str | None, default_ms: int = 24 * 60 * 60 * 1000) -> int:
    if not value:
        return default_ms
    text = value.strip().lower()
    units = {
        "h": 60 * 60 * 1000,
        "d": 24 * 60 * 60 * 1000,
        "w": 7 * 24 * 60 * 60 * 1000,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            amount = finite_float(text[:-1])
            return int(amount * multiplier) if amount and amount > 0 else default_ms
    amount = finite_float(text)
    return int(amount) if amount and amount > 0 else default_ms


def parse_query_ms(value: str | None, default: int) -> int:
    parsed = finite_float(value)
    return int(parsed) if parsed is not None and parsed > 0 else default


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def normalize_statistics_range(value: str | None) -> str:
    normalized = str(value or "24h").strip().lower()
    return normalized if normalized in {"live", "24h", "7d", "30d"} else "24h"


def normalize_statistics_device_id(value: str | None) -> str:
    return str(value or "").strip()[:200]


def is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def extract_rover_name(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ROVER_NAME_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        name = str(value).strip()
        if name and not is_ip_address(name):
            return name
    return ""


def decode_json_or_value(raw_payload: bytes) -> Any:
    text = raw_payload.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return coerce_value(text)


@dataclass
class DeviceRecord:
    device_id: str
    display_name: str = ""
    telemetry: dict[str, Any] = field(default_factory=dict)
    last_seen_ms: int = 0
    last_telemetry_seen_ms: int = 0
    last_position_seen_ms: int = 0
    mqtt_client_id: str = ""
    source_host: str = ""
    username: str = ""
    info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "display_name": self.display_name or self.device_id,
            "telemetry": self.telemetry,
            "last_seen_ms": self.last_seen_ms,
            "last_telemetry_seen_ms": self.last_telemetry_seen_ms,
            "last_position_seen_ms": self.last_position_seen_ms,
            "mqtt_client_id": self.mqtt_client_id,
            "source_host": self.source_host,
            "username": self.username,
            "info": self.info,
        }


class TelemetryLogger:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        logging_cfg = config.get("logging", {})
        self.enabled = bool(logging_cfg.get("enabled", True))
        self.raw_retention_days = int(logging_cfg.get("rawRetentionDays", 30) or 0)
        self.summary_retention_days = int(logging_cfg.get("summaryRetentionDays", 0) or 0)
        self.sample_min_interval_ms = int(float(logging_cfg.get("sampleMinIntervalSec", 2) or 0) * 1000)
        self.rollup_interval_sec = float(logging_cfg.get("rollupIntervalSec", 300) or 300)
        self._lock = threading.RLock()
        self._last_sample_by_device: dict[str, int] = {}
        self._last_uptime_by_device: dict[str, float] = {}
        self._last_cleanup_ms = 0
        self._stop = threading.Event()

        if not self.enabled:
            self.path = Path("")
            self.con = None
            return

        db_path = Path(str(logging_cfg.get("databasePath") or "data/rtk-dashboard.sqlite"))
        self.path = db_path if db_path.is_absolute() else ROOT / db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        if self.con is None:
            return
        with self._lock:
            self.con.execute("pragma journal_mode=WAL")
            self.con.execute("pragma synchronous=NORMAL")
            self.con.execute("pragma foreign_keys=ON")
            self.con.executescript(
                """
                create table if not exists devices (
                    device_id text primary key,
                    display_name text,
                    device_type text,
                    source_type text,
                    source_host text,
                    mqtt_client_id text,
                    username text,
                    first_seen_ms integer not null,
                    last_seen_ms integer not null
                );

                create table if not exists telemetry_samples (
                    id integer primary key autoincrement,
                    at_ms integer not null,
                    device_id text not null,
                    display_name text,
                    device_type text,
                    source_type text not null,
                    topic text,
                    source_host text,
                    mqtt_client_id text,
                    username text,
                    peer_id text,
                    latitude real,
                    longitude real,
                    fix_mode integer,
                    ntrip_status integer,
                    safe_distance_m real,
                    raw_distance_m real,
                    uncertainty_m real,
                    accuracy_m real,
                    peer_accuracy_m real,
                    battery_percent real,
                    battery_voltage_v real,
                    uptime_sec real,
                    payload_json text not null
                );

                create index if not exists idx_samples_at on telemetry_samples(at_ms);
                create index if not exists idx_samples_device_at on telemetry_samples(device_id, at_ms);
                create index if not exists idx_samples_peer_at on telemetry_samples(device_id, peer_id, at_ms);

                create table if not exists hourly_device_metrics (
                    hour_ms integer not null,
                    device_id text not null,
                    display_name text,
                    device_type text,
                    sample_count integer not null,
                    fix_fixed_count integer not null,
                    fix_float_count integer not null,
                    fix_no_count integer not null,
                    ntrip_connected_count integer not null,
                    ntrip_disconnected_count integer not null,
                    accuracy_min_m real,
                    accuracy_avg_m real,
                    accuracy_max_m real,
                    battery_min_percent real,
                    battery_avg_percent real,
                    battery_max_percent real,
                    uptime_max_sec real,
                    reset_count integer not null,
                    updated_ms integer not null,
                    primary key (hour_ms, device_id)
                );

                create table if not exists hourly_pair_metrics (
                    hour_ms integer not null,
                    device_id text not null,
                    peer_id text not null,
                    closest_safe_distance_m real,
                    closest_raw_distance_m real,
                    closest_uncertainty_m real,
                    closest_at_ms integer,
                    sample_count integer not null,
                    updated_ms integer not null,
                    primary key (hour_ms, device_id, peer_id)
                );

                create table if not exists system_events (
                    id integer primary key autoincrement,
                    at_ms integer not null,
                    event_type text not null,
                    device_id text,
                    severity text not null default 'info',
                    message text not null,
                    data_json text not null default '{}'
                );

                create index if not exists idx_events_at on system_events(at_ms);
                create index if not exists idx_events_device_at on system_events(device_id, at_ms);
                """
            )
            self.con.commit()

    def start(self) -> None:
        if not self.enabled:
            return
        thread = threading.Thread(target=self._run_rollups, name="telemetry-logger-rollups", daemon=True)
        thread.start()

    def _run_rollups(self) -> None:
        while not self._stop.wait(self.rollup_interval_sec):
            try:
                self.rollup_recent()
            except Exception as exc:
                logging.warning("Telemetry rollup failed: %s", exc)

    def log_event(
        self,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
        *,
        device_id: str | None = None,
        severity: str = "info",
    ) -> None:
        if not self.enabled or self.con is None:
            return
        at_ms = now_ms()
        with self._lock:
            self.con.execute(
                """
                insert into system_events (at_ms, event_type, device_id, severity, message, data_json)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    at_ms,
                    event_type,
                    device_id,
                    severity,
                    message,
                    json.dumps(data or {}, separators=(",", ":"), sort_keys=True),
                ),
            )
            self.con.commit()

    def log_sample(
        self,
        *,
        device_id: str,
        display_name: str,
        payload: dict[str, Any],
        source_type: str,
        topic: str = "",
        source_host: str = "",
        mqtt_client_id: str = "",
        username: str = "",
    ) -> None:
        if not self.enabled or self.con is None or not payload:
            return
        at_ms = now_ms()
        last_sample_ms = self._last_sample_by_device.get(device_id, 0)
        if self.sample_min_interval_ms and at_ms - last_sample_ms < self.sample_min_interval_ms:
            return

        device_type = infer_device_type(payload, source_type)
        uptime_sec = first_numeric(
            payload,
            (
                "uptime_sec",
                "app_uptime_sec",
                "device_uptime_sec",
                "uptime_s",
                "app_uptime_s",
                "device_uptime_s",
                "uptime",
            ),
        )
        previous_uptime = self._last_uptime_by_device.get(device_id)
        if uptime_sec is not None:
            self._last_uptime_by_device[device_id] = uptime_sec

        lat = finite_float(payload.get("latitude"))
        lon = finite_float(payload.get("longitude"))
        if (lat is None or lon is None) and isinstance(payload.get("position"), list) and len(payload["position"]) >= 2:
            lon = finite_float(payload["position"][0])
            lat = finite_float(payload["position"][1])

        raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
        values = (
            at_ms,
            device_id,
            display_name,
            device_type,
            source_type,
            topic,
            source_host,
            mqtt_client_id,
            username,
            str(payload.get("nearest_peer_id") or payload.get("peer_id") or ""),
            lat,
            lon,
            finite_int(payload.get("fix_mode")),
            finite_int(payload.get("ntrip_status")),
            finite_float(payload.get("nearest_peer_safe_distance_m")),
            finite_float(payload.get("nearest_peer_distance_m")),
            finite_float(payload.get("nearest_peer_uncertainty_m")),
            first_numeric(payload, ("local_accuracy_m", "accuracy_m", "horizontal_accuracy_m")),
            finite_float(payload.get("nearest_peer_accuracy_m")),
            finite_float(payload.get("battery_percent")),
            finite_float(payload.get("battery_voltage_v")),
            uptime_sec,
            raw_json,
        )

        with self._lock:
            existing = self.con.execute("select last_seen_ms from devices where device_id = ?", (device_id,)).fetchone()
            if existing is not None and at_ms - int(existing["last_seen_ms"]) > 5 * 60 * 1000:
                self.con.execute(
                    """
                    insert into system_events (at_ms, event_type, device_id, severity, message, data_json)
                    values (?, 'telemetry_gap', ?, 'warn', ?, ?)
                    """,
                    (
                        at_ms,
                        device_id,
                        f"{display_name or device_id} resumed after telemetry gap",
                        json.dumps({"previous_last_seen_ms": int(existing["last_seen_ms"])}, separators=(",", ":")),
                    ),
                )

            self.con.execute(
                """
                insert into devices (
                    device_id, display_name, device_type, source_type, source_host, mqtt_client_id, username,
                    first_seen_ms, last_seen_ms
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(device_id) do update set
                    display_name = excluded.display_name,
                    device_type = excluded.device_type,
                    source_type = excluded.source_type,
                    source_host = excluded.source_host,
                    mqtt_client_id = excluded.mqtt_client_id,
                    username = excluded.username,
                    last_seen_ms = excluded.last_seen_ms
                """,
                (
                    device_id,
                    display_name,
                    device_type,
                    source_type,
                    source_host,
                    mqtt_client_id,
                    username,
                    at_ms,
                    at_ms,
                ),
            )
            self.con.execute(
                """
                insert into telemetry_samples (
                    at_ms, device_id, display_name, device_type, source_type, topic, source_host,
                    mqtt_client_id, username, peer_id, latitude, longitude, fix_mode, ntrip_status,
                    safe_distance_m, raw_distance_m, uncertainty_m, accuracy_m, peer_accuracy_m,
                    battery_percent, battery_voltage_v, uptime_sec, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            if previous_uptime is not None and uptime_sec is not None and uptime_sec + 5 < previous_uptime:
                self.con.execute(
                    """
                    insert into system_events (at_ms, event_type, device_id, severity, message, data_json)
                    values (?, 'device_reset', ?, 'warn', ?, ?)
                    """,
                    (
                        at_ms,
                        device_id,
                        f"{display_name or device_id} uptime reset",
                        json.dumps(
                            {"previous_uptime_sec": previous_uptime, "uptime_sec": uptime_sec},
                            separators=(",", ":"),
                        ),
                    ),
                )
            self.con.commit()
            self._last_sample_by_device[device_id] = at_ms

        if at_ms - self._last_cleanup_ms > 60 * 60 * 1000:
            self.cleanup()
            self._last_cleanup_ms = at_ms

    def cleanup(self) -> None:
        if not self.enabled or self.con is None:
            return
        current_ms = now_ms()
        with self._lock:
            if self.raw_retention_days > 0:
                cutoff = current_ms - self.raw_retention_days * 24 * 60 * 60 * 1000
                self.con.execute("delete from telemetry_samples where at_ms < ?", (cutoff,))
            if self.summary_retention_days > 0:
                cutoff = current_ms - self.summary_retention_days * 24 * 60 * 60 * 1000
                self.con.execute("delete from hourly_device_metrics where hour_ms < ?", (cutoff,))
                self.con.execute("delete from hourly_pair_metrics where hour_ms < ?", (cutoff,))
            self.con.commit()

    def rollup_recent(self, from_ms: int | None = None, to_ms: int | None = None) -> None:
        if not self.enabled or self.con is None:
            return
        current_ms = now_ms()
        end_ms = to_ms or current_ms
        start_ms = from_ms or (end_ms - max(2 * 60 * 60 * 1000, int(self.rollup_interval_sec * 2000)))
        start_hour_ms = (start_ms // 3600000) * 3600000
        updated_ms = current_ms
        with self._lock:
            rows = self.con.execute(
                """
                select
                    (at_ms / 3600000) * 3600000 as hour_ms,
                    device_id,
                    max(display_name) as display_name,
                    max(device_type) as device_type,
                    count(*) as sample_count,
                    sum(case when fix_mode = 4 then 1 else 0 end) as fix_fixed_count,
                    sum(case when fix_mode = 3 then 1 else 0 end) as fix_float_count,
                    sum(case when fix_mode is null or fix_mode not in (3, 4) then 1 else 0 end) as fix_no_count,
                    sum(case when ntrip_status = 1 then 1 else 0 end) as ntrip_connected_count,
                    sum(case when ntrip_status = 0 then 1 else 0 end) as ntrip_disconnected_count,
                    min(accuracy_m) as accuracy_min_m,
                    avg(accuracy_m) as accuracy_avg_m,
                    max(accuracy_m) as accuracy_max_m,
                    min(battery_percent) as battery_min_percent,
                    avg(battery_percent) as battery_avg_percent,
                    max(battery_percent) as battery_max_percent,
                    max(uptime_sec) as uptime_max_sec
                from telemetry_samples
                where at_ms >= ? and at_ms <= ?
                group by hour_ms, device_id
                """,
                (start_hour_ms, end_ms),
            ).fetchall()
            for row in rows:
                reset_count = self.con.execute(
                    """
                    select count(*) from system_events
                    where event_type = 'device_reset' and device_id = ? and at_ms >= ? and at_ms < ?
                    """,
                    (row["device_id"], row["hour_ms"], int(row["hour_ms"]) + 3600000),
                ).fetchone()[0]
                self.con.execute(
                    """
                    insert into hourly_device_metrics (
                        hour_ms, device_id, display_name, device_type, sample_count, fix_fixed_count,
                        fix_float_count, fix_no_count, ntrip_connected_count, ntrip_disconnected_count,
                        accuracy_min_m, accuracy_avg_m, accuracy_max_m, battery_min_percent,
                        battery_avg_percent, battery_max_percent, uptime_max_sec, reset_count, updated_ms
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(hour_ms, device_id) do update set
                        display_name = excluded.display_name,
                        device_type = excluded.device_type,
                        sample_count = excluded.sample_count,
                        fix_fixed_count = excluded.fix_fixed_count,
                        fix_float_count = excluded.fix_float_count,
                        fix_no_count = excluded.fix_no_count,
                        ntrip_connected_count = excluded.ntrip_connected_count,
                        ntrip_disconnected_count = excluded.ntrip_disconnected_count,
                        accuracy_min_m = excluded.accuracy_min_m,
                        accuracy_avg_m = excluded.accuracy_avg_m,
                        accuracy_max_m = excluded.accuracy_max_m,
                        battery_min_percent = excluded.battery_min_percent,
                        battery_avg_percent = excluded.battery_avg_percent,
                        battery_max_percent = excluded.battery_max_percent,
                        uptime_max_sec = excluded.uptime_max_sec,
                        reset_count = excluded.reset_count,
                        updated_ms = excluded.updated_ms
                    """,
                    (
                        row["hour_ms"],
                        row["device_id"],
                        row["display_name"],
                        row["device_type"],
                        row["sample_count"],
                        row["fix_fixed_count"],
                        row["fix_float_count"],
                        row["fix_no_count"],
                        row["ntrip_connected_count"],
                        row["ntrip_disconnected_count"],
                        row["accuracy_min_m"],
                        row["accuracy_avg_m"],
                        row["accuracy_max_m"],
                        row["battery_min_percent"],
                        row["battery_avg_percent"],
                        row["battery_max_percent"],
                        row["uptime_max_sec"],
                        reset_count,
                        updated_ms,
                    ),
                )

            pair_rows = self.con.execute(
                """
                select
                    (at_ms / 3600000) * 3600000 as hour_ms,
                    device_id,
                    peer_id,
                    at_ms,
                    safe_distance_m,
                    raw_distance_m,
                    uncertainty_m
                from telemetry_samples
                where at_ms >= ? and at_ms <= ? and peer_id is not null and peer_id != ''
                order by hour_ms, device_id, peer_id, at_ms
                """,
                (start_hour_ms, end_ms),
            ).fetchall()
            pairs: dict[tuple[int, str, str], dict[str, Any]] = {}
            for row in pair_rows:
                key = (int(row["hour_ms"]), str(row["device_id"]), str(row["peer_id"]))
                metric = pairs.setdefault(
                    key,
                    {
                        "sample_count": 0,
                        "closest_distance": None,
                        "raw_distance": None,
                        "uncertainty": None,
                        "at_ms": None,
                    },
                )
                metric["sample_count"] += 1
                candidate = row["safe_distance_m"] if row["safe_distance_m"] is not None else row["raw_distance_m"]
                if candidate is None:
                    continue
                if metric["closest_distance"] is None or candidate < metric["closest_distance"]:
                    metric["closest_distance"] = candidate
                    metric["raw_distance"] = row["raw_distance_m"]
                    metric["uncertainty"] = row["uncertainty_m"]
                    metric["at_ms"] = row["at_ms"]
            for (hour_ms, device_id, peer_id), metric in pairs.items():
                self.con.execute(
                    """
                    insert into hourly_pair_metrics (
                        hour_ms, device_id, peer_id, closest_safe_distance_m, closest_raw_distance_m,
                        closest_uncertainty_m, closest_at_ms, sample_count, updated_ms
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(hour_ms, device_id, peer_id) do update set
                        closest_safe_distance_m = excluded.closest_safe_distance_m,
                        closest_raw_distance_m = excluded.closest_raw_distance_m,
                        closest_uncertainty_m = excluded.closest_uncertainty_m,
                        closest_at_ms = excluded.closest_at_ms,
                        sample_count = excluded.sample_count,
                        updated_ms = excluded.updated_ms
                    """,
                    (
                        hour_ms,
                        device_id,
                        peer_id,
                        metric["closest_distance"],
                        metric["raw_distance"],
                        metric["uncertainty"],
                        metric["at_ms"],
                        metric["sample_count"],
                        updated_ms,
                    ),
                )
            self.con.commit()

    def summary(self, range_ms: int, device_id: str = "") -> dict[str, Any]:
        if not self.enabled or self.con is None:
            return {"enabled": False}
        end_ms = now_ms()
        start_ms = end_ms - range_ms
        self.rollup_recent(start_ms, end_ms)
        filters = "where h.hour_ms >= ? and h.hour_ms <= ?"
        params: list[Any] = [start_ms, end_ms]
        if device_id:
            filters += " and h.device_id = ?"
            params.append(device_id)
        with self._lock:
            connection_percentages = self._connection_percentages(start_ms, end_ms, device_id)
            connection_values = [value for value in connection_percentages.values() if value is not None]
            connection_percent = sum(connection_values) / len(connection_values) if connection_values else None
            row = self.con.execute(
                f"""
                select
                    coalesce(sum(sample_count), 0) as sample_count,
                    coalesce(sum(fix_fixed_count), 0) as fix_fixed_count,
                    coalesce(sum(fix_float_count), 0) as fix_float_count,
                    coalesce(sum(fix_no_count), 0) as fix_no_count,
                    coalesce(sum(ntrip_connected_count), 0) as ntrip_connected_count,
                    coalesce(sum(ntrip_disconnected_count), 0) as ntrip_disconnected_count,
                    min(accuracy_min_m) as accuracy_min_m,
                    avg(accuracy_avg_m) as accuracy_avg_m,
                    max(accuracy_max_m) as accuracy_max_m,
                    sum(reset_count) as reset_count,
                    max(uptime_max_sec) as uptime_max_sec
                from hourly_device_metrics h
                {filters}
                """,
                params,
            ).fetchone()
            closest = self.con.execute(
                f"""
                select p.* from hourly_pair_metrics p
                join hourly_device_metrics h on h.hour_ms = p.hour_ms and h.device_id = p.device_id
                {filters} and p.closest_safe_distance_m is not null
                order by p.closest_safe_distance_m asc limit 1
                """,
                params,
            ).fetchone()
            last_sample = self.con.execute(
                "select max(at_ms) from telemetry_samples where (? = '' or device_id = ?)",
                (device_id, device_id),
            ).fetchone()[0]
            device_rows = self.con.execute(
                """
                select device_id, display_name, device_type, last_seen_ms from devices
                where (? = '' or device_id = ?)
                order by display_name collate nocase
                """,
                (device_id, device_id),
            ).fetchall()
        return {
            "enabled": True,
            "databasePath": str(self.path),
            "range_ms": range_ms,
            "from_ms": start_ms,
            "to_ms": end_ms,
            "devices": [dict(item) for item in device_rows],
            "sample_count": int(row["sample_count"] or 0),
            "fix_fixed_count": int(row["fix_fixed_count"] or 0),
            "fix_float_count": int(row["fix_float_count"] or 0),
            "fix_no_count": int(row["fix_no_count"] or 0),
            "ntrip_connected_count": int(row["ntrip_connected_count"] or 0),
            "ntrip_disconnected_count": int(row["ntrip_disconnected_count"] or 0),
            "accuracy_min_m": row["accuracy_min_m"],
            "accuracy_avg_m": row["accuracy_avg_m"],
            "accuracy_max_m": row["accuracy_max_m"],
            "reset_count": int(row["reset_count"] or 0),
            "uptime_max_sec": row["uptime_max_sec"],
            "last_sample_ms": last_sample,
            "connection_percent": connection_percent,
            "closest": dict(closest) if closest is not None else None,
        }

    def hourly(self, from_ms: int, to_ms: int, device_id: str = "") -> dict[str, Any]:
        if not self.enabled or self.con is None:
            return {"enabled": False, "device_metrics": [], "pair_metrics": []}
        self.rollup_recent(from_ms, to_ms)
        params: list[Any] = [from_ms, to_ms]
        device_filter = ""
        if device_id:
            device_filter = " and device_id = ?"
            params.append(device_id)
        with self._lock:
            device_rows = self.con.execute(
                f"""
                select * from hourly_device_metrics
                where hour_ms >= ? and hour_ms <= ?{device_filter}
                order by hour_ms asc, display_name collate nocase
                """,
                params,
            ).fetchall()
            device_metric_rows = [dict(row) for row in device_rows]
            connection_percentages = self._connection_percentages(from_ms, to_ms, device_id)
            existing_keys = {(int(row["hour_ms"]), str(row["device_id"])) for row in device_metric_rows}
            for row in device_metric_rows:
                row["connection_percent"] = connection_percentages.get((int(row["hour_ms"]), str(row["device_id"])))

            device_infos = self.con.execute(
                """
                select device_id, display_name, device_type from devices
                where (? = '' or device_id = ?)
                """,
                (device_id, device_id),
            ).fetchall()
            device_info_by_id = {str(row["device_id"]): dict(row) for row in device_infos}
            for (hour_ms, connected_device_id), connection_percent in connection_percentages.items():
                if (hour_ms, connected_device_id) in existing_keys:
                    continue
                info = device_info_by_id.get(connected_device_id, {"device_id": connected_device_id})
                device_metric_rows.append(
                    {
                        "hour_ms": hour_ms,
                        "device_id": connected_device_id,
                        "display_name": info.get("display_name") or connected_device_id,
                        "device_type": info.get("device_type") or "device",
                        "sample_count": 0,
                        "fix_fixed_count": 0,
                        "fix_float_count": 0,
                        "fix_no_count": 0,
                        "ntrip_connected_count": 0,
                        "ntrip_disconnected_count": 0,
                        "accuracy_min_m": None,
                        "accuracy_avg_m": None,
                        "accuracy_max_m": None,
                        "battery_min_percent": None,
                        "battery_avg_percent": None,
                        "battery_max_percent": None,
                        "uptime_max_sec": None,
                        "reset_count": 0,
                        "updated_ms": now_ms(),
                        "connection_percent": connection_percent,
                    }
                )
            device_metric_rows.sort(key=lambda row: (int(row["hour_ms"]), str(row.get("display_name") or row["device_id"])))
            pair_rows = self.con.execute(
                f"""
                select * from hourly_pair_metrics
                where hour_ms >= ? and hour_ms <= ?{device_filter}
                order by hour_ms asc, device_id collate nocase, peer_id collate nocase
                """,
                params,
            ).fetchall()
        return {
            "enabled": True,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "device_metrics": device_metric_rows,
            "pair_metrics": [dict(row) for row in pair_rows],
        }

    def _connection_percentages(self, from_ms: int, to_ms: int, device_id: str = "") -> dict[tuple[int, str], float]:
        if not self.enabled or self.con is None or to_ms <= from_ms:
            return {}

        start_hour_ms = (from_ms // 3600000) * 3600000
        device_rows = self.con.execute(
            """
            select device_id from devices
            where (? = '' or device_id = ?)
            """,
            (device_id, device_id),
        ).fetchall()
        device_ids = [str(row["device_id"]) for row in device_rows]
        if not device_ids:
            return {}

        device_filter = ""
        params: list[Any] = [start_hour_ms, to_ms]
        event_params: list[Any] = [to_ms]
        if device_id:
            device_filter = " and device_id = ?"
            params.append(device_id)
            event_params.append(device_id)

        sample_rows = self.con.execute(
            f"""
            select (at_ms / 3600000) * 3600000 as hour_ms, device_id, count(*) as sample_count
            from telemetry_samples
            where at_ms >= ? and at_ms <= ?{device_filter}
            group by hour_ms, device_id
            """,
            params,
        ).fetchall()
        sample_counts = {(int(row["hour_ms"]), str(row["device_id"])): int(row["sample_count"]) for row in sample_rows}

        event_rows = self.con.execute(
            f"""
            select at_ms, device_id, event_type
            from system_events
            where at_ms <= ?
              and event_type in ('device_disconnected', 'device_reconnected')
              {device_filter}
            order by device_id collate nocase, at_ms asc
            """,
            event_params,
        ).fetchall()
        events_by_device: dict[str, list[dict[str, Any]]] = {}
        for row in event_rows:
            events_by_device.setdefault(str(row["device_id"]), []).append(dict(row))

        percentages: dict[tuple[int, str], float] = {}
        for connected_device_id in device_ids:
            device_events = events_by_device.get(connected_device_id, [])
            event_index = 0
            connected_state: bool | None = None
            for event in device_events:
                if int(event["at_ms"]) >= start_hour_ms:
                    break
                connected_state = event["event_type"] == "device_reconnected"
                event_index += 1

            hour_ms = start_hour_ms
            while hour_ms <= to_ms:
                hour_start = max(hour_ms, from_ms)
                hour_end = min(hour_ms + 3600000, to_ms)
                if hour_end <= hour_start:
                    hour_ms += 3600000
                    continue

                hour_events = []
                while event_index < len(device_events) and int(device_events[event_index]["at_ms"]) < hour_end:
                    event = device_events[event_index]
                    if int(event["at_ms"]) >= hour_start:
                        hour_events.append(event)
                    else:
                        connected_state = event["event_type"] == "device_reconnected"
                    event_index += 1

                sample_count = sample_counts.get((hour_ms, connected_device_id), 0)
                if connected_state is None and sample_count > 0:
                    connected_state = True
                elif connected_state is None and not hour_events:
                    hour_ms += 3600000
                    continue
                elif connected_state is None:
                    connected_state = True

                connected_ms = 0
                cursor_ms = hour_start
                state_for_hour = connected_state
                for event in hour_events:
                    event_ms = max(hour_start, min(hour_end, int(event["at_ms"])))
                    if state_for_hour:
                        connected_ms += max(0, event_ms - cursor_ms)
                    state_for_hour = event["event_type"] == "device_reconnected"
                    cursor_ms = event_ms
                if state_for_hour:
                    connected_ms += max(0, hour_end - cursor_ms)
                connected_state = state_for_hour

                if not hour_events and sample_count > 0 and connected_ms == 0:
                    connected_ms = hour_end - hour_start

                percentages[(hour_ms, connected_device_id)] = (connected_ms / max(1, hour_end - hour_start)) * 100
                hour_ms += 3600000

        return percentages

    def events(self, from_ms: int, to_ms: int, device_id: str = "", limit: int = 200) -> dict[str, Any]:
        if not self.enabled or self.con is None:
            return {"enabled": False, "events": []}
        limit = max(1, min(int(limit), 500))
        params: list[Any] = [from_ms, to_ms]
        device_filter = ""
        if device_id:
            device_filter = " and device_id = ?"
            params.append(device_id)
        params.append(limit)
        with self._lock:
            rows = self.con.execute(
                f"""
                select * from system_events
                where at_ms >= ? and at_ms <= ?{device_filter}
                order by at_ms desc
                limit ?
                """,
                params,
            ).fetchall()
        return {"enabled": True, "from_ms": from_ms, "to_ms": to_ms, "events": [dict(row) for row in rows]}

    def samples(self, from_ms: int, to_ms: int, device_id: str = "", limit: int = 500) -> dict[str, Any]:
        if not self.enabled or self.con is None:
            return {"enabled": False, "samples": []}
        limit = max(1, min(int(limit), 500))
        params: list[Any] = [from_ms, to_ms]
        device_filter = ""
        if device_id:
            device_filter = " and device_id = ?"
            params.append(device_id)
        params.append(limit)
        with self._lock:
            rows = self.con.execute(
                f"""
                select * from telemetry_samples
                where at_ms >= ? and at_ms <= ?{device_filter}
                order by at_ms desc
                limit ?
                """,
                params,
            ).fetchall()
        return {"enabled": True, "from_ms": from_ms, "to_ms": to_ms, "samples": [dict(row) for row in rows]}

    def statistics_snapshot(self, range_name: str, device_id: str = "") -> dict[str, Any]:
        """Build the statistics UI payload using the same queries as the HTTP APIs."""
        range_name = normalize_statistics_range(range_name)
        # Preserve the former browser behavior: live charts/events cover ten
        # minutes, while the summary endpoint historically defaulted to 24h.
        range_ms = 10 * 60 * 1000 if range_name == "live" else parse_range_ms(range_name)
        summary_range_ms = 24 * 60 * 60 * 1000 if range_name == "live" else range_ms
        end_ms = now_ms()
        start_ms = end_ms - range_ms
        return {
            "summary": self.summary(summary_range_ms, device_id),
            "hourly": self.hourly(start_ms, end_ms, device_id),
            "events": self.events(start_ms, end_ms, device_id, 80),
            "samples": self.samples(start_ms, end_ms, device_id, 500) if range_name == "live" else None,
        }

    def replay_range(self) -> dict[str, Any]:
        if not self.enabled or self.con is None:
            return {"enabled": False}
        with self._lock:
            row = self.con.execute(
                """
                select
                    count(*) as sample_count,
                    min(at_ms) as from_ms,
                    max(at_ms) as to_ms
                from telemetry_samples
                where latitude is not null and longitude is not null
                """
            ).fetchone()
            devices = self.con.execute(
                """
                select device_id, display_name, device_type, first_seen_ms, last_seen_ms
                from devices
                order by display_name collate nocase
                """
            ).fetchall()
        return {
            "enabled": True,
            "databasePath": str(self.path),
            "sample_count": int(row["sample_count"] or 0),
            "from_ms": row["from_ms"],
            "to_ms": row["to_ms"],
            "devices": [dict(item) for item in devices],
        }

    def replay_snapshot(self, at_ms: int, lookback_ms: int = 5 * 60 * 1000) -> dict[str, Any]:
        if not self.enabled or self.con is None:
            return {"enabled": False, "at_ms": at_ms, "devices": {}}

        lookback_ms = max(1000, min(int(lookback_ms), 24 * 60 * 60 * 1000))
        from_ms = at_ms - lookback_ms
        with self._lock:
            rows = self.con.execute(
                """
                select s.* from telemetry_samples s
                join (
                    select device_id, max(at_ms) as at_ms
                    from telemetry_samples
                    where at_ms <= ? and at_ms >= ?
                      and latitude is not null and longitude is not null
                    group by device_id
                ) latest on latest.device_id = s.device_id and latest.at_ms = s.at_ms
                order by s.display_name collate nocase, s.device_id collate nocase
                """,
                (at_ms, from_ms),
            ).fetchall()

        devices = {}
        for row in rows:
            payload = {}
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            payload["latitude"] = row["latitude"]
            payload["longitude"] = row["longitude"]
            payload["position"] = [row["longitude"], row["latitude"]]
            payload["fix_mode"] = row["fix_mode"]
            payload["ntrip_status"] = row["ntrip_status"]
            payload["nearest_peer_id"] = row["peer_id"] or payload.get("nearest_peer_id")
            payload["nearest_peer_safe_distance_m"] = row["safe_distance_m"]
            payload["nearest_peer_distance_m"] = row["raw_distance_m"]
            payload["nearest_peer_uncertainty_m"] = row["uncertainty_m"]
            payload["local_accuracy_m"] = row["accuracy_m"]
            payload["nearest_peer_accuracy_m"] = row["peer_accuracy_m"]
            payload["battery_percent"] = row["battery_percent"]
            payload["battery_voltage_v"] = row["battery_voltage_v"]
            payload["uptime_sec"] = row["uptime_sec"]
            payload["fix_mode_label"] = FIX_MODE_LABELS.get(row["fix_mode"], "UNKNOWN")
            payload["ntrip_status_label"] = NTRIP_STATUS_LABELS.get(row["ntrip_status"], "UNKNOWN")

            device_id = str(row["device_id"])
            devices[device_id] = {
                "device_id": device_id,
                "display_name": row["display_name"] or device_id,
                "telemetry": payload,
                "last_seen_ms": row["at_ms"],
                "last_telemetry_seen_ms": row["at_ms"],
                "last_position_seen_ms": row["at_ms"],
                "mqtt_client_id": row["mqtt_client_id"] or "",
                "source_host": row["source_host"] or "",
                "username": row["username"] or "",
                "info": {},
            }

        return {
            "enabled": True,
            "at_ms": at_ms,
            "lookback_ms": lookback_ms,
            "devices": devices,
        }


@dataclass
class StatisticsSubscription:
    range_name: str
    device_id: str
    refresh_interval_sec: float
    condition: threading.Condition = field(default_factory=threading.Condition)
    clients: set[int] = field(default_factory=set)
    cached_payload: dict[str, Any] | None = None
    version: int = 0
    generated_at_ms: int = 0
    refresh_in_flight: bool = False
    refresh_requested: bool = False
    producer_stopping: bool = False
    producer: threading.Thread | None = None
    last_manual_refresh_ms: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.range_name, self.device_id


class StatisticsSubscriptionRegistry:
    """Process-local shared statistics producers.

    If the application is deployed with multiple backend instances, replace this
    registry with Redis Pub/Sub or Streams, PostgreSQL LISTEN/NOTIFY, or NATS.
    """

    HEARTBEAT_SEC = 20.0
    MANUAL_REFRESH_COOLDOWN_MS = 1000

    def __init__(self, logger: TelemetryLogger):
        self.logger = logger
        self._lock = threading.RLock()
        self._groups: dict[tuple[str, str], StatisticsSubscription] = {}
        self._next_client_id = 0

    @staticmethod
    def refresh_interval(range_name: str) -> float:
        if range_name == "live":
            return 5.0
        if range_name == "24h":
            return 60.0
        return 5 * 60.0

    def subscribe(self, range_name: str, device_id: str) -> tuple[StatisticsSubscription, int]:
        range_name = normalize_statistics_range(range_name)
        device_id = normalize_statistics_device_id(device_id)
        key = (range_name, device_id)
        with self._lock:
            group = self._groups.get(key)
            if group is None:
                group = StatisticsSubscription(range_name, device_id, self.refresh_interval(range_name))
                self._groups[key] = group
            self._next_client_id += 1
            client_id = self._next_client_id
            with group.condition:
                group.clients.add(client_id)
                if group.producer is None or not group.producer.is_alive() or group.producer_stopping:
                    group.producer_stopping = False
                    group.producer = threading.Thread(
                        target=self._run_group,
                        args=(group,),
                        name=f"statistics-{range_name}-{device_id or 'all'}",
                        daemon=True,
                    )
                    group.producer.start()
        return group, client_id

    def unsubscribe(self, group: StatisticsSubscription, client_id: int) -> None:
        with group.condition:
            group.clients.discard(client_id)
            group.condition.notify_all()

    def wait_for_snapshot(
        self, group: StatisticsSubscription, version: int, timeout: float | None = None
    ) -> dict[str, Any] | None:
        timeout = self.HEARTBEAT_SEC if timeout is None else timeout
        with group.condition:
            if group.cached_payload is None or group.version <= version:
                group.condition.wait(timeout)
            if group.cached_payload is not None and group.version > version:
                return group.cached_payload
            return None

    def request_refresh(self, range_name: str, device_id: str) -> dict[str, Any]:
        key = (normalize_statistics_range(range_name), normalize_statistics_device_id(device_id))
        with self._lock:
            group = self._groups.get(key)
        if group is None:
            return {"accepted": False, "status": "no_active_subscription"}

        current_ms = now_ms()
        with group.condition:
            if not group.clients:
                return {"accepted": False, "status": "no_active_subscription"}
            if current_ms - group.last_manual_refresh_ms < self.MANUAL_REFRESH_COOLDOWN_MS:
                return {"accepted": False, "status": "cooldown", "version": group.version}
            group.last_manual_refresh_ms = current_ms
            group.refresh_requested = True
            group.condition.notify_all()
            return {"accepted": True, "status": "refresh_requested", "version": group.version}

    def _run_group(self, group: StatisticsSubscription) -> None:
        try:
            while True:
                with group.condition:
                    if not group.clients:
                        group.producer_stopping = True
                        return
                    group.refresh_requested = False
                    group.refresh_in_flight = True

                try:
                    statistics = self.logger.statistics_snapshot(group.range_name, group.device_id)
                    generated_at_ms = now_ms()
                    with group.condition:
                        group.version += 1
                        group.generated_at_ms = generated_at_ms
                        group.cached_payload = {
                            "subscription": {
                                "range": group.range_name,
                                "device_id": group.device_id,
                            },
                            "version": group.version,
                            "generated_at_ms": generated_at_ms,
                            "statistics": statistics,
                        }
                        group.condition.notify_all()
                except Exception as exc:
                    logging.warning(
                        "Statistics refresh failed range=%s device_id=%s: %s",
                        group.range_name,
                        group.device_id or "all",
                        exc,
                    )
                finally:
                    with group.condition:
                        group.refresh_in_flight = False
                        group.condition.notify_all()

                deadline = time.monotonic() + group.refresh_interval_sec
                with group.condition:
                    while group.clients and not group.refresh_requested:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        group.condition.wait(remaining)
                    if not group.clients:
                        group.producer_stopping = True
                        return
        finally:
            with self._lock:
                with group.condition:
                    if not group.clients and self._groups.get(group.key) is group:
                        self._groups.pop(group.key, None)


class DashboardState:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._version = 0
        self._devices: dict[str, DeviceRecord] = {}
        self._peers: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._device_connection_state: dict[str, bool] = {}
        self.started_ms = now_ms()
        self.mbtiles = discover_mbtiles()
        self.telemetry_logger = TelemetryLogger(config)
        self.statistics_registry = StatisticsSubscriptionRegistry(self.telemetry_logger)
        self.telemetry_logger.start()
        self.start_connection_monitor()

    def start_connection_monitor(self) -> None:
        thread = threading.Thread(target=self._monitor_device_connections, name="device-connection-monitor", daemon=True)
        thread.start()

    def _monitor_device_connections(self) -> None:
        while True:
            time.sleep(1)
            current_ms = now_ms()
            disconnected: list[tuple[str, str, int]] = []
            with self._condition:
                for device_id, record in self._devices.items():
                    last_telemetry_ms = int(record.last_telemetry_seen_ms or record.last_position_seen_ms or 0)
                    if not last_telemetry_ms:
                        continue
                    is_connected = current_ms - last_telemetry_ms <= 5000
                    was_connected = self._device_connection_state.get(device_id, True)
                    if was_connected and not is_connected:
                        self._device_connection_state[device_id] = False
                        disconnected.append((device_id, record.display_name or device_id, last_telemetry_ms))

            for device_id, display_name, last_telemetry_ms in disconnected:
                self.telemetry_logger.log_event(
                    "device_disconnected",
                    f"{display_name} telemetry disconnected",
                    {"last_telemetry_seen_ms": last_telemetry_ms},
                    device_id=device_id,
                    severity="warn",
                )

    def configured_rover_name(self, *keys: str) -> str:
        names = self.config.get("dashboard", {}).get("roverNames", {})
        if not isinstance(names, dict):
            return ""
        for key in keys:
            if not key:
                continue
            value = names.get(str(key))
            if value is None:
                continue
            name = str(value).strip()
            if name and not is_ip_address(name):
                return name
        return ""

    def publish_event(self, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
        event = {
            "type": event_type,
            "message": message,
            "data": data or {},
            "at_ms": now_ms(),
        }
        with self._condition:
            self._events.append(event)
            self._events = self._events[-80:]
            self._version += 1
            self._condition.notify_all()
        self.telemetry_logger.log_event(event_type, message, data)

    def update_from_mqtt(
        self,
        *,
        topic: str,
        raw_payload: bytes,
        client_id: str,
        username: str,
        source_host: str,
    ) -> None:
        decoded = decode_json_or_value(raw_payload)
        payload: dict[str, Any]
        info_payload: dict[str, Any] | None = None

        if isinstance(decoded, dict):
            payload = dict(decoded)
        else:
            payload = {}

        if topic == "info/mcu" and isinstance(decoded, dict):
            info_payload = decoded
        elif topic.startswith("ds/"):
            payload = {topic.split("/", 1)[1]: decoded}
        elif topic.startswith("batch_ds/") and isinstance(decoded, dict):
            payload = dict(decoded)
        elif topic != "batch_ds" and not isinstance(decoded, dict):
            payload = {topic.replace("/", "_"): decoded}

        explicit_id = (
            payload.get("device_id")
            or payload.get("deviceId")
            or (topic.split("/", 1)[1] if topic.startswith("batch_ds/") and "/" in topic else None)
        )
        if explicit_id:
            device_id = str(explicit_id)
        elif client_id and not client_id.startswith("dashboard-auto-"):
            device_id = client_id
        elif username and username != "device":
            device_id = username
        else:
            device_id = source_host

        display_name = (
            extract_rover_name(info_payload)
            or extract_rover_name(payload)
            or self.configured_rover_name(device_id, client_id, username, source_host)
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
                normalize_position(payload)
                if has_valid_position(payload):
                    record.last_position_seen_ms = seen_ms
                record.last_telemetry_seen_ms = seen_ms
                record.telemetry.update(payload)
                record.telemetry["fix_mode_label"] = FIX_MODE_LABELS.get(record.telemetry.get("fix_mode"), "UNKNOWN")
                record.telemetry["ntrip_status_label"] = NTRIP_STATUS_LABELS.get(
                    record.telemetry.get("ntrip_status"), "UNKNOWN"
                )

            record.last_seen_ms = seen_ms
            record.mqtt_client_id = client_id
            record.source_host = source_host
            record.username = username
            self._version += 1
            self._condition.notify_all()

            logged_display_name = record.display_name or device_id
            was_disconnected = (
                info_payload is None
                and bool(payload)
                and self._device_connection_state.get(device_id) is False
            )
            if info_payload is None and bool(payload):
                self._device_connection_state[device_id] = True

        if topic in {"batch_ds", "info/mcu"} or topic.startswith(("batch_ds/", "ds/")):
            logging.debug("MQTT %s from %s: %s", topic, device_id, decoded)

        if info_payload is None:
            if was_disconnected:
                self.telemetry_logger.log_event(
                    "device_reconnected",
                    f"{logged_display_name} telemetry reconnected",
                    {"topic": topic, "source_host": source_host},
                    device_id=device_id,
                )
            self.telemetry_logger.log_sample(
                device_id=device_id,
                display_name=logged_display_name,
                payload=payload,
                source_type="mqtt",
                topic=topic,
                source_host=source_host,
                mqtt_client_id=client_id,
                username=username,
            )

    def update_from_peer_udp(self, payload: dict[str, Any], source_host: str, max_age_sec: float) -> None:
        if payload.get("schema") != "crane-rover-peer-v1":
            return
        device_id = str(payload.get("device_id") or "")
        if not device_id:
            return

        peer = dict(payload)
        peer["source_host"] = source_host
        peer["last_seen_ms"] = now_ms()
        peer["max_age_sec"] = max_age_sec
        peer["display_name"] = (
            extract_rover_name(peer)
            or self.configured_rover_name(device_id, source_host)
            or device_id
        )
        normalize_position(peer)
        peer["last_position_seen_ms"] = peer["last_seen_ms"] if has_valid_position(peer) else 0

        with self._condition:
            self._peers[device_id] = peer
            self._version += 1
            self._condition.notify_all()

        self.telemetry_logger.log_sample(
            device_id=device_id,
            display_name=str(peer.get("display_name") or device_id),
            payload=peer,
            source_type="udp_peer",
            source_host=source_host,
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
                    "started_ms": self.started_ms,
                    "now_ms": current_ms,
                    "mqtt": self.config["mqtt"],
                    "http": self.config["http"],
                    "udpPeers": self.config["udpPeers"],
                    "dashboard": self.config["dashboard"],
                    "mbtiles": self.mbtiles,
                },
                "devices": {key: record.to_dict() for key, record in self._devices.items()},
                "peers": peers,
                "events": list(self._events),
            }

    def wait_for_update(self, version: int, timeout: float = 25.0) -> tuple[bool, dict[str, Any]]:
        with self._condition:
            if self._version <= version:
                self._condition.wait(timeout)
            snapshot = self.snapshot()
            changed = int(snapshot["version"]) > version
            return changed, snapshot


@dataclass
class MqttClientContext:
    client_id: str = ""
    username: str = ""
    source_host: str = ""


class MinimalMqttBroker:
    def __init__(self, state: DashboardState, host: str, port: int):
        self.state = state
        self.host = host
        self.port = int(port)
        self._server: ThreadedTcpServer | None = None
        self._counter = 0
        self._counter_lock = threading.Lock()

    def next_client_id(self) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"dashboard-auto-{self._counter}"

    def start(self) -> None:
        broker = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                handle_mqtt_client(self.request, self.client_address, broker)

        self._server = ThreadedTcpServer((self.host, self.port), Handler)
        thread = threading.Thread(target=self._server.serve_forever, name="mqtt-broker", daemon=True)
        thread.start()
        logging.info("MQTT listening on %s:%s", self.host, self.port)


class ThreadedTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def read_remaining_length(sock: socket.socket) -> int:
    multiplier = 1
    value = 0
    while True:
        encoded = read_exact(sock, 1)[0]
        value += (encoded & 127) * multiplier
        if (encoded & 128) == 0:
            break
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise ValueError("malformed MQTT remaining length")
    return value


def encode_remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 128
        encoded.append(digit)
        if length == 0:
            break
    return bytes(encoded)


def mqtt_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise ValueError("malformed MQTT string")
    size = struct.unpack("!H", data[offset : offset + 2])[0]
    offset += 2
    if offset + size > len(data):
        raise ValueError("malformed MQTT string payload")
    return data[offset : offset + size].decode("utf-8", errors="replace"), offset + size


def send_packet(sock: socket.socket, packet_type: int, flags: int, payload: bytes) -> None:
    sock.sendall(bytes([(packet_type << 4) | flags]) + encode_remaining_length(len(payload)) + payload)


def handle_connect(payload: bytes, broker: MinimalMqttBroker, ctx: MqttClientContext, sock: socket.socket) -> None:
    protocol_name, offset = mqtt_string(payload, 0)
    if protocol_name not in {"MQTT", "MQIsdp"}:
        raise ValueError(f"unsupported MQTT protocol {protocol_name!r}")

    protocol_level = payload[offset]
    offset += 1
    connect_flags = payload[offset]
    offset += 1
    keepalive = struct.unpack("!H", payload[offset : offset + 2])[0]
    offset += 2
    del protocol_level, keepalive

    client_id, offset = mqtt_string(payload, offset)
    has_username = bool(connect_flags & 0x80)
    has_password = bool(connect_flags & 0x40)
    has_will = bool(connect_flags & 0x04)
    username = ""

    if has_will:
        _, offset = mqtt_string(payload, offset)
        _, offset = mqtt_string(payload, offset)
    if has_username:
        username, offset = mqtt_string(payload, offset)
    if has_password:
        _, offset = mqtt_string(payload, offset)

    ctx.client_id = client_id or broker.next_client_id()
    ctx.username = username
    sock.sendall(b"\x20\x02\x00\x00")
    logging.info("MQTT connected client_id=%s username=%s from=%s", ctx.client_id, username or "-", ctx.source_host)


def handle_publish(packet_flags: int, payload: bytes, broker: MinimalMqttBroker, ctx: MqttClientContext, sock: socket.socket):
    topic, offset = mqtt_string(payload, 0)
    qos = (packet_flags >> 1) & 0x03
    packet_id = None
    if qos:
        packet_id = struct.unpack("!H", payload[offset : offset + 2])[0]
        offset += 2
    message_payload = payload[offset:]

    broker.state.update_from_mqtt(
        topic=topic,
        raw_payload=message_payload,
        client_id=ctx.client_id,
        username=ctx.username,
        source_host=ctx.source_host,
    )

    if qos == 1 and packet_id is not None:
        send_packet(sock, 4, 0, struct.pack("!H", packet_id))


def handle_subscribe(payload: bytes, sock: socket.socket) -> None:
    if len(payload) < 3:
        raise ValueError("malformed MQTT subscribe")
    packet_id = struct.unpack("!H", payload[:2])[0]
    offset = 2
    granted = []
    while offset < len(payload):
        _, offset = mqtt_string(payload, offset)
        if offset >= len(payload):
            raise ValueError("malformed MQTT subscription qos")
        requested_qos = payload[offset]
        offset += 1
        granted.append(min(requested_qos, 1))
    send_packet(sock, 9, 0, struct.pack("!H", packet_id) + bytes(granted or [0]))


def handle_mqtt_client(sock: socket.socket, client_address: tuple[str, int], broker: MinimalMqttBroker) -> None:
    ctx = MqttClientContext(source_host=client_address[0])
    sock.settimeout(90)
    try:
        while True:
            first = sock.recv(1)
            if not first:
                return
            fixed_header = first[0]
            packet_type = fixed_header >> 4
            flags = fixed_header & 0x0F
            remaining = read_remaining_length(sock)
            payload = read_exact(sock, remaining) if remaining else b""

            if packet_type == 1:
                handle_connect(payload, broker, ctx, sock)
            elif packet_type == 3:
                handle_publish(flags, payload, broker, ctx, sock)
            elif packet_type == 8:
                handle_subscribe(payload, sock)
            elif packet_type == 12:
                sock.sendall(b"\xD0\x00")
            elif packet_type == 14:
                return
            else:
                logging.debug("Ignoring unsupported MQTT packet type %s from %s", packet_type, ctx.source_host)
    except Exception as exc:
        logging.debug("MQTT client closed from %s: %s", ctx.source_host, exc)
        broker.state.telemetry_logger.log_event(
            "mqtt_client_closed",
            f"MQTT client closed from {ctx.source_host}",
            {"error": str(exc), "client_id": ctx.client_id},
            severity="debug",
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass


class PeerUdpListener:
    def __init__(self, state: DashboardState, host: str, port: int, max_age_sec: float):
        self.state = state
        self.host = host
        self.port = int(port)
        self.max_age_sec = float(max_age_sec)

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="peer-udp-listener", daemon=True)
        thread.start()
        logging.info("UDP peer listener on %s:%s", self.host, self.port)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            logging.warning("UDP peer listener disabled: %s", exc)
            return

        while True:
            try:
                data, addr = sock.recvfrom(8192)
                payload = json.loads(data.decode("utf-8"))
                self.state.update_from_peer_udp(payload, addr[0], self.max_age_sec)
            except Exception as exc:
                logging.debug("UDP peer packet ignored: %s", exc)


class DashboardHttpHandler(BaseHTTPRequestHandler):
    server_version = "RTKDashboard/0.1"

    @property
    def dashboard_state(self) -> DashboardState:
        return self.server.dashboard_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(self.dashboard_state.snapshot())
            return
        if parsed.path.startswith("/api/logs/"):
            self.handle_logs_api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/replay/"):
            self.handle_replay_api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path == "/api/statistics/stream":
            self.handle_statistics_stream(parse_qs(parsed.query))
            return
        if parsed.path == "/events":
            self.handle_events()
            return
        if parsed.path.startswith("/tiles/"):
            self.serve_mbtiles_tile(parsed.path)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/statistics/refresh":
            query = parse_qs(parsed.query)
            result = self.dashboard_state.statistics_registry.request_refresh(
                first_query_value(query, "range"), first_query_value(query, "device_id")
            )
            self.send_json(result, HTTPStatus.ACCEPTED if result["accepted"] else HTTPStatus.OK)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_logs_api(self, path: str, query: dict[str, list[str]]) -> None:
        logger = self.dashboard_state.telemetry_logger
        range_ms = parse_range_ms(first_query_value(query, "range"), 24 * 60 * 60 * 1000)
        end_ms = parse_query_ms(first_query_value(query, "to"), now_ms())
        start_ms = parse_query_ms(first_query_value(query, "from"), end_ms - range_ms)
        device_id = first_query_value(query, "device_id") or ""
        limit = int(finite_int(first_query_value(query, "limit")) or 200)

        if path == "/api/logs/summary":
            self.send_json(logger.summary(range_ms, device_id))
            return
        if path == "/api/logs/hourly":
            self.send_json(logger.hourly(start_ms, end_ms, device_id))
            return
        if path == "/api/logs/events":
            self.send_json(logger.events(start_ms, end_ms, device_id, limit))
            return
        if path == "/api/logs/samples":
            self.send_json(logger.samples(start_ms, end_ms, device_id, limit))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_replay_api(self, path: str, query: dict[str, list[str]]) -> None:
        logger = self.dashboard_state.telemetry_logger
        if path == "/api/replay/range":
            self.send_json(logger.replay_range())
            return
        if path == "/api/replay/state":
            at_ms = parse_query_ms(first_query_value(query, "at"), now_ms())
            lookback_ms = parse_query_ms(first_query_value(query, "lookback"), 5 * 60 * 1000)
            replay = logger.replay_snapshot(at_ms, lookback_ms)
            live = self.dashboard_state.snapshot()
            self.send_json(
                {
                    "version": -at_ms,
                    "server": {
                        **live["server"],
                        "now_ms": at_ms,
                        "replay": True,
                        "requested_at_ms": at_ms,
                        "lookback_ms": replay.get("lookback_ms", lookback_ms),
                    },
                    "devices": replay.get("devices", {}),
                    "peers": {},
                    "events": [],
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def handle_statistics_stream(self, query: dict[str, list[str]]) -> None:
        registry = self.dashboard_state.statistics_registry
        group, client_id = registry.subscribe(
            first_query_value(query, "range"), first_query_value(query, "device_id")
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.connection.settimeout(30)

        version = -1
        try:
            while True:
                payload = registry.wait_for_snapshot(group, version)
                if payload is None:
                    self.wfile.write(b": heartbeat\n\n")
                else:
                    version = int(payload["version"])
                    encoded = json.dumps(payload, separators=(",", ":"))
                    self.wfile.write(f"event: snapshot\ndata: {encoded}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
        finally:
            registry.unsubscribe(group, client_id)

    def handle_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        version = -1
        try:
            while True:
                changed, snapshot = self.dashboard_state.wait_for_update(version)
                if not changed:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue

                version = int(snapshot["version"])
                encoded = json.dumps(snapshot, separators=(",", ":"))
                self.wfile.write(f"event: state\ndata: {encoded}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def serve_mbtiles_tile(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[0] != "tiles":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        tileset_id = unquote(parts[1])
        y_name = parts[4]
        y_text, _, extension = y_name.partition(".")
        try:
            zoom = int(parts[2])
            tile_column = int(parts[3])
            tile_y = int(y_text)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if zoom < 0 or tile_column < 0 or tile_y < 0 or tile_column >= 2**zoom or tile_y >= 2**zoom:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        tile_path = (MBTILES_ROOT / f"{tileset_id}.mbtiles").resolve()
        try:
            tile_path.relative_to(MBTILES_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not tile_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        tile_row = (2**zoom - 1) - tile_y
        try:
            with open_mbtiles(tile_path) as con:
                row = con.execute(
                    "select tile_data from tiles where zoom_level = ? and tile_column = ? and tile_row = ?",
                    (zoom, tile_column, tile_row),
                ).fetchone()
        except sqlite3.Error as exc:
            logging.warning("MBTiles tile read failed for %s: %s", tile_path.name, exc)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        data = bytes(row[0])
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", tile_content_type(extension))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"

        relative = unquote(path).lstrip("/")
        requested = (STATIC_ROOT / relative).resolve()
        if not str(requested).startswith(str(STATIC_ROOT.resolve())) or not requested.exists() or requested.is_dir():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = "application/octet-stream"
        if requested.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif requested.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif requested.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif requested.suffix == ".svg":
            content_type = "image/svg+xml"

        data = requested.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


class DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], state: DashboardState):
        super().__init__(server_address, handler_class)
        self.dashboard_state = state


def start_http(state: DashboardState, host: str, port: int) -> DashboardHttpServer:
    server = DashboardHttpServer((host, int(port)), DashboardHttpHandler, state)
    thread = threading.Thread(target=server.serve_forever, name="http-dashboard", daemon=True)
    thread.start()
    logging.info("HTTP dashboard on http://%s:%s", host, port)
    return server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Blynk-compatible MQTT dashboard for crane-rover")
    parser.add_argument("--config", default="config.yaml", help="config YAML path")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config(Path(args.config))
    state = DashboardState(config)
    state.publish_event("server", "Dashboard started")

    mqtt_cfg = config["mqtt"]
    broker = MinimalMqttBroker(state, str(mqtt_cfg["host"]), int(mqtt_cfg["port"]))
    broker.start()

    udp_cfg = config["udpPeers"]
    if bool(udp_cfg.get("enabled", True)):
        PeerUdpListener(
            state,
            str(udp_cfg.get("host", "0.0.0.0")),
            int(udp_cfg.get("port", 5005)),
            float(udp_cfg.get("maxAgeSec", 5)),
        ).start()

    http_cfg = config["http"]
    start_http(state, str(http_cfg["host"]), int(http_cfg["port"]))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logging.info("Stopping dashboard")


if __name__ == "__main__":
    main()
