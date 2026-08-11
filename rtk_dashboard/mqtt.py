"""Minimal MQTT 3.1/3.1.1 transport used for local device ingestion."""
from __future__ import annotations

import logging
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass

from .state import DashboardState

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
