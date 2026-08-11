import tempfile
import unittest
from pathlib import Path

import server


class ProfileMetricStorageTests(unittest.TestCase):
    def test_profile_metrics_are_rolled_up_without_schema_specific_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "logging": {
                    "enabled": True,
                    "databasePath": str(Path(directory) / "telemetry.sqlite"),
                    "rawRetentionDays": 1,
                    "summaryRetentionDays": 1,
                    "sampleMinIntervalSec": 0,
                    "rollupIntervalSec": 3600,
                },
                "devices": {"types": {}},
            }
            logger = server.TelemetryLogger(config, server.DeviceRegistry(config))
            try:
                logger.log_sample(
                    device_id="weather-1",
                    display_name="Weather 1",
                    payload={"device_type": "weather_station", "temperature_c": 30.5, "humidity_percent": 70},
                    source_type="mqtt",
                )
                current_ms = server.now_ms()
                result = logger.hourly(current_ms - 3600000, current_ms + 1000, "weather-1")

                metrics = {row["metric_key"]: row for row in result["numeric_metrics"]}
                self.assertEqual(30.5, metrics["temperature_c"]["avg_value"])
                self.assertEqual(70, metrics["humidity_percent"]["max_value"])
            finally:
                logger.con.close()

    def test_device_measurement_timestamp_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "logging": {
                    "enabled": True,
                    "databasePath": str(Path(directory) / "timestamps.sqlite"),
                    "rawRetentionDays": 1,
                    "summaryRetentionDays": 1,
                    "sampleMinIntervalSec": 10,
                    "rollupIntervalSec": 3600,
                },
                "devices": {"types": {}},
            }
            logger = server.TelemetryLogger(config, server.DeviceRegistry(config))
            measured_at_ms = 1_800_000_000_000
            try:
                logger.log_sample(
                    device_id="tide-1",
                    display_name="Tide 1",
                    payload={"device_type": "tide_sensor", "water_level_m": 1.2},
                    source_type="mqtt",
                    at_ms=measured_at_ms,
                )
                rows = logger.samples(measured_at_ms - 1, measured_at_ms + 1, "tide-1")["samples"]
                self.assertEqual(measured_at_ms, rows[0]["at_ms"])
            finally:
                logger.con.close()


if __name__ == "__main__":
    unittest.main()
