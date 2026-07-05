from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.area_model import AreaModel
from flowmind.config import ControlConfig
from flowmind.metrics import MetricsCollector


class FakeSimulation:
    departed: tuple[str, ...] = ()
    arrived: tuple[str, ...] = ()

    def getDepartedIDList(self) -> tuple[str, ...]:
        return self.departed

    def getArrivedIDList(self) -> tuple[str, ...]:
        return self.arrived


class FakeLane:
    def getLastStepHaltingNumber(self, _lane_id: str) -> int:
        return 0

    def getLastStepOccupancy(self, _lane_id: str) -> float:
        return 0.0

    def getLastStepVehicleIDs(self, _lane_id: str) -> tuple[str, ...]:
        return ()


class FakeVehicle:
    def __init__(self) -> None:
        self.active: set[str] = set()

    def getSpeed(self, _vehicle_id: str) -> float:
        return 0.0

    def getAccumulatedWaitingTime(self, _vehicle_id: str) -> float:
        return 0.0

    def getIDList(self) -> tuple[str, ...]:
        return tuple(self.active)

    def getRoute(self, _vehicle_id: str) -> tuple[str, ...]:
        return ("start", "middle", "hospital")

    def getRouteIndex(self, _vehicle_id: str) -> int:
        return 1

    def getPosition(self, _vehicle_id: str) -> tuple[float, float]:
        return (10.0, 20.0)

    def getRoadID(self, _vehicle_id: str) -> str:
        return "middle"

    def getLaneID(self, _vehicle_id: str) -> str:
        return "middle_0"

    def getLanePosition(self, _vehicle_id: str) -> float:
        return 42.0


class FakeTraci:
    def __init__(self) -> None:
        self.simulation = FakeSimulation()
        self.lane = FakeLane()
        self.vehicle = FakeVehicle()


class MetricsCollectorTest(unittest.TestCase):
    def test_lifecycle_events_are_counted_between_metric_samples(self) -> None:
        traci = FakeTraci()
        collector = MetricsCollector(
            traci,
            AreaModel(()),
            ControlConfig(decision_interval=5),
            priority_vehicle="veh1",
        )

        traci.simulation.departed = ("veh1",)
        traci.vehicle.active = {"veh1"}
        collector.collect(1.0)
        traci.simulation.departed = ()
        traci.simulation.arrived = ("veh1",)
        collector.collect(3.0)
        traci.vehicle.active = set()
        traci.simulation.arrived = ()
        collector.collect(5.0)

        summary = collector.summary("fixed", 5.0)
        self.assertEqual(summary["throughput"], 1)
        self.assertEqual(summary["departed_vehicles"], 1)
        self.assertEqual(summary["peak_active_vehicles"], 1)
        self.assertEqual(summary["average_travel_time"], 2.0)
        self.assertEqual(summary["emergency_departure_time"], 1.0)
        self.assertEqual(summary["emergency_arrival_time"], 3.0)
        self.assertEqual(summary["emergency_eta"], 2.0)
        self.assertEqual(summary["emergency_trace_samples"], 2)
        self.assertEqual(collector.emergency_trace[0].edge_id, "middle")
        self.assertEqual(collector.emergency_trace[0].remaining_edges, 1)
        self.assertEqual(len(collector.samples), 1)

    def test_live_status_snapshot_is_written(self) -> None:
        traci = FakeTraci()
        collector = MetricsCollector(
            traci,
            AreaModel(()),
            ControlConfig(decision_interval=3),
            priority_vehicle=None,
        )

        collector.collect(3.0)

        with TemporaryDirectory() as temp_dir:
            output_path = collector.write_live_status(Path(temp_dir), "flowmind", 3.0)
            self.assertEqual(output_path, Path(temp_dir) / "live_status.json")
            self.assertTrue(output_path.exists())
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "flowmind")
            self.assertEqual(payload["simulated_time"], 3.0)
            self.assertEqual(payload["latest_sample"]["time"], 3.0)
            self.assertEqual(payload["summary"]["throughput"], 0)


if __name__ == "__main__":
    unittest.main()
