"""MBTiles discovery and tile metadata helpers."""
from __future__ import annotations

import logging
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .paths import MBTILES_ROOT

def open_mbtiles(path: Path) -> sqlite3.Connection:
    uri_path = quote(str(path.resolve()), safe="/")
    return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)


def parse_mbtiles_bounds(value: Any) -> list[float] | None:
    try:
        bounds = [float(part.strip()) for part in str(value).split(",")]
    except (TypeError, ValueError):
        return None
    if len(bounds) != 4 or not all(math.isfinite(part) for part in bounds):
        return None
    west, south, east, north = bounds
    if west >= east or south >= north:
        return None
    return [west, south, east, north]


def normalize_tile_format(value: Any) -> str:
    tile_format = str(value or "png").strip().lower().lstrip(".")
    if tile_format == "jpeg":
        return "jpg"
    if tile_format in {"jpg", "png", "webp", "pbf"}:
        return tile_format
    return "png"


def tile_content_type(tile_format: str) -> str:
    return {
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "pbf": "application/x-protobuf",
    }.get(normalize_tile_format(tile_format), "application/octet-stream")


def read_mbtiles_info(path: Path) -> dict[str, Any] | None:
    try:
        with closing(open_mbtiles(path)) as con:
            metadata = {str(name): value for name, value in con.execute("select name, value from metadata")}
            min_zoom, max_zoom = con.execute("select min(zoom_level), max(zoom_level) from tiles").fetchone()
    except sqlite3.Error as exc:
        logging.warning("MBTiles metadata skipped for %s: %s", path.name, exc)
        return None

    if min_zoom is None or max_zoom is None:
        return None

    tile_format = normalize_tile_format(metadata.get("format"))
    tileset_id = path.stem
    return {
        "id": tileset_id,
        "name": str(metadata.get("name") or tileset_id),
        "description": str(metadata.get("description") or ""),
        "type": str(metadata.get("type") or "overlay"),
        "format": tile_format,
        "bounds": parse_mbtiles_bounds(metadata.get("bounds")),
        "minZoom": int(min_zoom),
        "maxZoom": int(max_zoom),
        "tileUrl": f"/tiles/{quote(tileset_id, safe='')}/{{z}}/{{x}}/{{y}}.{tile_format}",
    }


def discover_mbtiles() -> list[dict[str, Any]]:
    if not MBTILES_ROOT.exists():
        return []
    tilesets: list[dict[str, Any]] = []
    for path in sorted(MBTILES_ROOT.glob("*.mbtiles")):
        if path.stem.lower() in {"old", "backup", "archive"} or path.stem.lower().startswith(("old_", "backup_")):
            continue
        info = read_mbtiles_info(path)
        if info is not None:
            tilesets.append(info)
    return tilesets
