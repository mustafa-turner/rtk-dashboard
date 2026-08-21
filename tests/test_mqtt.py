import unittest

from rtk_dashboard.mqtt import handle_mqtt_client


class FakeBroker:
    def __init__(self):
        self._counter = 0

    def next_client_id(self):
        self._counter += 1
        return f"test-client-{self._counter}"


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        chunk = self.incoming[:size]
        del self.incoming[:size]
        return bytes(chunk)

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        pass


class MqttConnectionTests(unittest.TestCase):
    def test_valid_connect_receives_connack(self):
        client_id = b"tide-test"
        variable_header = b"\x00\x04MQTT\x04\x02\x00\x0f"
        payload = len(client_id).to_bytes(2, "big") + client_id
        connect = (
            b"\x10" + bytes([len(variable_header) + len(payload)])
            + variable_header + payload
        )
        sock = FakeSocket(connect + b"\xe0\x00")

        handle_mqtt_client(sock, ("127.0.0.1", 12345), FakeBroker())

        self.assertEqual(b"\x20\x02\x00\x00", bytes(sock.sent))
        self.assertEqual(90, sock.timeout)


if __name__ == "__main__":
    unittest.main()
