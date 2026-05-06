from __future__ import annotations

import json
import socket
import struct
import time


HOST = "127.0.0.1"
PORT = 1883
CLIENT_ID = "sample-rover"
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
            break
    return bytes(encoded)


def packet(packet_type: int, flags: int, payload: bytes) -> bytes:
    return bytes([(packet_type << 4) | flags]) + remaining_length(len(payload)) + payload


def connect(sock: socket.socket) -> None:
    variable_header = mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
    payload = mqtt_string(CLIENT_ID) + mqtt_string(USERNAME) + mqtt_string(PASSWORD)
    sock.sendall(packet(1, 0, variable_header + payload))
    response = sock.recv(4)
    if response != b"\x20\x02\x00\x00":
        raise RuntimeError(f"MQTT connect failed: {response!r}")


def publish(sock: socket.socket, topic: str, payload_obj: dict) -> None:
    payload = mqtt_string(topic) + json.dumps(payload_obj).encode("utf-8")
    sock.sendall(packet(3, 0, payload))


def main() -> None:
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        connect(sock)
        base_lat = -6.2088
        base_lon = 106.8456
        for index in range(6):
            lat = base_lat + index * 0.00002
            lon = base_lon + index * 0.00003
            publish(
                sock,
                "batch_ds",
                {
                    "device_id": CLIENT_ID,
                    "latitude": lat,
                    "longitude": lon,
                    "altitude_m": 12.4,
                    "position": [lon, lat],
                    "satellites": 22,
                    "hdop": 0.78,
                    "rtcm_age_sec": 0.6,
                    "fix_mode": 4,
                    "ntrip_status": 1,
                    "battery_percent": 84.2,
                    "battery_voltage_v": 4.081,
                    "battery_current_a": -0.43,
                    "battery_power_w": -1.75,
                    "battery_status": "DISCHARGING",
                    "battery_present": 1,
                    "local_accuracy_m": 0.02,
                    "nearest_peer_distance_m": 18.442,
                    "nearest_peer_safe_distance_m": 18.414,
                    "nearest_peer_uncertainty_m": 0.028,
                    "nearest_peer_combined_accuracy_m": 0.028,
                    "nearest_peer_accuracy_m": 0.02,
                    "nearest_peer_fix_mode": 4,
                    "nearest_peer_id": "peer-02",
                },
            )
            time.sleep(1)
    print("sample telemetry published")


if __name__ == "__main__":
    main()
