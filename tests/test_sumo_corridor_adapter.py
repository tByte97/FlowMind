from __future__ import annotations

import unittest

from flowmind.sumo_corridor_adapter import SumoCorridorObservationAdapter


class FakeVehicleDomain:
    def __init__(self) -> None:
        self.active = True
        self.next_tls: list[tuple[str, int, float, str]] = []

    def getIDList(self) -> tuple[str, ...]:
        return ("ambulance",) if self.active else ()

    def getNextTLS(self, _vehicle_id: str) -> list[tuple[str, int, float, str]]:
        return self.next_tls


class FakeTraci:
    def __init__(self) -> None:
        self.vehicle = FakeVehicleDomain()


class SumoCorridorObservationAdapterTest(unittest.TestCase):
    def test_confirms_only_an_armed_stop_line_passage(self) -> None:
        traci = FakeTraci()
        adapter = SumoCorridorObservationAdapter(traci, "ambulance")

        traci.vehicle.next_tls = [("tls-a", 1, 100.0, "r")]
        self.assertEqual(adapter.observe().passed_tls_ids, ())
        traci.vehicle.next_tls = [("tls-a", 1, 20.0, "G")]
        self.assertEqual(adapter.observe().passed_tls_ids, ())
        traci.vehicle.next_tls = [("tls-b", 2, 200.0, "r")]
        observation = adapter.observe()

        self.assertEqual(observation.passed_tls_ids, ("tls-a",))
        self.assertEqual(observation.upcoming_tls, (("tls-b", 2, 200.0),))

    def test_tls_disappearance_far_from_stop_line_is_not_a_passage(self) -> None:
        traci = FakeTraci()
        adapter = SumoCorridorObservationAdapter(traci, "ambulance")
        traci.vehicle.next_tls = [("tls-a", 1, 500.0, "r")]
        adapter.observe()
        traci.vehicle.next_tls = [("tls-b", 2, 400.0, "r")]

        self.assertEqual(adapter.observe().passed_tls_ids, ())


if __name__ == "__main__":
    unittest.main()
