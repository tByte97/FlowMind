from __future__ import annotations

import unittest

from flowmind.area_model import Intersection
from flowmind.corridor_recovery import CorridorRecoveryPlanner


class CorridorRecoveryPlannerTest(unittest.TestCase):
    def test_reconstructs_baseline_phase_and_remaining_offset(self) -> None:
        intersection = Intersection(
            tls_id="tls",
            position=(0.0, 0.0),
            phases=("G", "y", "r", "u"),
            links=(),
            phase_durations=(10.0, 3.0, 7.0, 2.0),
        )
        planner = CorridorRecoveryPlanner()
        planner.capture(intersection, phase_index=0, phase_elapsed=4.0, simulation_time=5.0)

        target = planner.target("tls", simulation_time=14.0)

        self.assertIsNotNone(target)
        self.assertEqual(target.phase_index, 2)
        self.assertEqual(target.phase_elapsed, 0.0)
        self.assertEqual(target.remaining_duration, 7.0)

    def test_capture_is_idempotent_until_restored(self) -> None:
        intersection = Intersection(
            tls_id="tls",
            position=(0.0, 0.0),
            phases=("G", "y"),
            links=(),
            phase_durations=(10.0, 2.0),
        )
        planner = CorridorRecoveryPlanner()
        planner.capture(intersection, 0, 2.0, 0.0)
        planner.capture(intersection, 1, 1.0, 5.0)
        self.assertEqual(planner.target("tls", 0.0).phase_index, 0)

        planner.mark_restored("tls")
        planner.capture(intersection, 1, 1.0, 5.0)
        self.assertEqual(planner.target("tls", 5.0).phase_index, 1)


if __name__ == "__main__":
    unittest.main()
