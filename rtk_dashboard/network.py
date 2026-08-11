"""Network addresses safe to expose through the public dashboard state."""

from __future__ import annotations

import ipaddress
import socket
import struct
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux deployment provides fcntl
    fcntl = None

WILDCARD_HOSTS = {"", "0.0.0.0", "::", "[::]"}


def discover_lan_ip() -> str:
    """Best-effort address for the interface carrying the default route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # UDP connect selects a route without sending application data.
            probe.connect(("192.0.2.1", 9))
            candidate = str(probe.getsockname()[0])
            if candidate and not ipaddress.ip_address(candidate).is_loopback:
                return candidate
    except OSError:
        pass

    interface_candidates: list[str] = []
    if fcntl is not None:
        try:
            interfaces = socket.if_nameindex()
        except OSError:
            interfaces = []
        for _, interface_name in interfaces:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    request = struct.pack("256s", interface_name.encode("utf-8")[:15])
                    response = fcntl.ioctl(probe.fileno(), 0x8915, request)  # Linux SIOCGIFADDR
                    interface_candidates.append(socket.inet_ntoa(response[20:24]))
            except OSError:
                continue
    for candidate in interface_candidates:
        address = ipaddress.ip_address(candidate)
        if address.is_private and not address.is_loopback:
            return candidate
    for candidate in interface_candidates:
        if not ipaddress.ip_address(candidate).is_loopback:
            return candidate

    try:
        candidates = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        candidates = []
    for candidate in candidates:
        try:
            if not ipaddress.ip_address(candidate).is_loopback:
                return candidate
        except ValueError:
            continue
    return "127.0.0.1"


def advertised_mqtt_host(mqtt_config: dict[str, Any]) -> str:
    configured = str(mqtt_config.get("advertisedHost") or "auto").strip()
    if configured.lower() != "auto" and configured not in WILDCARD_HOSTS:
        return configured
    bind_host = str(mqtt_config.get("host") or "").strip()
    return bind_host if bind_host not in WILDCARD_HOSTS else discover_lan_ip()


def public_mqtt_config(mqtt_config: dict[str, Any]) -> dict[str, Any]:
    """Return connection metadata used by the browser header."""
    advertised_setting = str(mqtt_config.get("advertisedHost") or "auto").strip()
    return {
        "host": mqtt_config.get("host", "0.0.0.0"),
        "port": int(mqtt_config.get("port", 1883)),
        "advertisedHost": advertised_mqtt_host(mqtt_config),
        "advertisedHostSource": "auto" if advertised_setting.lower() == "auto" else "explicit",
    }
