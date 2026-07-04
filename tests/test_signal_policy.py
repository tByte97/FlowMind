from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.signal_policy import (
    area_pressure_by_incoming_lane,
    choose_phase,
    score_phases,
)
from flowmind.traffic_state import LaneState, TrafficState


class SignalPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.intersection = Intersection(
            tls_id="test",
            position=(0.0, 0.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink("north", "south", 0),
                ControlledLink("east", "west", 1),
            ),
        )
        self.config = ControlConfig()

    def test_local_controller_prefers_largest_incoming_queue(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(12, 12, 0.5, 0.0, 3.0),
                "south": LaneState(0, 0, 0.0, 10.0, 15.0),
                "east": LaneState(3, 3, 0.2, 0.0, 8.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )
        best = choose_phase(
            score_phases(self.intersection, state, "local", self.config)
        )
        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 0)

    def test_area_controller_avoids_blocked_downstream(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(12, 12, 0.5, 0.0, 3.0),
                "south": LaneState(10, 15, 0.95, 0.0, 0.0),
                "east": LaneState(5, 5, 0.3, 0.0, 8.0),
                "west": LaneState(0, 0, 0.1, 10.0, 15.0),
            }
        )
        best = choose_phase(
            score_phases(self.intersection, state, "flowmind", self.config)
        )
        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 2)

    def test_priority_link_overrides_regular_score(self) -> None:
        state = TrafficState({})
        best = choose_phase(
            score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
                priority_link=1,
            )
        )
        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 2)

    def test_flowmind_can_use_area_pressure_bias(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(4, 12, 0.9, 0.0, 2.0),
                "south": LaneState(0, 0, 0.1, 10.0, 15.0),
                "east": LaneState(5, 5, 0.3, 0.0, 8.0),
                "west": LaneState(0, 0, 0.8, 1.0, 2.0),
            }
        )
        area_pressure = area_pressure_by_incoming_lane(
            AreaModel((self.intersection,)),
            state,
            self.config,
        )

        best = choose_phase(
            score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
                area_pressure=area_pressure,
            )
        )

        self.assertIsNotNone(best)
        self.assertIn("north", area_pressure)
        self.assertEqual(best.phase_index, 0)


if __name__ == "__main__":
    unittest.main()
