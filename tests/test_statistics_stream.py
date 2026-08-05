import threading
import time
import unittest

import server


class FakeStatisticsLogger:
    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []
        self.fail_next = False

    def statistics_snapshot(self, range_name, device_id=""):
        with self._lock:
            self.calls.append((range_name, device_id))
            call_count = len(self.calls)
            should_fail = self.fail_next
            self.fail_next = False
        if should_fail:
            raise RuntimeError("temporary test failure")
        return {
            "summary": {"enabled": True, "sample_count": call_count},
            "hourly": {"enabled": True, "device_metrics": [], "pair_metrics": []},
            "events": {"enabled": True, "events": []},
            "samples": {"enabled": True, "samples": []} if range_name == "live" else None,
        }


class StatisticsSubscriptionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.logger = FakeStatisticsLogger()
        self.registry = server.StatisticsSubscriptionRegistry(self.logger)

    def tearDown(self):
        for group in list(self.registry._groups.values()):
            with group.condition:
                group.clients.clear()
                group.condition.notify_all()
            if group.producer:
                group.producer.join(timeout=1)

    def wait_until(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_ten_matching_clients_share_one_producer_and_cached_snapshot(self):
        subscriptions = [self.registry.subscribe("live", "rover-1")]
        group, _ = subscriptions[0]
        first = self.registry.wait_for_snapshot(group, -1, timeout=1)
        self.assertIsNotNone(first)
        self.assertEqual(1, len(self.logger.calls))

        subscriptions.extend(self.registry.subscribe("live", "rover-1") for _ in range(9))
        for matching_group, _ in subscriptions[1:]:
            cached = self.registry.wait_for_snapshot(matching_group, -1, timeout=0)
            self.assertIs(group, matching_group)
            self.assertIs(first, cached)
        self.assertEqual(1, len(self.logger.calls))
        self.assertEqual(10, len(group.clients))

        for matching_group, client_id in subscriptions[:-1]:
            self.registry.unsubscribe(matching_group, client_id)
        self.assertTrue(group.producer.is_alive())
        self.assertEqual(1, len(group.clients))
        self.registry.unsubscribe(*subscriptions[-1])
        self.assertTrue(self.wait_until(lambda: group.key not in self.registry._groups))

    def test_manual_refresh_is_shared_and_rate_limited(self):
        group, client = self.registry.subscribe("24h", "rover-2")
        first = self.registry.wait_for_snapshot(group, -1, timeout=1)
        self.assertEqual(1, first["version"])

        accepted = self.registry.request_refresh("24h", "rover-2")
        duplicate = self.registry.request_refresh("24h", "rover-2")
        second = self.registry.wait_for_snapshot(group, 1, timeout=1)

        self.assertTrue(accepted["accepted"])
        self.assertEqual("cooldown", duplicate["status"])
        self.assertEqual(2, second["version"])
        self.assertEqual(2, len(self.logger.calls))
        self.registry.unsubscribe(group, client)

    def test_temporary_failure_keeps_group_alive_for_next_refresh(self):
        self.logger.fail_next = True
        group, client = self.registry.subscribe("7d", "rover-3")
        self.assertTrue(self.wait_until(lambda: len(self.logger.calls) == 1))
        self.assertTrue(group.producer.is_alive())

        result = self.registry.request_refresh("7d", "rover-3")
        snapshot = self.registry.wait_for_snapshot(group, -1, timeout=1)

        self.assertTrue(result["accepted"])
        self.assertIsNotNone(snapshot)
        self.assertEqual(1, snapshot["version"])
        self.assertEqual(2, len(self.logger.calls))
        self.registry.unsubscribe(group, client)

    def test_range_normalization_and_refresh_intervals(self):
        self.assertEqual(5, self.registry.refresh_interval("live"))
        self.assertEqual(60, self.registry.refresh_interval("24h"))
        self.assertEqual(300, self.registry.refresh_interval("7d"))
        self.assertEqual("24h", server.normalize_statistics_range("unknown"))


if __name__ == "__main__":
    unittest.main()
