import unittest

import server


class DeviceProfileTests(unittest.TestCase):
    def test_weather_station_is_inferred_and_aliases_are_canonicalized(self):
        registry = server.DeviceRegistry()

        payload, profile = registry.normalize(
            {"temperature": 31.5, "humidity": 72, "lat": 13.75, "lng": 100.5}
        )

        self.assertEqual("weather_station", profile.key)
        self.assertEqual(31.5, payload["temperature_c"])
        self.assertEqual(72, payload["humidity_percent"])
        self.assertEqual([100.5, 13.75], payload["position"])

    def test_unknown_explicit_type_keeps_its_identity_with_generic_behavior(self):
        registry = server.DeviceRegistry()

        payload, profile = registry.normalize({"device_type": "soil_probe", "moisture": 44})

        self.assertEqual("soil_probe", profile.key)
        self.assertEqual("Soil Probe", profile.label)
        self.assertEqual("soil_probe", payload["device_type"])

    def test_custom_profile_can_be_declared_only_in_configuration(self):
        registry = server.DeviceRegistry(
            {
                "devices": {
                    "types": {
                        "air_quality": {
                            "label": "Air Quality",
                            "matchFields": ["pm25"],
                            "aliases": {"pm25_ugm3": ["pm25"]},
                            "metrics": [{"key": "pm25_ugm3", "label": "PM2.5", "unit": " ug/m3"}],
                        }
                    }
                }
            }
        )

        payload, profile = registry.normalize({"pm25": 12.4})

        self.assertEqual("air_quality", profile.key)
        self.assertEqual(12.4, payload["pm25_ugm3"])
        self.assertEqual("PM2.5", profile.metrics[0].label)

    def test_tide_logger_fields_are_normalized_for_its_dashboard_profile(self):
        registry = server.DeviceRegistry()

        payload, profile = registry.normalize(
            {
                "device_type": "tide_sensor",
                "tide_level_m": 1.725,
                "distance_mm": 1275,
                "battery_voltage": 4.012,
                "quality": 2,
                "used_samples": 44,
            }
        )

        self.assertEqual("Tide Logger", profile.label)
        self.assertEqual(1.725, payload["water_level_m"])
        self.assertEqual(1275, payload["distance_to_water_mm"])
        self.assertEqual(4.012, payload["battery_voltage_v"])
        self.assertEqual(44, payload["samples_used"])


class MultiDeviceIngestionTests(unittest.TestCase):
    def setUp(self):
        self.config = server.load_config(__import__("pathlib").Path("/definitely/missing/config.yaml"))
        self.config["logging"]["enabled"] = False
        self.config["udpPeers"]["enabled"] = False
        self.state = server.DashboardState(self.config)

    def publish(self, topic, payload, client_id):
        import json

        self.state.update_from_mqtt(
            topic=topic,
            raw_payload=json.dumps(payload).encode(),
            client_id=client_id,
            username="device",
            source_host="127.0.0.1",
        )

    def test_canonical_topics_keep_multiple_device_types_separate(self):
        self.publish("telemetry/weather_station/weather-1", {"temperature": 29.2}, "gateway")
        self.publish("telemetry/truck/truck-7", {"speed": 18, "lat": 1, "lon": 2}, "gateway")

        snapshot = self.state.snapshot()

        self.assertEqual({"weather-1", "truck-7"}, set(snapshot["devices"]))
        self.assertEqual("weather_station", snapshot["devices"]["weather-1"]["device_type"])
        self.assertEqual(18, snapshot["devices"]["truck-7"]["telemetry"]["speed_kph"])
        self.assertIn("weather_station", snapshot["server"]["device_types"])

    def test_legacy_batch_ds_rover_remains_compatible(self):
        self.publish("batch_ds", {"device_id": "rover-1", "fix_mode": 4, "ntrip_status": 1}, "rover-1")

        rover = self.state.snapshot()["devices"]["rover-1"]

        self.assertEqual("crane_rover", rover["device_type"])
        self.assertEqual("RTK FIXED", rover["telemetry"]["fix_mode_label"])
        self.assertTrue(rover["profile"]["supports_peer_safety"])

    def test_legacy_info_topic_updates_the_client_device(self):
        self.publish("batch_ds", {"fix_mode": 4}, "rover-info")
        self.publish("info/mcu", {"device_name": "North Rover", "firmware": "1.2.3"}, "rover-info")

        snapshot = self.state.snapshot()

        self.assertEqual({"rover-info"}, set(snapshot["devices"]))
        self.assertEqual("North Rover", snapshot["devices"]["rover-info"]["display_name"])
        self.assertEqual("crane_rover", snapshot["devices"]["rover-info"]["device_type"])


if __name__ == "__main__":
    unittest.main()
