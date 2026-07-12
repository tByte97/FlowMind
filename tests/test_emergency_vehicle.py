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
        self.calls: list[tuple[object, ...]] = []

    def getLoadedIDList(self) -> tuple[str, ...]:
        return ()

    def add(self, *args: object, **kwargs: object) -> None:
        self.added = args
        self.kwargs = kwargs

    def __getattr__(self, name: str):
        def record(*args: object, **kwargs: object) -> None:
            self.calls.append((name, *args, kwargs))

        return record


class FakeLane:
    def getIDList(self) -> tuple[str, ...]:
        return ("start_0", "middle_0", "hospital_0")

    def getEdgeID(self, lane_id: str) -> str:
        return lane_id.removesuffix("_0")

    def getShape(self, lane_id: str) -> tuple[tuple[float, float], ...]:
        index = {"start_0": 0, "middle_0": 1, "hospital_0": 2}[lane_id]
        return ((float(index), 0.0), (float(index + 1), 0.0))


class FakePolygon:
    def __init__(self) -> None:
        self.added: list[tuple[object, ...]] = []
        self.colors: list[tuple[str, tuple[int, int, int, int]]] = []
        self.widths: list[tuple[str, float]] = []

    def getIDList(self) -> tuple[str, ...]:
        return ()

    def add(self, *args: object, **_kwargs: object) -> None:
        self.added.append(args)

    def setColor(
        self,
        polygon_id: str,
        color: tuple[int, int, int, int],
    ) -> None:
        self.colors.append((polygon_id, color))

    def setLineWidth(self, polygon_id: str, width: float) -> None:
        self.widths.append((polygon_id, width))


class FakePoi(FakePolygon):
    pass


class FakeSimulation:
    def findRoute(self, *_args: object) -> FakeStage:
        return FakeStage()


class FakeEdge:
    def __init__(self, ids: tuple[str, ...] = ("start", "middle", "hospital")):
        self.ids = ids

    def getIDList(self) -> tuple[str, ...]:
        return self.ids


class FakeTraci:
    def __init__(self) -> None:
        self.vehicletype = FakeVehicleType()
        self.edge = FakeEdge()
        self.route = FakeRoute()
        self.vehicle = FakeVehicle()
        self.simulation = FakeSimulation()
        self.lane = FakeLane()
        self.polygon = FakePolygon()
        self.poi = FakePoi()


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
        self.assertEqual(len(traci.polygon.added), 3)
        self.assertEqual(len(traci.poi.added), 2)
        self.assertEqual(details.route_edge_count, 3)
        self.assertEqual(details.route_edges, ("start", "middle", "hospital"))
        self.assertEqual(details.expected_travel_time, 180.0)
        self.assertEqual(details.predicted_eta, 180.0)

    def test_manager_preserves_preselected_route_metrics(self) -> None:
        traci = FakeTraci()
        details = EmergencyVehicleManager(traci, self._config()).install(
            precalculated_edges=("start", "alt", "hospital"),
            route_length=1_500.0,
            expected_travel_time=120.0,
            predicted_eta=150.0,
        )

        self.assertEqual(
            traci.route.added,
            ("ambulance_route", ("start", "alt", "hospital")),
        )
        self.assertEqual(details.route_length, 1_500.0)
        self.assertEqual(details.expected_travel_time, 120.0)
        self.assertEqual(details.predicted_eta, 150.0)

    def test_green_corridor_recolors_route_overlay(self) -> None:
        traci = FakeTraci()
        manager = EmergencyVehicleManager(traci, self._config())
        manager.install()

        manager.update_corridor_visualization("GREEN_WINDOW", "tls_1")

        self.assertEqual(len(traci.polygon.colors), 3)
        self.assertTrue(
            all(color == (20, 255, 90, 255) for _, color in traci.polygon.colors)
        )
        self.assertTrue(
            all(width == 9.0 for _, width in traci.polygon.widths)
        )

    def test_scheduled_route_can_be_reassessed_before_departure(self) -> None:
        traci = FakeTraci()
        manager = EmergencyVehicleManager(traci, self._config())
        manager.install()

        changed = manager.replace_scheduled_route(
            ("start", "alt", "hospital"),
            route_length=1_500.0,
            expected_travel_time=100.0,
            predicted_eta=120.0,
        )

        self.assertTrue(changed)
        self.assertIn(
            (
                "setRoute",
                "ambulance",
                ("start", "alt", "hospital"),
                {},
            ),
            traci.vehicle.calls,
        )
        self.assertEqual(manager.details.route_edges, ("start", "alt", "hospital"))
        self.assertEqual(manager.details.predicted_eta, 120.0)

    def test_manager_reports_edges_from_another_map(self) -> None:
        traci = FakeTraci()
        traci.edge = FakeEdge(("other_edge",))

        with self.assertRaisesRegex(
            ValueError,
            "absent from the current SUMO map.*start.*hospital",
        ):
            EmergencyVehicleManager(traci, self._config()).install()


if __name__ == "__main__":
    unittest.main()
