from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.signal_policy import (
    area_pressure_by_incoming_lane,
    choose_phase,
    effective_max_green,
    effective_min_green,
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

    def test_blocked_downstream_phase_is_removed_from_candidates(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(30, 30, 0.3, 0.0, 8.0),
                "south": LaneState(30, 30, 0.95, 0.0, 0.0),
                "east": LaneState(5, 5, 0.2, 0.0, 8.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        scores = score_phases(self.intersection, state, "flowmind", self.config)
        best = choose_phase(scores)

        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 2)
        self.assertNotIn(0, [item.phase_index for item in scores])

    def test_full_moving_downstream_is_blocked_even_without_a_queue(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(8, 8, 0.4, 0.0, 5.0),
                "south": LaneState(0, 16, 0.75, 8.0, 0.0),
                "east": LaneState(4, 4, 0.2, 0.0, 8.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        scores = score_phases(self.intersection, state, "flowmind", self.config)

        self.assertNotIn(0, [item.phase_index for item in scores])

    def test_idle_blocked_movement_does_not_disable_useful_shared_phase(self) -> None:
        intersection = Intersection(
            tls_id="shared",
            position=(0.0, 0.0),
            phases=("GG", "yy", "rr"),
            links=(
                ControlledLink("idle", "blocked", 0),
                ControlledLink("busy", "open", 1),
            ),
        )
        state = TrafficState(
            {
                "idle": LaneState(0, 0, 0.0, 0.0, 10.0),
                "blocked": LaneState(12, 12, 0.95, 0.0, 0.0),
                "busy": LaneState(8, 8, 0.5, 0.0, 4.0),
                "open": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        scores = score_phases(intersection, state, "flowmind", self.config)

        self.assertEqual([item.phase_index for item in scores], [0])

    def test_demanded_blocked_group_is_masked_without_dropping_shared_phase(
        self,
    ) -> None:
        intersection = Intersection(
            tls_id="shared",
            position=(0.0, 0.0),
            phases=("GG", "yy", "rr"),
            links=(
                ControlledLink("blocked_demand", "blocked", 0),
                ControlledLink("busy", "open", 1),
            ),
        )
        state = TrafficState(
            {
                "blocked_demand": LaneState(8, 8, 0.5, 0.0, 4.0),
                "blocked": LaneState(12, 16, 0.95, 0.0, 0.0),
                "busy": LaneState(8, 8, 0.5, 0.0, 4.0),
                "open": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        scores = score_phases(intersection, state, "flowmind", self.config)

        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].phase_index, 0)
        self.assertEqual(scores[0].blocked_signal_indices, (0,))

    def test_priority_cannot_force_blocked_downstream_phase(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(30, 30, 0.3, 0.0, 8.0),
                "south": LaneState(30, 30, 0.95, 0.0, 0.0),
                "east": LaneState(5, 5, 0.2, 0.0, 8.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        best = choose_phase(
            score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
                priority_link=0,
            )
        )

        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 2)

    def test_priority_requires_storage_even_before_vehicle_reaches_sensor(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(0, 0, 0.0, 0.0, 15.0),
                "south": LaneState(0, 12, 0.70, 8.0, 1.5),
                "east": LaneState(3, 3, 0.2, 0.0, 8.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        regular_scores = score_phases(
            self.intersection,
            state,
            "flowmind",
            self.config,
        )
        priority_scores = score_phases(
            self.intersection,
            state,
            "flowmind",
            self.config,
            priority_link=0,
        )

        self.assertIn(0, [item.phase_index for item in regular_scores])
        self.assertNotIn(0, [item.phase_index for item in priority_scores])

    def test_priority_link_overrides_regular_score(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(0, 0, 0.0, 0.0, 15.0),
                "south": LaneState(0, 0, 0.0, 10.0, 15.0),
                "east": LaneState(0, 0, 0.0, 0.0, 15.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )
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

    def test_prepare_target_is_a_soft_bonus_not_hard_priority(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(3, 3, 0.2, 0.0, 8.0),
                "south": LaneState(0, 0, 0.0, 10.0, 15.0),
                "east": LaneState(3, 3, 0.2, 0.0, 8.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )
        regular = {
            item.phase_index: item.score
            for item in score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
            )
        }
        prepared = {
            item.phase_index: item.score
            for item in score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
                preparation_link=1,
            )
        }

        self.assertEqual(
            prepared[2] - regular[2],
            self.config.corridor_prepare_bonus,
        )
        self.assertLess(prepared[2] - regular[2], 1_000.0)

    def test_prepare_target_cannot_open_a_blocked_downstream(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(1, 1, 0.1, 0.0, 8.0),
                "south": LaneState(0, 0, 0.0, 10.0, 15.0),
                "east": LaneState(0, 0, 0.0, 0.0, 8.0),
                "west": LaneState(8, 16, 0.95, 0.0, 0.0),
            }
        )

        scores = score_phases(
            self.intersection,
            state,
            "flowmind",
            self.config,
            preparation_link=1,
        )

        self.assertNotIn(2, [item.phase_index for item in scores])

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

    def test_flowmind_can_use_queue_forecast_bias(self) -> None:
        state = TrafficState({})

        best = choose_phase(
            score_phases(
                self.intersection,
                state,
                "flowmind",
                ControlConfig(queue_forecast_weight=1.0),
                queue_forecast={(2, 1): 9.0},
            )
        )

        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 2)

    def test_flowmind_keeps_empty_approach_low_priority(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(0, 0, 0.0, 0.0, 15.0),
                "south": LaneState(0, 0, 0.0, 10.0, 15.0),
                "east": LaneState(9, 12, 0.72, 0.0, 2.0),
                "west": LaneState(0, 0, 0.1, 10.0, 15.0),
            }
        )

        best = choose_phase(
            score_phases(self.intersection, state, "flowmind", self.config)
        )

        self.assertIsNotNone(best)
        self.assertEqual(best.phase_index, 2)

    def test_waiting_demand_timer_can_break_a_small_tie(self) -> None:
        state = TrafficState(
            {
                "north": LaneState(2, 2, 0.15, 0.0, 10.0),
                "south": LaneState(0, 0, 0.0, 10.0, 15.0),
                "east": LaneState(2, 2, 0.15, 0.0, 10.0),
                "west": LaneState(0, 0, 0.0, 10.0, 15.0),
            }
        )

        fresh = choose_phase(
            score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
                demand_wait_by_lane={"north": 12.0, "east": 1.0},
            )
        )
        waited = choose_phase(
            score_phases(
                self.intersection,
                state,
                "flowmind",
                self.config,
                demand_wait_by_lane={"north": 1.0, "east": 12.0},
            )
        )

        self.assertIsNotNone(fresh)
        self.assertIsNotNone(waited)
        self.assertEqual(fresh.phase_index, 0)
        self.assertEqual(waited.phase_index, 2)

    def test_default_phase_timing_caps_effective_green_window(self) -> None:
        intersection = Intersection(
            tls_id="timed",
            position=(0.0, 0.0),
            phases=("G", "y"),
            links=(ControlledLink("north", "south", 0),),
            phase_durations=(6.0, 3.0),
        )

        self.assertEqual(effective_min_green(self.config, intersection, 0), 6.0)
        self.assertEqual(effective_max_green(self.config, intersection, 0), 14.0)

    def test_sumo_min_dur_is_a_hard_floor_for_every_timing_mode(self) -> None:
        intersection = Intersection(
            tls_id="timed",
            position=(0.0, 0.0),
            phases=("G", "y"),
            links=(ControlledLink("north", "south", 0),),
            phase_durations=(6.0, 3.0),
            phase_min_durations=(13.0, None),
            phase_max_durations=(50.0, None),
        )

        self.assertEqual(effective_min_green(self.config, intersection, 0), 13.0)
        self.assertEqual(
            effective_min_green(
                ControlConfig(use_default_phase_timing=False, min_green=4),
                intersection,
                0,
            ),
            13.0,
        )
        self.assertEqual(effective_max_green(self.config, intersection, 0), 14.0)


if __name__ == "__main__":
    unittest.main()
