from __future__ import annotations

import unittest

from sumolib.net import Phase
from traci import constants as tc
from traci._trafficlight import Logic

from flowmind.area_model import load_zone_tls_ids
from flowmind.config import PROJECT_ROOT
from flowmind.sumo_tls_adapter import SumoTlsSafetyAdapter
from flowmind.tls_safety import validate_tls_catalog


SCENARIO_DIR = PROJECT_ROOT / "simulation" / "rivne_area"


class FakeTrafficLight:
    def __init__(self, states: dict[str, tuple[str, ...]]) -> None:
        self._states = states

    def getProgram(self, _tls_id: str) -> str:
        return "0"

    def getPhase(self, _tls_id: str) -> int:
        return 0

    def getAllProgramLogics(self, tls_id: str) -> tuple[Logic, ...]:
        phases = tuple(
            Phase(10 if "G" in state or "g" in state else 3, state)
            for state in self._states[tls_id]
        )
        return (
            Logic("0", tc.TRAFFICLIGHT_TYPE_STATIC, 0, phases),
        )


class FakeTraci:
    def __init__(self, states: dict[str, tuple[str, ...]]) -> None:
        self.trafficlight = FakeTrafficLight(states)


class SumoTlsSafetyAdapterTest(unittest.TestCase):
    def test_sumo_is_translated_to_neutral_catalog(self) -> None:
        tls_ids = load_zone_tls_ids(SCENARIO_DIR / "central_zone.json")
        from flowmind.area_model import discover_area

        area = discover_area(
            SCENARIO_DIR / "osm.net.xml.gz",
            requested_tls=tls_ids,
        )
        states = {item.tls_id: item.phases for item in area.intersections}
        catalog = SumoTlsSafetyAdapter(
            SCENARIO_DIR / "osm.net.xml.gz",
            FakeTraci(states),
        ).load_catalog(tls_ids)

        self.assertEqual(catalog.source.split(":", 1)[0], "sumo")
        self.assertEqual(len(catalog.intersections), 6)
        self.assertTrue(all(item.movements for item in catalog.intersections))
        self.assertTrue(all(item.conflicts for item in catalog.intersections))
        self.assertTrue(all(item.plans for item in catalog.intersections))

        report = validate_tls_catalog(catalog)
        self.assertTrue(report.valid, [item.as_payload() for item in report.issues])


if __name__ == "__main__":
    unittest.main()
