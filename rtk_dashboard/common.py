"""Small dependency-free parsing and value helpers."""

from __future__ import annotations

import ipaddress
import math
import time
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def measurement_timestamp_ms(payload: dict[str, Any]) -> int | None:
    """Return a sane device-supplied measurement timestamp, if present."""
    for key in ("measured_at_ms", "timestamp_ms", "recorded_at_ms"):
        value = finite_float(payload.get(key))
        if value is None:
            continue
        # Tolerate epoch seconds even though the canonical fields end in `_ms`.
        timestamp = int(value * 1000) if 1_700_000_000 <= value < 100_000_000_000 else int(value)
        current = now_ms()
        if 1_700_000_000_000 <= timestamp <= current + 5 * 60 * 1000:
            return timestamp
    return None


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def finite_int(value: Any) -> int | None:
    number = finite_float(value)
    return int(number) if number is not None else None


def first_numeric(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = finite_float(payload.get(key))
        if value is not None:
            return value
    return None


def parse_range_ms(value: str | None, default_ms: int = 24 * 60 * 60 * 1000) -> int:
    if not value:
        return default_ms
    text = value.strip().lower()
    units = {"h": 60 * 60 * 1000, "d": 24 * 60 * 60 * 1000, "w": 7 * 24 * 60 * 60 * 1000}
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
