"""Compatibility entry point for the modular RTK Dashboard backend.

Application code lives in :mod:`rtk_dashboard`.  Re-exports keep existing
service files, tests, and small integrations using `import server` working.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from rtk_dashboard.common import (
    finite_float,
    finite_int,
    first_numeric,
    first_query_value,
    normalize_statistics_device_id,
    normalize_statistics_range,
    now_ms,
    parse_query_ms,
    parse_range_ms,
)
from rtk_dashboard.config import load_config, parse_simple_scalar, parse_simple_yaml
from rtk_dashboard.device_profiles import (
    DeviceProfile,
    DeviceRegistry,
    coerce_value,
    decode_json_or_value,
    extract_device_name,
    has_valid_position,
    infer_device_type,
    normalize_position,
)
from rtk_dashboard.http_server import DashboardHttpServer, DashboardHttpHandler, start_http
from rtk_dashboard.models import DeviceRecord
from rtk_dashboard.mqtt import MinimalMqttBroker
from rtk_dashboard.peer_udp import PeerUdpListener
from rtk_dashboard.state import DashboardState, FIX_MODE_LABELS, NTRIP_STATUS_LABELS
from rtk_dashboard.statistics import StatisticsSubscription, StatisticsSubscriptionRegistry
from rtk_dashboard.storage import TelemetryLogger
from rtk_dashboard.tiles import (
    discover_mbtiles,
    normalize_tile_format,
    open_mbtiles,
    parse_mbtiles_bounds,
    read_mbtiles_info,
    tile_content_type,
)


# Historical name retained for downstream imports.
extract_rover_name = extract_device_name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local MQTT dashboard for multiple IoT device types")
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
    MinimalMqttBroker(state, str(mqtt_cfg["host"]), int(mqtt_cfg["port"])).start()

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
