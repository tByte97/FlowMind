from __future__ import annotations

import unittest

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
    def getSpeed(self, _vehicle_id: str) -> float:
        return 0.0

    def getAccumulatedWaitingTime(self, _vehicle_id: str) -> float:
        return 0.0


class FakeTraci:
    def __init__(self) -> None:
        self.simulation = FakeSimulation()
        self.lane = FakeLane()
        self.vehicle = FakeVehicle()


class MetricsCollectorTest(unittest.TestCase):
    def test_lifecycle_events_are_counted_between_metric_samples(self) -> None:
        traci = FakeTraci()
        collector = MetricsCollector(
            traci, AreaModel(()), ControlConfig(decision_interval=5)
        )

        traci.simulation.departed = ("veh1",)
        collector.collect(1.0)
        traci.simulation.departed = ()
        traci.simulation.arrived = ("veh1",)
        collector.collect(3.0)
        traci.simulation.arrived = ()
        collector.collect(5.0)

        summary = collector.summary("fixed", 5.0)
        self.assertEqual(summary["throughput"], 1)
        self.assertEqual(summary["average_travel_time"], 2.0)
        self.assertEqual(len(collector.samples), 1)


if __name__ == "__main__":
    unittest.main()
