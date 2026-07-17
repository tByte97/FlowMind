from __future__ import annotations

import json
import unittest

from flowmind.area_model import discover_area, load_zone_tls_ids
from flowmind.config import PROJECT_ROOT
from flowmind.emergency_router import EmergencyRouter
from flowmind.emergency_vehicle import load_emergency_config


class FakeEdgeDomain:
    def getLastStepHaltingNumber(self, _edge_id: str) -> int:
        return 0

    def getLastStepVehicleNumber(self, _edge_id: str) -> int:
        return 0

    def getLastStepMeanSpeed(self, _edge_id: str) -> float:
        return 10.0

    def getEffort(self, _edge_id: str, _time: float) -> float:
        return -1.0

    def getTraveltime(self, _edge_id: str) -> float:
        return 1.0

    def setEffort(self, *_args: object) -> None:
        return None


class FakeSimulation:
    def findRoute(self, *_args: object, **_kwargs: object):
        class EmptyStage:
            edges: tuple[str, ...] = ()
            length = 0.0
            travelTime = 0.0

        return EmptyStage()


class FakeTraci:
    def __init__(self) -> None:
        self.edge = FakeEdgeDomain()
        self.simulation = FakeSimulation()


class DemoScenarioContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario_dir = PROJECT_ROOT / "simulation" / "rivne_area"
        cls.net_path = cls.scenario_dir / "osm.net.xml.gz"
        cls.zone_path = cls.scenario_dir / "central_zone.json"

    def test_demo_demand_is_3600_vehicles_per_hour(self) -> None:
        manifest = json.loads(
            (self.scenario_dir / "focused.manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["vehicles_per_hour"], 3600)

    def test_emergency_route_expands_to_every_controllable_tls(self) -> None:
        base = discover_area(
            self.net_path,
            requested_tls=load_zone_tls_ids(self.zone_path),
        )
        emergency = load_emergency_config(self.scenario_dir / "emergency.json")
        best, _logs = EmergencyRouter(
            FakeTraci(),
            base,
            self.net_path,
        ).find_alternatives(
            emergency.start.edge_id,
            emergency.destination.edge_id,
            emergency.base_vehicle_type_id,
            emergency.depart_time,
            num_alternatives=5,
        )
        self.assertIsNotNone(best)
        requested = tuple(dict.fromkeys((*base.tls_ids, *best.tls_sequence)))
        expanded = discover_area(
            self.net_path,
            requested_tls=requested,
            strict_requested=False,
        )

        self.assertTrue(set(base.tls_ids).issubset(expanded.tls_ids))
        self.assertTrue(set(best.tls_sequence).issubset(expanded.tls_ids))
        self.assertGreater(len(expanded.tls_ids), len(base.tls_ids))


if __name__ == "__main__":
    unittest.main()
