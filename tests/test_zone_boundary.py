from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.zone_boundary import build_zone_boundary
from flowmind.zone_graph import (
    AreaGraph,
    Corridor,
    IntersectionStorage,
    RoadSegment,
    ZoneDefinition,
    ZoneIntersection,
)


class ZoneBoundaryTest(unittest.TestCase):
    def test_internal_corridor_lanes_are_not_counted_as_boundaries(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    "A",
                    (0.0, 0.0),
                    ("G",),
                    (
                        ControlledLink("entry_0", "internal-a_0", 0),
                    ),
                ),
                Intersection(
                    "B",
                    (1.0, 0.0),
                    ("G",),
                    (
                        ControlledLink("internal-b_0", "exit_0", 0),
                    ),
                ),
            )
        )
        zone = ZoneDefinition(
            "zone",
            "Zone",
            (
                ZoneIntersection("A", "A", ("main",)),
                ZoneIntersection("B", "B", ("main",)),
            ),
            (Corridor("main", ("A", "B")),),
        )
        graph = AreaGraph(
            zone,
            (
                RoadSegment(
                    "A>B",
                    "main",
                    "A",
                    "B",
                    ("internal-a", "internal-b"),
                    ("internal-a_0", "internal-b_0"),
                    ("internal-a_0",),
                    ("internal-b_0",),
                    100.0,
                    10.0,
                ),
            ),
            (
                IntersectionStorage("A", ("internal-a_0",), 10.0),
                IntersectionStorage("B", ("internal-b_0",), 10.0),
            ),
        )

        boundary = build_zone_boundary(area, graph)

        self.assertEqual(boundary.incoming_edge_ids, ("entry",))
        self.assertEqual(boundary.outgoing_edge_ids, ("exit",))
        self.assertTrue(boundary.valid)

    def test_all_segment_path_lanes_are_internal(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    "A",
                    (0.0, 0.0),
                    ("G",),
                    (
                        ControlledLink("entry_0", "path_0", 0),
                        ControlledLink("path_0", "exit_0", 1),
                    ),
                ),
            )
        )
        zone = ZoneDefinition(
            "zone",
            "Zone",
            (
                ZoneIntersection("A", "A", ("loop",)),
                ZoneIntersection("B", "B", ("loop",)),
            ),
            (Corridor("loop", ("A", "B")),),
        )
        graph = AreaGraph(
            zone,
            (
                RoadSegment(
                    "A>B",
                    "loop",
                    "A",
                    "B",
                    ("path",),
                    ("path_0",),
                    ("path_0",),
                    ("path_0",),
                    100.0,
                    10.0,
                ),
            ),
            (
                IntersectionStorage("A", ("entry_0",), 10.0),
                IntersectionStorage("B", ("path_0",), 10.0),
            ),
        )

        boundary = build_zone_boundary(area, graph)

        self.assertEqual(boundary.incoming_edge_ids, ("entry",))
        self.assertEqual(boundary.outgoing_edge_ids, ("exit",))

    def test_edge_shared_by_two_controlled_tls_is_internal(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    "A",
                    (0.0, 0.0),
                    ("G",),
                    (ControlledLink("entry_0", "shared_0", 0),),
                ),
                Intersection(
                    "B",
                    (1.0, 0.0),
                    ("G",),
                    (ControlledLink("shared_1", "exit_0", 0),),
                ),
            )
        )
        zone = ZoneDefinition(
            "zone",
            "Zone",
            (
                ZoneIntersection("A", "A", ("main",)),
                ZoneIntersection("B", "B", ("main",)),
            ),
            (Corridor("main", ("A", "B")),),
        )
        graph = AreaGraph(
            zone,
            (
                RoadSegment(
                    "A>B",
                    "main",
                    "A",
                    "B",
                    ("other",),
                    ("other_0",),
                    ("other_0",),
                    ("other_0",),
                    100.0,
                    10.0,
                ),
            ),
            (
                IntersectionStorage("A", ("entry_0",), 10.0),
                IntersectionStorage("B", ("shared_1",), 10.0),
            ),
        )

        boundary = build_zone_boundary(area, graph)

        self.assertEqual(boundary.incoming_edge_ids, ("entry",))
        self.assertEqual(boundary.outgoing_edge_ids, ("exit",))


if __name__ == "__main__":
    unittest.main()
