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


if __name__ == "__main__":
    unittest.main()
