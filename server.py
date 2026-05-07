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
from urllib.parse import quote, unquote, urlparse

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
        "dashboard": {
            "title": "Crane Rover Dashboard",
            "defaultCenter": {"latitude": -2.5489, "longitude": 118.0149, "zoom": 5},
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
            "mqtt_client_id": self.mqtt_client_id,
            "source_host": self.source_host,
            "username": self.username,
            "info": self.info,
        }


class DashboardState:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._version = 0
        self._devices: dict[str, DeviceRecord] = {}
        self._peers: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self.started_ms = now_ms()
        self.mbtiles = discover_mbtiles()

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
                record.telemetry.update(payload)
                record.telemetry["fix_mode_label"] = FIX_MODE_LABELS.get(record.telemetry.get("fix_mode"), "UNKNOWN")
                record.telemetry["ntrip_status_label"] = NTRIP_STATUS_LABELS.get(
                    record.telemetry.get("ntrip_status"), "UNKNOWN"
                )

            record.last_seen_ms = now_ms()
            record.mqtt_client_id = client_id
            record.source_host = source_host
            record.username = username
            self._version += 1
            self._condition.notify_all()

        if topic in {"batch_ds", "info/mcu"} or topic.startswith(("batch_ds/", "ds/")):
            logging.debug("MQTT %s from %s: %s", topic, device_id, decoded)

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

        with self._condition:
            self._peers[device_id] = peer
            self._version += 1
            self._condition.notify_all()

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

    def wait_for_update(self, version: int, timeout: float = 25.0) -> dict[str, Any]:
        with self._condition:
            if self._version <= version:
                self._condition.wait(timeout)
            return self.snapshot()


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
        if parsed.path == "/events":
            self.handle_events()
            return
        if parsed.path.startswith("/tiles/"):
            self.serve_mbtiles_tile(parsed.path)
            return
        self.serve_static(parsed.path)

    def send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def handle_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        version = -1
        try:
            while True:
                snapshot = self.dashboard_state.wait_for_update(version)
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
