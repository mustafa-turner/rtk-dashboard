"""Filesystem locations used by the dashboard."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = ROOT / "static"
MBTILES_ROOT = ROOT / "mbtiles"
