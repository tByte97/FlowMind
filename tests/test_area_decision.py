from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.signal_policy import choose_phase, score_phases
from flowmind.traffic_state import LaneState, TrafficState
from flowmind.zone_graph import (
    AreaGraph,
    Corridor,
    IntersectionStorage,
    RoadSegment,
    ZoneDefinition,
    ZoneIntersection,
    build_area_decision_snapshot,
)


class AreaDecisionPropagationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ControlConfig()
        self.i02 = Intersection(
            tls_id="I-02",
            position=(0.0, 0.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink("i02_main_in", "i02_main_out", 0),
                ControlledLink("i02_side_in", "i02_side_out", 1),
            ),
        )
        tls_ids = ("I-02", "I-03", "REMOTE-1", "REMOTE-2")
        self.area = AreaModel(
            (
                self.i02,
                *(
                    Intersection(
                        tls_id=tls_id,
                        position=(0.0, 0.0),
                        phases=("G", "y"),
                        links=(
                            ControlledLink(
                                f"{tls_id}_in",
                                f"{tls_id}_out",
                                0,
                            ),
                        ),
                    )
                    for tls_id in tls_ids[1:]
                ),
            )
        )
        zone = ZoneDefinition(
            "zone",
            "Zone",
            (
                ZoneIntersection("I-02", "I-02", ("main",)),
                ZoneIntersection("I-03", "I-03", ("main",)),
                ZoneIntersection("REMOTE-1", "REMOTE-1", ("remote",)),
                ZoneIntersection("REMOTE-2", "REMOTE-2", ("remote",)),
            ),
            (
                Corridor("main", ("I-02", "I-03")),
                Corridor("remote", ("REMOTE-1", "REMOTE-2")),
            ),
        )
        self.graph = AreaGraph(
            zone,
            (
                RoadSegment(
                    "main:I-02>I-03",
                    "main",
                    "I-02",
                    "I-03",
                    ("main_edge",),
                    ("i02_main_out", "main_middle", "I-03_in"),
                    ("i02_main_out",),
                    ("I-03_in",),
                    100.0,
                    10.0,
                ),
                RoadSegment(
                    "remote:REMOTE-1>REMOTE-2",
                    "remote",
                    "REMOTE-1",
                    "REMOTE-2",
                    ("remote_edge",),
                    ("REMOTE-1_out", "remote_middle", "REMOTE-2_in"),
                    ("REMOTE-1_out",),
                    ("REMOTE-2_in",),
                    100.0,
                    10.0,
                ),
            ),
            (
                IntersectionStorage("I-02", ("i02_main_in",), 10.0),
                IntersectionStorage("I-03", ("I-03_in",), 10.0),
                IntersectionStorage("REMOTE-1", ("REMOTE-1_in",), 10.0),
                IntersectionStorage("REMOTE-2", ("REMOTE-2_in",), 10.0),
            ),
        )

    @staticmethod
    def lane(
        queue: int = 0,
        vehicles: int = 0,
        occupancy: float = 0.0,
        free_slots: float = 15.0,
    ) -> LaneState:
        return LaneState(queue, vehicles, occupancy, 0.0, free_slots)

    def traffic(
        self,
        *,
        i03_congested: bool = False,
        remote_congested: bool = False,
    ) -> TrafficState:
        return TrafficState(
            {
                "i02_main_in": self.lane(8, 8, 0.45),
                "i02_main_out": self.lane(),
                "main_middle": self.lane(),
                "i02_side_in": self.lane(7, 7, 0.4),
                "i02_side_out": self.lane(),
                "I-03_in": (
                    self.lane(9, 10, 0.95, 0.0)
                    if i03_congested
                    else self.lane()
                ),
                "REMOTE-1_in": self.lane(),
                "REMOTE-1_out": self.lane(),
                "remote_middle": self.lane(),
                "REMOTE-2_in": (
                    self.lane(9, 10, 0.95, 0.0)
                    if remote_congested
                    else self.lane()
                ),
            }
        )

    def selected_i02_phase(self, traffic: TrafficState) -> int | None:
        snapshot = build_area_decision_snapshot(
            self.area,
            self.graph,
            traffic,
            12.0,
            self.config,
        )
        best = choose_phase(
            score_phases(
                self.i02,
                traffic,
                "flowmind",
                self.config,
                area_pressure=snapshot.area_pressure_by_incoming_lane,
                downstream_risk_by_outgoing_lane=(
                    snapshot.downstream_risk_by_outgoing_lane
                ),
            )
        )
        return best.phase_index if best is not None else None

    def test_congestion_on_i03_changes_i02_decision(self) -> None:
        clear_phase = self.selected_i02_phase(self.traffic())
        congested_phase = self.selected_i02_phase(
            self.traffic(i03_congested=True)
        )

        self.assertEqual(clear_phase, 0)
        self.assertEqual(congested_phase, 2)

    def test_unrelated_remote_tls_does_not_change_i02_decision(self) -> None:
        clear_phase = self.selected_i02_phase(self.traffic())
        remote_phase = self.selected_i02_phase(
            self.traffic(remote_congested=True)
        )

        self.assertEqual(clear_phase, 0)
        self.assertEqual(remote_phase, clear_phase)


if __name__ == "__main__":
    unittest.main()
