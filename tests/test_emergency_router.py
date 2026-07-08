from __future__ import annotations

import unittest
from pathlib import Path

from flowmind.area_model import discover_area, load_zone_tls_ids
from flowmind.emergency_router import EmergencyRouter
from flowmind.config import PROJECT_ROOT


class FakeSimulation:
    def findRoute(self, *_args: object, **_kwargs: object):
        class Stage:
            edges = ("-110281352#3", "148132825")
            length = 1_000.0
            travelTime = 100.0

        return Stage()


class FakeEdge:
    def getLastStepHaltingNumber(self, _edge_id: str) -> int:
        return 0

    def getEffort(self, _edge_id: str, _time: float) -> float:
        return -1.0

    def getTraveltime(self, _edge_id: str) -> float:
        return 1.0

    def setEffort(
        self,
        _edge_id: str,
        _effort: float,
        _begin: float | None = None,
        _end: float | None = None,
    ) -> None:
        return None


class FakeTraci:
    def __init__(self) -> None:
        self.simulation = FakeSimulation()
        self.edge = FakeEdge()


class EmergencyRouterTest(unittest.TestCase):
    def test_router_finds_multiple_network_alternatives(self) -> None:
        scenario_dir = PROJECT_ROOT / "simulation" / "new_area"
        net_path = scenario_dir / "osm.net.xml.gz"
        zone_path = scenario_dir / "central_zone.json"
        if not net_path.exists() or not zone_path.exists():
            self.skipTest("new_area scenario is not generated")

        area = discover_area(net_path, requested_tls=load_zone_tls_ids(zone_path))
        best, alternatives = EmergencyRouter(
            FakeTraci(),
            area,
            Path(net_path),
        ).find_alternatives(
            "-110281352#3",
            "148132825",
            "zone_passenger",
            60.0,
            num_alternatives=5,
        )

        self.assertIsNotNone(best)
        self.assertGreaterEqual(len(alternatives), 3)
        self.assertGreaterEqual(len(best.tls_sequence), 4)
        self.assertEqual(alternatives[0]["reason"], "Selected (Lowest ETA)")
        self.assertIn("blocked_edge_penalty", alternatives[0])
        self.assertIn("corridor_activation_cost", alternatives[0])
        self.assertIn("civil_traffic_impact", alternatives[0])


if __name__ == "__main__":
    unittest.main()
