from __future__ import annotations

import unittest
from unittest.mock import patch

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.signal_policy import PhaseScore
from flowmind.traffic_state import LaneState, TrafficState
from flowmind.zone_graph import (
    AreaDecisionSnapshot,
    AreaGraph,
    IntersectionStorage,
    NodeStorageState,
    RoadSegment,
    SegmentTrafficState,
    ZoneDefinition,
    ZoneIntersection,
)
from flowmind.zone_optimizer import optimize_zone_phases


def _build_corridor_fixture(
    pair_count: int,
) -> tuple[
    AreaModel,
    AreaGraph,
    AreaDecisionSnapshot,
    dict[str, tuple[PhaseScore, ...]],
]:
    intersections: list[Intersection] = []
    zone_intersections: list[ZoneIntersection] = []
    segments: list[RoadSegment] = []
    node_storage: list[IntersectionStorage] = []
    lane_ids: set[str] = set()
    segment_states: dict[str, SegmentTrafficState] = {}
    node_states: dict[str, NodeStorageState] = {}
    platoon_arrivals: dict[str, float] = {}
    downstream_storage: dict[str, float] = {}
    upstream_queues: dict[str, float] = {}
    downstream_occupancy: dict[str, float] = {}
    scores: dict[str, tuple[PhaseScore, ...]] = {}

    for index in range(pair_count):
        suffix = str(index)
        upstream_id = f"upstream-{suffix}"
        downstream_id = f"downstream-{suffix}"
        segment_in = f"segment_in-{suffix}"
        segment_out = f"segment_out-{suffix}"
        segment_lane = f"segment-{suffix}"
        destination = f"dest-{suffix}"
        upstream = Intersection(
            tls_id=upstream_id,
            position=(0.0, float(index) * 100.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink(f"feed-{suffix}", segment_out, 0),
                ControlledLink(f"side-{suffix}", f"side_out-{suffix}", 1),
            ),
        )
        downstream = Intersection(
            tls_id=downstream_id,
            position=(100.0, float(index) * 100.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink(segment_in, destination, 0),
                ControlledLink(f"cross-{suffix}", f"cross_out-{suffix}", 1),
            ),
        )
        intersections.extend((upstream, downstream))
        zone_intersections.extend(
            (
                ZoneIntersection(upstream_id, upstream_id, (f"main-{suffix}",)),
                ZoneIntersection(
                    downstream_id,
                    downstream_id,
                    (f"main-{suffix}",),
                ),
            )
        )
        segment = RoadSegment(
            segment_id=f"main-{suffix}:{upstream_id}>{downstream_id}",
            corridor_id=f"main-{suffix}",
            upstream_tls=upstream_id,
            downstream_tls=downstream_id,
            edge_ids=(f"edge-{suffix}",),
            lane_ids=(segment_lane,),
            upstream_outgoing_lanes=(segment_out,),
            downstream_incoming_lanes=(segment_in,),
            length_meters=100.0,
            capacity_slots=20.0,
        )
        segments.append(segment)
        node_storage.extend(
            (
                IntersectionStorage(upstream_id, (segment_out,), 20.0),
                IntersectionStorage(downstream_id, (destination,), 20.0),
            )
        )
        lane_ids.update(
            (
                f"feed-{suffix}",
                segment_out,
                f"side-{suffix}",
                f"side_out-{suffix}",
                segment_in,
                destination,
                f"cross-{suffix}",
                f"cross_out-{suffix}",
                segment_lane,
            )
        )
        segment_states[segment.segment_id] = SegmentTrafficState(
            segment.segment_id,
            20.0,
            8.0,
            12.0,
            2,
            0.4,
            0.0,
        )
        for tls_id in (upstream_id, downstream_id):
            node_states[tls_id] = NodeStorageState(
                tls_id,
                20.0,
                2.0,
                18.0,
                0.1,
                0.0,
            )
        platoon_arrivals[segment_in] = 10.0
        downstream_storage[segment_out] = 12.0
        upstream_queues[segment_in] = 8.0
        downstream_occupancy[segment_out] = 0.4
        scores[upstream_id] = (PhaseScore(0, 12.0), PhaseScore(2, 11.0))
        scores[downstream_id] = (PhaseScore(0, 11.0), PhaseScore(2, 12.0))

    area = AreaModel(tuple(intersections))
    graph = AreaGraph(
        zone=ZoneDefinition(
            zone_id="test",
            name="test",
            intersections=tuple(zone_intersections),
            corridors=(),
        ),
        segments=tuple(segments),
        node_storage=tuple(node_storage),
    )
    snapshot = AreaDecisionSnapshot(
        simulation_time=30.0,
        traffic=TrafficState(
            {
                lane_id: LaneState(0, 0, 0.0, 10.0, 20.0)
                for lane_id in lane_ids
            }
        ),
        segment_states=segment_states,
        node_states=node_states,
        downstream_risk_by_outgoing_lane={},
        area_pressure_by_incoming_lane={},
        platoon_arrival_by_incoming_lane=platoon_arrivals,
        downstream_storage_by_outgoing_lane=downstream_storage,
        upstream_queue_by_incoming_lane=upstream_queues,
        downstream_occupancy_by_outgoing_lane=downstream_occupancy,
    )
    return area, graph, snapshot, scores


