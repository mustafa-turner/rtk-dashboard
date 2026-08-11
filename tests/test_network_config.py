import os
import unittest
from unittest import mock

import server
from rtk_dashboard.network import advertised_mqtt_host, public_mqtt_config


class MqttNetworkConfigTests(unittest.TestCase):
    def test_explicit_advertised_host_is_public_but_credentials_are_not(self):
        config = {
            "host": "0.0.0.0",
            "port": 1883,
            "advertisedHost": "192.168.50.20",
            "username": "device",
            "password": "must-not-leak",
        }

        public = public_mqtt_config(config)

        self.assertEqual("192.168.50.20", public["advertisedHost"])
        self.assertEqual("explicit", public["advertisedHostSource"])
        self.assertTrue(public["authRequired"])
        self.assertNotIn("username", public)
        self.assertNotIn("password", public)

    def test_specific_bind_host_is_advertised_when_auto_is_selected(self):
        self.assertEqual(
            "10.33.240.3",
            advertised_mqtt_host({"host": "10.33.240.3", "advertisedHost": "auto"}),
        )

    def test_broker_reads_password_from_environment(self):
        state = type(
            "State",
            (),
            {"config": {"mqtt": {"username": "tide", "passwordEnv": "TEST_MQTT_PASSWORD"}}},
        )()
        with mock.patch.dict(os.environ, {"TEST_MQTT_PASSWORD": "secret-value"}):
            broker = server.MinimalMqttBroker(state, "127.0.0.1", 1883)

        self.assertTrue(broker.credentials_are_valid("tide", "secret-value"))
        self.assertFalse(broker.credentials_are_valid("tide", "wrong"))


if __name__ == "__main__":
    unittest.main()
