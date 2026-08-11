"""Configuration loading and defaults."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on minimal installs
    yaml = None


DEFAULT_CONFIG: dict[str, Any] = {
    "mqtt": {"host": "0.0.0.0", "port": 1883, "advertisedHost": "auto"},
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
        "title": "IoT Device Dashboard",
        "defaultCenter": {"latitude": -2.5489, "longitude": 118.0149, "zoom": 5},
        "roverAntennaOffset": {"x": 0, "y": 0},
        "deviceNames": {},
    },
    "devices": {"types": {}},
}


def parse_simple_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [parse_simple_scalar(item) for item in inner.split(",")] if inner else []
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if lower in {"null", "none"}:
        return None
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, value = (part.strip() for part in line.strip().split(":", 1))
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


def _merge(defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _merge(DEFAULT_CONFIG, {})
    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text) if yaml is not None else parse_simple_yaml(text)
    return _merge(DEFAULT_CONFIG, loaded or {})
