"""RTK Dashboard backend package.

The package is intentionally split by responsibility.  ``server.py`` remains a
small compatibility entry point for existing installations and imports.
"""

from .config import load_config
from .device_profiles import DeviceProfile, DeviceRegistry
from .http_server import DashboardHttpServer, start_http
from .mqtt import MinimalMqttBroker
from .state import DashboardState
from .statistics import StatisticsSubscription, StatisticsSubscriptionRegistry
from .storage import TelemetryLogger

__all__ = [
    "DashboardHttpServer",
    "DashboardState",
    "DeviceProfile",
    "DeviceRegistry",
    "MinimalMqttBroker",
    "StatisticsSubscription",
    "StatisticsSubscriptionRegistry",
    "TelemetryLogger",
    "load_config",
    "start_http",
]
