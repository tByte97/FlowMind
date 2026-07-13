from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
