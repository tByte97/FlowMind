from __future__ import annotations

import unittest

from experiments.tune_control import objective_score


class TuneControlTest(unittest.TestCase):
    def test_objective_rewards_outflow_and_penalizes_spillback(self) -> None:
        healthy = objective_score(
            {
                "average_waiting_time": 20,
                "average_queue_length": 4,
                "blocked_outgoing_share": 0.01,
                "stops_count": 100,
                "departed_vehicles": 100,
                "zone_outflow": 500,
            }
        )
        congested = objective_score(
            {
                "average_waiting_time": 30,
                "average_queue_length": 8,
                "blocked_outgoing_share": 0.30,
                "stops_count": 200,
                "departed_vehicles": 100,
                "zone_outflow": 300,
            }
        )
        self.assertLess(healthy, congested)


if __name__ == "__main__":
    unittest.main()
