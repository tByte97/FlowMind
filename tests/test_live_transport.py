from __future__ import annotations

import socket
import unittest

from flowmind.live_transport import LiveTelemetryClient, LiveTelemetryPublisher


def find_free_port(start: int = 8766) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LiveTransportTests(unittest.TestCase):
    def test_publisher_and_client_exchange_payloads(self) -> None:
        port = find_free_port()
        publisher = LiveTelemetryPublisher(host="127.0.0.1", port=port)
        publisher.start()
        client = LiveTelemetryClient(url=f"ws://127.0.0.1:{port}")
        client.start()

        try:
            payload = {"simulated_time": 12.5, "summary": {"throughput": 3}}
            publisher.publish(payload)
            received = client.wait_for_update(timeout=2.0)
            self.assertEqual(received["simulated_time"], 12.5)
            self.assertEqual(received["summary"]["throughput"], 3)
        finally:
            publisher.stop()
            client.stop()

    def test_late_client_receives_only_latest_snapshot(self) -> None:
        port = find_free_port()
        publisher = LiveTelemetryPublisher(host="127.0.0.1", port=port)
        publisher.start()
        publisher.publish({"simulated_time": 1.0})
        publisher.publish({"simulated_time": 2.0})
        client = LiveTelemetryClient(url=f"ws://127.0.0.1:{port}/live")
        client.start()

        try:
            self.assertTrue(client.wait_until_connected(timeout=2.0))
            received = client.wait_for_update(timeout=2.0)
            self.assertEqual(received["simulated_time"], 2.0)
            self.assertIsNone(client.latest_update())
            self.assertTrue(client.status["connected"])
        finally:
            publisher.stop()
            client.stop()

    def test_nonblocking_wait_reads_queued_update(self) -> None:
        client = LiveTelemetryClient(url="ws://127.0.0.1:1")
        client._enqueue_latest({"simulated_time": 3.0})

        self.assertEqual(client.wait_for_update(timeout=0)["simulated_time"], 3.0)


if __name__ == "__main__":
    unittest.main()
