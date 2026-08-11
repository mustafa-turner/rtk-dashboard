from __future__ import annotations

import argparse
import json
import socket
import struct
import time
from typing import Any


USERNAME = "device"
PASSWORD = "local-dashboard"


def mqtt_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


def remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 128
        encoded.append(digit)
        if not length:
            return bytes(encoded)


def packet(packet_type: int, flags: int, payload: bytes) -> bytes:
    return bytes([(packet_type << 4) | flags]) + remaining_length(len(payload)) + payload


def connect(sock: socket.socket, client_id: str) -> None:
    variable_header = mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
    payload = mqtt_string(client_id) + mqtt_string(USERNAME) + mqtt_string(PASSWORD)
    sock.sendall(packet(1, 0, variable_header + payload))
    response = sock.recv(4)
    if response != b"\x20\x02\x00\x00":
        raise RuntimeError(f"MQTT connect failed: {response!r}")


def publish(sock: socket.socket, topic: str, payload: dict[str, Any]) -> None:
    body = mqtt_string(topic) + json.dumps(payload).encode("utf-8")
    sock.sendall(packet(3, 0, body))


def sample_messages(index: int) -> dict[str, tuple[str, dict[str, Any]]]:
    latitude = -6.2088 + index * 0.00002
    longitude = 106.8456 + index * 0.00003
    return {
        "rover": (
            "batch_ds",
            {
                "device_id": "sample-rover",
                "latitude": latitude,
                "longitude": longitude,
                "satellites": 22,
                "hdop": 0.78,
                "rtcm_age_sec": 0.6,
                "fix_mode": 4,
                "ntrip_status": 1,
                "battery_percent": 84.2,
                "local_accuracy_m": 0.02,
                "nearest_peer_distance_m": 18.442,
                "nearest_peer_safe_distance_m": 18.414,
                "nearest_peer_uncertainty_m": 0.028,
                "nearest_peer_accuracy_m": 0.02,
                "nearest_peer_fix_mode": 4,
                "nearest_peer_id": "peer-02",
            },
        ),
        "weather": (
            "telemetry/weather_station/weather-01",
            {
                "temperature": 29.5 + index * 0.1,
                "humidity": 71 - index * 0.2,
                "wind_speed": 3.4,
                "rainfall_mm": 1.2,
                "lat": latitude + 0.001,
                "lon": longitude,
                "battery_pct": 92,
            },
        ),
        "tide": (
            "telemetry/tide_sensor/tide-01",
            {
                "schema": "tide-logger-v1",
                "device_id": "tide-01",
                "device_type": "tide_sensor",
                "measured_at_ms": int(time.time() * 1000),
                "sequence": index + 1,
                "water_level_m": 1.8 + index * 0.01,
                "distance_to_water_mm": 1200 - index * 10,
                "sensor_height_m": 3.0,
                "battery_voltage_v": 4.012,
                "solar_voltage_v": 18.4,
                "system_voltage_v": 5.016,
                "system_current_a": 0.35,
                "temperature_c": 28.5,
                "humidity_percent": 76.2,
                "measurement_quality": 2,
                "samples_acquired": 50,
                "samples_used": 44,
                "mad_outliers": 2,
                "distance_mad_mm": 4.0,
                "acquisition_duration_ms": 5100,
                "pending_records": 0,
                "dropped_records": 0,
                "latitude": latitude,
                "longitude": longitude + 0.001,
                "firmware_version": "sample",
                "wifi_rssi_dbm": -61,
            },
        ),
        "truck": (
            "telemetry/truck/truck-07",
            {
                "speed": 18 + index,
                "heading": 145,
                "lat": latitude + 0.001,
                "lng": longitude + 0.001,
                "battery_percent": 68,
            },
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish sample IoT telemetry to the local dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--type", choices=("rover", "weather", "tide", "truck", "all"), default="rover")
    parser.add_argument("--count", type=int, default=6)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected_types = ("rover", "weather", "tide", "truck") if args.type == "all" else (args.type,)
    with socket.create_connection((args.host, args.port), timeout=5) as sock:
        connect(sock, "sample-publisher")
        for index in range(max(1, args.count)):
            messages = sample_messages(index)
            for device_type in selected_types:
                topic, payload = messages[device_type]
                publish(sock, topic, payload)
            time.sleep(1)
    print(f"sample telemetry published for: {', '.join(selected_types)}")


if __name__ == "__main__":
    main()