class ZoneOptimizerTest(unittest.TestCase):
    def test_joint_objective_aligns_upstream_and_downstream_phases(self) -> None:
        upstream = Intersection(
            tls_id="upstream",
            position=(0.0, 0.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink("feed", "segment_out", 0),
                ControlledLink("side", "side_out", 1),
            ),
        )
        downstream = Intersection(
            tls_id="downstream",
            position=(100.0, 0.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink("segment_in", "dest", 0),
                ControlledLink("cross", "cross_out", 1),
            ),
        )
        area = AreaModel((upstream, downstream))
        zone = ZoneDefinition(
            zone_id="test",
            name="test",
            intersections=(
                ZoneIntersection("upstream", "upstream", ("main",)),
                ZoneIntersection("downstream", "downstream", ("main",)),
            ),
            corridors=(),
        )
        segment = RoadSegment(
            segment_id="main:upstream>downstream",
            corridor_id="main",
            upstream_tls="upstream",
            downstream_tls="downstream",
            edge_ids=("edge",),
            lane_ids=("segment",),
            upstream_outgoing_lanes=("segment_out",),
            downstream_incoming_lanes=("segment_in",),
            length_meters=100.0,
            capacity_slots=20.0,
        )
        graph = AreaGraph(
            zone=zone,
            segments=(segment,),
            node_storage=(
                IntersectionStorage("upstream", ("segment_out",), 20.0),
                IntersectionStorage("downstream", ("dest",), 20.0),
            ),
        )
        traffic = TrafficState(
            {
                lane_id: LaneState(0, 0, 0.0, 10.0, 20.0)
                for lane_id in (
                    "feed",
                    "segment_out",
                    "side",
                    "side_out",
                    "segment_in",
                    "dest",
                    "cross",
                    "cross_out",
                    "segment",
                )
            }
        )
        snapshot = AreaDecisionSnapshot(
            simulation_time=30.0,
            traffic=traffic,
            segment_states={
                segment.segment_id: SegmentTrafficState(
                    segment.segment_id,
                    20.0,
                    8.0,
                    12.0,
                    2,
                    0.4,
                    0.0,
                )
            },
            node_states={
                tls_id: NodeStorageState(tls_id, 20.0, 2.0, 18.0, 0.1, 0.0)
                for tls_id in ("upstream", "downstream")
            },
            downstream_risk_by_outgoing_lane={},
            area_pressure_by_incoming_lane={},
            platoon_arrival_by_incoming_lane={"segment_in": 10.0},
            downstream_storage_by_outgoing_lane={"segment_out": 12.0},
            upstream_queue_by_incoming_lane={"segment_in": 8.0},
            downstream_occupancy_by_outgoing_lane={"segment_out": 0.4},
        )

        choices = optimize_zone_phases(
            area,
            graph,
            snapshot,
            {
                "upstream": (PhaseScore(0, 12.0), PhaseScore(2, 11.0)),
                "downstream": (PhaseScore(0, 11.0), PhaseScore(2, 12.0)),
            },
            ControlConfig(),
        )

        self.assertEqual(choices["upstream"].phase_index, 0)
        self.assertEqual(choices["downstream"].phase_index, 0)

    def test_weak_coordination_gain_is_rejected_in_favour_of_local_phase(self) -> None:
        area, graph, snapshot, scores = _build_corridor_fixture(1)

        choices = optimize_zone_phases(
            area,
            graph,
            snapshot,
            scores,
            ControlConfig(
                zone_coordination_weight=10.0,
                zone_override_min_gain=1_000_000.0,
                zone_override_max_local_loss=100.0,
            ),
        )

        downstream = choices["downstream-0"]
        self.assertEqual(downstream.local_phase_index, 2)
        self.assertEqual(downstream.phase_index, downstream.local_phase_index)
        self.assertFalse(downstream.is_override)
        self.assertEqual(downstream.coordination_gain, 0.0)
        self.assertEqual(downstream.local_score_loss, 0.0)

    def test_hold_only_contract_excludes_non_current_zone_switches(self) -> None:
        area, graph, snapshot, scores = _build_corridor_fixture(1)

        choices = optimize_zone_phases(
            area,
            graph,
            snapshot,
            scores,
            ControlConfig(),
            current_phase_by_tls={
                "upstream-0": 0,
                "downstream-0": 2,
            },
        )

        downstream = choices["downstream-0"]
        self.assertEqual(downstream.local_phase_index, 2)
        self.assertEqual(downstream.phase_index, 2)
        self.assertFalse(downstream.is_override)

    def test_in_flight_platoon_can_justify_downstream_green_hold(self) -> None:
        area, graph, snapshot, scores = _build_corridor_fixture(1)
        scores = {
            **scores,
            "upstream-0": (PhaseScore(0, 0.0), PhaseScore(2, 100.0)),
            "downstream-0": (PhaseScore(0, 11.4), PhaseScore(2, 12.0)),
        }

        choices = optimize_zone_phases(
            area,
            graph,
            snapshot,
            scores,
            ControlConfig(
                zone_override_min_gain=0.1,
                zone_override_max_local_loss=1.0,
            ),
            current_phase_by_tls={
                "upstream-0": 2,
                "downstream-0": 0,
            },
        )

        downstream = choices["downstream-0"]
        self.assertEqual(downstream.local_phase_index, 2)
        self.assertEqual(downstream.phase_index, 0)
        self.assertTrue(downstream.is_override)
        self.assertAlmostEqual(downstream.local_score_loss, 0.6)
        self.assertGreaterEqual(downstream.coordination_gain, 0.1)

    def test_candidate_hold_is_valued_against_fixed_neighbour(self) -> None:
        area, graph, snapshot, scores = _build_corridor_fixture(1)

        choices = optimize_zone_phases(
            area,
            graph,
            snapshot,
            {"downstream-0": scores["downstream-0"]},
            ControlConfig(
                zone_override_min_gain=0.1,
                zone_override_max_local_loss=2.0,
            ),
            current_phase_by_tls={
                "upstream-0": 0,
                "downstream-0": 0,
            },
        )

        self.assertEqual(set(choices), {"downstream-0"})
        downstream = choices["downstream-0"]
        self.assertEqual(downstream.local_phase_index, 2)
        self.assertEqual(downstream.phase_index, 0)
        self.assertTrue(downstream.is_override)
        self.assertGreater(downstream.coordination_gain, 0.1)

    def test_override_cap_keeps_only_strongest_diagnostics(self) -> None:
        area, graph, snapshot, scores = _build_corridor_fixture(2)

        choices = optimize_zone_phases(
            area,
            graph,
            snapshot,
            scores,
            ControlConfig(
                zone_coordination_weight=10.0,
                zone_override_min_gain=0.0,
                zone_override_max_local_loss=100.0,
                max_zone_overrides_per_step=1,
            ),
        )

        overrides = [choice for choice in choices.values() if choice.is_override]
        self.assertEqual(len(overrides), 1)
        self.assertGreater(overrides[0].coordination_gain, 0.0)
        self.assertEqual(overrides[0].local_score_loss, 1.0)
        self.assertNotEqual(
            overrides[0].phase_index,
            overrides[0].local_phase_index,
        )
        for choice in choices.values():
            if choice.is_override:
                continue
            self.assertEqual(choice.phase_index, choice.local_phase_index)
            self.assertEqual(choice.coordination_gain, 0.0)
            self.assertEqual(choice.local_score_loss, 0.0)

    def test_override_gain_is_revalidated_after_cap_removes_synergy(self) -> None:
        area, graph, snapshot, scores = _build_corridor_fixture(1)
        local_phases = {
            "upstream-0": 0,
            "downstream-0": 2,
        }

        def synergistic_objective(
            assignment,
            _intersections,
            _graph,
            _snapshot,
            _score_lookup,
            _config,
        ) -> float:
            override_count = sum(
                phase_index != local_phases[tls_id]
                for tls_id, phase_index in assignment.items()
            )
            return (0.0, 0.4, 2.0)[override_count]

        with patch(
            "flowmind.zone_optimizer._joint_objective",
            side_effect=synergistic_objective,
        ):
            choices = optimize_zone_phases(
                area,
                graph,
                snapshot,
                scores,
                ControlConfig(
                    zone_override_min_gain=0.5,
                    zone_override_max_local_loss=100.0,
                    max_zone_overrides_per_step=1,
                ),
            )

        self.assertFalse(any(choice.is_override for choice in choices.values()))
        for choice in choices.values():
            self.assertEqual(choice.phase_index, choice.local_phase_index)
            self.assertEqual(choice.coordination_gain, 0.0)



if __name__ == "__main__":
    unittest.main()
