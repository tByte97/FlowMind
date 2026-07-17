from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.traffic_state import LaneState, TrafficState
from flowmind.zone_graph import (
    AreaGraph,
    Corridor,
    IntersectionStorage,
    RoadSegment,
    ZoneDefinition,
    ZoneIntersection,
    build_area_decision_snapshot,
    load_zone_definition,
)


class ZoneGraphTest(unittest.TestCase):
    def test_loads_and_validates_corridors(self) -> None:
        payload = {
            "id": "test-zone",
            "intersections": [
                {"tls_id": "I-02", "corridors": ["main"]},
                {"tls_id": "I-03", "corridors": ["main"]},
            ],
            "corridors": {"main": ["I-02", "I-03"]},
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "zone.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            zone = load_zone_definition(path)

        self.assertEqual(zone.zone_id, "test-zone")
        self.assertEqual(zone.tls_ids, ("I-02", "I-03"))
        self.assertEqual(zone.corridors[0].tls_sequence, ("I-02", "I-03"))

    def test_snapshot_propagates_only_connected_downstream_risk(self) -> None:
        zone = ZoneDefinition(
            "zone",
            "Zone",
            (
                ZoneIntersection("I-02", "I-02", ("main",)),
                ZoneIntersection("I-03", "I-03", ("main",)),
                ZoneIntersection("REMOTE", "REMOTE", ("remote",)),
                ZoneIntersection("REMOTE-2", "REMOTE-2", ("remote",)),
            ),
            (
                Corridor("main", ("I-02", "I-03")),
                Corridor("remote", ("REMOTE", "REMOTE-2")),
            ),
        )
        graph = AreaGraph(
            zone,
            (
                RoadSegment(
                    "main:I-02>I-03",
                    "main",
                    "I-02",
                    "I-03",
                    ("e1",),
                    ("i02_out", "i03_in"),
                    ("i02_out",),
                    ("i03_in",),
                    150.0,
                    20.0,
                ),
                RoadSegment(
                    "remote:REMOTE>REMOTE-2",
                    "remote",
                    "REMOTE",
                    "REMOTE-2",
                    ("e2",),
                    ("remote_out", "remote2_in"),
                    ("remote_out",),
                    ("remote2_in",),
                    150.0,
                    20.0,
                ),
            ),
            (
                IntersectionStorage("I-02", ("i02_in",), 10.0),
                IntersectionStorage("I-03", ("i03_in",), 10.0),
                IntersectionStorage("REMOTE", ("remote_in",), 10.0),
                IntersectionStorage("REMOTE-2", ("remote2_in",), 10.0),
            ),
        )
        area = AreaModel(
            tuple(
                Intersection(
                    tls_id=tls_id,
                    position=(0.0, 0.0),
                    phases=("G", "y"),
                    links=(ControlledLink(f"{tls_id}_in", f"{tls_id}_out", 0),),
                )
                for tls_id in zone.tls_ids
            )
        )
        traffic = TrafficState(
            {
                "i02_out": LaneState(0, 0, 0.0, 10.0, 20.0),
                "i03_in": LaneState(9, 10, 0.95, 0.0, 0.0),
                "remote_out": LaneState(0, 0, 0.0, 10.0, 20.0),
                "i02_in": LaneState(0, 0, 0.0, 10.0, 10.0),
                "remote_in": LaneState(0, 0, 0.0, 10.0, 10.0),
                "remote2_in": LaneState(0, 0, 0.0, 10.0, 10.0),
            }
        )

        snapshot = build_area_decision_snapshot(
            area,
            graph,
            traffic,
            12.0,
            ControlConfig(),
        )

        self.assertGreater(snapshot.downstream_risk_by_outgoing_lane["i02_out"], 0.8)
        self.assertEqual(
            snapshot.downstream_risk_by_outgoing_lane["remote_out"],
            0.0,
        )
        self.assertGreater(snapshot.node_states["I-03"].spillback_probability, 0.8)


if __name__ == "__main__":
    unittest.main()
