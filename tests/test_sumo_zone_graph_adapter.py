from __future__ import annotations

import unittest

from flowmind.area_model import discover_area
from flowmind.config import PROJECT_ROOT
from flowmind.sumo_zone_graph_adapter import SumoZoneGraphAdapter
from flowmind.zone_graph import load_zone_definition


SCENARIO_DIR = PROJECT_ROOT / "simulation" / "rivne_area"


class SumoZoneGraphAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.zone = load_zone_definition(SCENARIO_DIR / "central_zone.json")
        cls.area = discover_area(
            SCENARIO_DIR / "osm.net.xml.gz",
            requested_tls=cls.zone.tls_ids,
        )
        cls.graph = SumoZoneGraphAdapter(
            SCENARIO_DIR / "osm.net.xml.gz"
        ).build_graph(cls.zone, cls.area)

    def test_builds_both_directions_for_each_corridor_pair(self) -> None:
        expected = sum(
            (len(corridor.tls_sequence) - 1) * 2
            for corridor in self.zone.corridors
        )

        self.assertEqual(len(self.graph.segments), expected)
        self.assertTrue(all(segment.edge_ids for segment in self.graph.segments))
        self.assertTrue(all(segment.lane_ids for segment in self.graph.segments))
        self.assertTrue(
            all(segment.capacity_slots > 0 for segment in self.graph.segments)
        )

    def test_segments_connect_controlled_outgoing_to_downstream_incoming(self) -> None:
        for segment in self.graph.segments:
            with self.subTest(segment=segment.segment_id):
                upstream = self.area.intersection(segment.upstream_tls)
                downstream = self.area.intersection(segment.downstream_tls)
                self.assertTrue(
                    set(segment.upstream_outgoing_lanes)
                    <= {link.outgoing_lane for link in upstream.links}
                )
                self.assertTrue(
                    set(segment.downstream_incoming_lanes)
                    <= {link.incoming_lane for link in downstream.links}
                )
                self.assertTrue(segment.upstream_outgoing_lanes)
                self.assertTrue(segment.downstream_incoming_lanes)

    def test_storage_covers_every_zone_node(self) -> None:
        self.assertEqual(
            {item.tls_id for item in self.graph.node_storage},
            set(self.zone.tls_ids),
        )
        self.assertTrue(
            all(item.capacity_slots > 0 for item in self.graph.node_storage)
        )


if __name__ == "__main__":
    unittest.main()
