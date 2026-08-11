"""HTTP API, server-sent events, static assets, and MBTiles delivery."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .common import finite_int, first_query_value, now_ms, parse_query_ms, parse_range_ms
from .paths import MBTILES_ROOT, STATIC_ROOT
from .state import DashboardState
from .tiles import open_mbtiles, tile_content_type

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
        if parsed.path.startswith("/api/logs/"):
            self.handle_logs_api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/replay/"):
            self.handle_replay_api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path == "/api/statistics/stream":
            self.handle_statistics_stream(parse_qs(parsed.query))
            return
        if parsed.path == "/events":
            self.handle_events()
            return
        if parsed.path.startswith("/tiles/"):
            self.serve_mbtiles_tile(parsed.path)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/statistics/refresh":
            query = parse_qs(parsed.query)
            result = self.dashboard_state.statistics_registry.request_refresh(
                first_query_value(query, "range"), first_query_value(query, "device_id")
            )
            self.send_json(result, HTTPStatus.ACCEPTED if result["accepted"] else HTTPStatus.OK)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_logs_api(self, path: str, query: dict[str, list[str]]) -> None:
        logger = self.dashboard_state.telemetry_logger
        range_ms = parse_range_ms(first_query_value(query, "range"), 24 * 60 * 60 * 1000)
        end_ms = parse_query_ms(first_query_value(query, "to"), now_ms())
        start_ms = parse_query_ms(first_query_value(query, "from"), end_ms - range_ms)
        device_id = first_query_value(query, "device_id") or ""
        limit = int(finite_int(first_query_value(query, "limit")) or 200)

        if path == "/api/logs/summary":
            self.send_json(logger.summary(range_ms, device_id))
            return
        if path == "/api/logs/hourly":
            self.send_json(logger.hourly(start_ms, end_ms, device_id))
            return
        if path == "/api/logs/events":
            self.send_json(logger.events(start_ms, end_ms, device_id, limit))
            return
        if path == "/api/logs/samples":
            self.send_json(logger.samples(start_ms, end_ms, device_id, limit))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_replay_api(self, path: str, query: dict[str, list[str]]) -> None:
        logger = self.dashboard_state.telemetry_logger
        if path == "/api/replay/range":
            self.send_json(logger.replay_range())
            return
        if path == "/api/replay/state":
            at_ms = parse_query_ms(first_query_value(query, "at"), now_ms())
            lookback_ms = parse_query_ms(first_query_value(query, "lookback"), 5 * 60 * 1000)
            replay = logger.replay_snapshot(at_ms, lookback_ms)
            live = self.dashboard_state.snapshot()
            self.send_json(
                {
                    "version": -at_ms,
                    "server": {
                        **live["server"],
                        "now_ms": at_ms,
                        "replay": True,
                        "requested_at_ms": at_ms,
                        "lookback_ms": replay.get("lookback_ms", lookback_ms),
                    },
                    "devices": replay.get("devices", {}),
                    "peers": {},
                    "events": [],
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def handle_statistics_stream(self, query: dict[str, list[str]]) -> None:
        registry = self.dashboard_state.statistics_registry
        group, client_id = registry.subscribe(
            first_query_value(query, "range"), first_query_value(query, "device_id")
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.connection.settimeout(30)

        version = -1
        try:
            while True:
                payload = registry.wait_for_snapshot(group, version)
                if payload is None:
                    self.wfile.write(b": heartbeat\n\n")
                else:
                    version = int(payload["version"])
                    encoded = json.dumps(payload, separators=(",", ":"))
                    self.wfile.write(f"event: snapshot\ndata: {encoded}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
        finally:
            registry.unsubscribe(group, client_id)

    def handle_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        version = -1
        try:
            while True:
                changed, snapshot = self.dashboard_state.wait_for_update(version)
                if not changed:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue

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
            with closing(open_mbtiles(tile_path)) as con:
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
