"""SQLite telemetry history, rollups, queries, and replay."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .common import finite_float, finite_int, first_numeric, normalize_statistics_range, now_ms, parse_range_ms
from .device_profiles import DEFAULT_DEVICE_REGISTRY, DeviceRegistry
from .paths import ROOT

class TelemetryLogger:
    def __init__(self, config: dict[str, Any], device_registry: DeviceRegistry | None = None):
        self.config = config
        self.device_registry = device_registry or DEFAULT_DEVICE_REGISTRY
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

                create table if not exists hourly_numeric_metrics (
                    hour_ms integer not null,
                    device_id text not null,
                    metric_key text not null,
                    min_value real not null,
                    avg_value real not null,
                    max_value real not null,
                    sample_count integer not null,
                    updated_ms integer not null,
                    primary key (hour_ms, device_id, metric_key)
                );

                create index if not exists idx_numeric_device_hour
                    on hourly_numeric_metrics(device_id, hour_ms);

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
        at_ms: int | None = None,
    ) -> None:
        if not self.enabled or self.con is None or not payload:
            return
        received_at_ms = now_ms()
        has_measurement_time = at_ms is not None
        at_ms = int(at_ms) if has_measurement_time else received_at_ms
        last_sample_ms = self._last_sample_by_device.get(device_id, 0)
        if not has_measurement_time and self.sample_min_interval_ms and received_at_ms - last_sample_ms < self.sample_min_interval_ms:
            return

        device_type = self.device_registry.infer_type(payload, source_type)
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
            if existing is not None and received_at_ms - int(existing["last_seen_ms"]) > 5 * 60 * 1000:
                self.con.execute(
                    """
                    insert into system_events (at_ms, event_type, device_id, severity, message, data_json)
                    values (?, 'telemetry_gap', ?, 'warn', ?, ?)
                    """,
                    (
                        received_at_ms,
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
                    received_at_ms,
                    received_at_ms,
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
            self._last_sample_by_device[device_id] = received_at_ms

        if received_at_ms - self._last_cleanup_ms > 60 * 60 * 1000:
            self.cleanup()
            self._last_cleanup_ms = received_at_ms

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
                self.con.execute("delete from hourly_numeric_metrics where hour_ms < ?", (cutoff,))
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

            numeric_rows = self.con.execute(
                """
                select at_ms, device_id, device_type, payload_json
                from telemetry_samples
                where at_ms >= ? and at_ms <= ?
                """,
                (start_hour_ms, end_ms),
            ).fetchall()
            numeric: dict[tuple[int, str, str], dict[str, float | int]] = {}
            for sample in numeric_rows:
                try:
                    payload = json.loads(sample["payload_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                profile = self.device_registry.profile(str(sample["device_type"] or "device"))
                for metric in profile.metrics:
                    value = finite_float(payload.get(metric.key))
                    if value is None:
                        continue
                    key = ((int(sample["at_ms"]) // 3600000) * 3600000, str(sample["device_id"]), metric.key)
                    aggregate = numeric.setdefault(
                        key, {"min": value, "max": value, "sum": 0.0, "count": 0}
                    )
                    aggregate["min"] = min(float(aggregate["min"]), value)
                    aggregate["max"] = max(float(aggregate["max"]), value)
                    aggregate["sum"] = float(aggregate["sum"]) + value
                    aggregate["count"] = int(aggregate["count"]) + 1
            for (hour_ms, device_id, metric_key), aggregate in numeric.items():
                count = int(aggregate["count"])
                self.con.execute(
                    """
                    insert into hourly_numeric_metrics (
                        hour_ms, device_id, metric_key, min_value, avg_value, max_value, sample_count, updated_ms
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(hour_ms, device_id, metric_key) do update set
                        min_value = excluded.min_value,
                        avg_value = excluded.avg_value,
                        max_value = excluded.max_value,
                        sample_count = excluded.sample_count,
                        updated_ms = excluded.updated_ms
                    """,
                    (
                        hour_ms, device_id, metric_key, aggregate["min"],
                        float(aggregate["sum"]) / count, aggregate["max"], count, updated_ms,
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
            return {"enabled": False, "device_metrics": [], "pair_metrics": [], "numeric_metrics": []}
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
            numeric_rows = self.con.execute(
                f"""
                select * from hourly_numeric_metrics
                where hour_ms >= ? and hour_ms <= ?{device_filter}
                order by hour_ms asc, device_id collate nocase, metric_key collate nocase
                """,
                params,
            ).fetchall()
        return {
            "enabled": True,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "device_metrics": device_metric_rows,
            "pair_metrics": [dict(row) for row in pair_rows],
            "numeric_metrics": [dict(row) for row in numeric_rows],
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
            restored_fields = {
                "fix_mode": row["fix_mode"],
                "ntrip_status": row["ntrip_status"],
                "nearest_peer_id": row["peer_id"],
                "nearest_peer_safe_distance_m": row["safe_distance_m"],
                "nearest_peer_distance_m": row["raw_distance_m"],
                "nearest_peer_uncertainty_m": row["uncertainty_m"],
                "local_accuracy_m": row["accuracy_m"],
                "nearest_peer_accuracy_m": row["peer_accuracy_m"],
                "battery_percent": row["battery_percent"],
                "battery_voltage_v": row["battery_voltage_v"],
                "uptime_sec": row["uptime_sec"],
            }
            payload.update({key: value for key, value in restored_fields.items() if value is not None and value != ""})
            if row["fix_mode"] is not None:
                payload["fix_mode_label"] = FIX_MODE_LABELS.get(row["fix_mode"], "UNKNOWN")
            if row["ntrip_status"] is not None:
                payload["ntrip_status_label"] = NTRIP_STATUS_LABELS.get(row["ntrip_status"], "UNKNOWN")

            device_id = str(row["device_id"])
            device_type = str(row["device_type"] or self.device_registry.infer_type(payload))
            profile = self.device_registry.profile(device_type)
            devices[device_id] = {
                "device_id": device_id,
                "display_name": row["display_name"] or device_id,
                "device_type": device_type,
                "profile": profile.to_dict(),
                "telemetry": payload,
                "last_seen_ms": row["at_ms"],
                "last_telemetry_seen_ms": row["at_ms"],
                "last_position_seen_ms": row["at_ms"],
                "last_measurement_ms": row["at_ms"],
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
