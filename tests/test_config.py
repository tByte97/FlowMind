from __future__ import annotations

import unittest

from flowmind.config import ControlConfig


class ControlConfigValidationTest(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        config = ControlConfig()

        self.assertEqual(config.decision_interval, 3)
        self.assertFalse(config.graph_hard_mask_enabled)
        self.assertTrue(config.physical_hard_mask_enabled)
        self.assertTrue(config.throughput_fallback_enabled)

    def test_rejects_impossible_green_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_green"):
            ControlConfig(min_green=50, max_green=10)

    def test_rejects_zero_scheduler_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "decision_interval"):
            ControlConfig(decision_interval=0)

    def test_rejects_invalid_probability(self) -> None:
        with self.assertRaisesRegex(ValueError, "blocked_occupancy"):
            ControlConfig(blocked_occupancy=1.5)

    def test_rejects_duplicate_forecast_horizons(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            ControlConfig(queue_forecast_horizon_weights=((30, 0.5), (30, 0.5)))

    def test_rejects_invalid_corridor_lookahead(self) -> None:
        with self.assertRaisesRegex(ValueError, "corridor_prepare_tls_count"):
            ControlConfig(corridor_prepare_tls_count=0)


if __name__ == "__main__":
    unittest.main()
