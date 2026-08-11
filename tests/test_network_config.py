import unittest

import server
from rtk_dashboard.network import advertised_mqtt_host, public_mqtt_config


class MqttNetworkConfigTests(unittest.TestCase):
    def test_explicit_advertised_host_is_public(self):
        config = {
            "host": "0.0.0.0",
            "port": 1883,
            "advertisedHost": "192.168.50.20",
        }

        public = public_mqtt_config(config)

        self.assertEqual("192.168.50.20", public["advertisedHost"])
        self.assertEqual("explicit", public["advertisedHostSource"])

    def test_specific_bind_host_is_advertised_when_auto_is_selected(self):
        self.assertEqual(
            "10.33.240.3",
            advertised_mqtt_host({"host": "10.33.240.3", "advertisedHost": "auto"}),
        )

if __name__ == "__main__":
    unittest.main()
