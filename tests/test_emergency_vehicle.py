from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.emergency_vehicle import (
    EmergencyVehicleManager,
    load_emergency_config,
)


class FakeStage:
    edges = ("start", "middle", "hospital")
    length = 2_500.0
    travelTime = 180.0


class FakeVehicleType:
    def __init__(self) -> None:
        self.ids = ["zone_passenger"]
        self.calls: list[tuple[object, ...]] = []

    def getIDList(self) -> list[str]:
        return self.ids

    def copy(self, source: str, destination: str) -> None:
        self.ids.append(destination)
        self.calls.append(("copy", source, destination))

    def __getattr__(self, name: str):
        def record(*args: object) -> None:
            self.calls.append((name, *args))

        return record


class FakeRoute:
    def __init__(self) -> None:
        self.added: tuple[str, tuple[str, ...]] | None = None

    def getIDList(self) -> tuple[str, ...]:
        return ()

    def add(self, route_id: str, edges: tuple[str, ...]) -> None:
        self.added = (route_id, edges)


class FakeVehicle:
    def __init__(self) -> None:
        self.added: tuple[object, ...] | None = None
        self.kwargs: dict[str, object] = {}

    def getLoadedIDList(self) -> tuple[str, ...]:
        return ()

    def add(self, *args: object, **kwargs: object) -> None:
        self.added = args
        self.kwargs = kwargs


class FakeSimulation:
    def findRoute(self, *_args: object) -> FakeStage:
        return FakeStage()


class FakeTraci:
    def __init__(self) -> None:
        self.vehicletype = FakeVehicleType()
        self.route = FakeRoute()
        self.vehicle = FakeVehicle()
        self.simulation = FakeSimulation()


class EmergencyVehicleTest(unittest.TestCase):
    def _config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "emergency.json"
            path.write_text(
                """
                {
                  "vehicle_id": "ambulance",
                  "route_id": "ambulance_route",
                  "vehicle_type_id": "ambulance_type",
                  "base_vehicle_type_id": "zone_passenger",
                  "depart_time": 180,
                  "start": {"name": "Station", "edge_id": "start"},
                  "destination": {"name": "Hospital", "edge_id": "hospital"},
                  "color": [255, 0, 0, 255]
                }
                """,
                encoding="utf-8",
            )
            return load_emergency_config(path)

    def test_load_and_override_emergency_config(self) -> None:
        config = self._config().with_overrides(
            depart_time=120, start_edge="new_start", destination_edge="new_hospital"
        )
        self.assertEqual(config.depart_time, 120)
        self.assertEqual(config.start.edge_id, "new_start")
        self.assertEqual(config.destination.edge_id, "new_hospital")

    def test_manager_creates_type_route_and_scheduled_vehicle(self) -> None:
        traci = FakeTraci()
        details = EmergencyVehicleManager(traci, self._config()).install()

        self.assertEqual(
            traci.route.added,
            ("ambulance_route", ("start", "middle", "hospital")),
        )
        self.assertEqual(
            traci.vehicle.added,
            ("ambulance", "ambulance_route"),
        )
        self.assertEqual(traci.vehicle.kwargs["depart"], "180.0")
        self.assertEqual(details.route_edge_count, 3)
        self.assertEqual(details.expected_travel_time, 180.0)


if __name__ == "__main__":
    unittest.main()
