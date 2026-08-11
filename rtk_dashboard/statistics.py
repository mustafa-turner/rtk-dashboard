"""Shared statistics stream subscriptions."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .common import normalize_statistics_device_id, normalize_statistics_range, now_ms
from .storage import TelemetryLogger

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
