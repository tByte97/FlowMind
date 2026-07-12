from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.controller import AreaSignalController


class FakeLaneDomain:
    counts = {"north": 0, "south": 0, "east": 10, "west": 0}

    def getLength(self, _lane_id: str) -> float:
        return 120.0

    def getLastStepVehicleIDs(self, _lane_id: str) -> tuple[str, ...]:
        return ()

    def getLastStepVehicleNumber(self, lane_id: str) -> int:
        return self.counts[lane_id]

    def getLastStepHaltingNumber(self, lane_id: str) -> int:
        return self.counts[lane_id]

    def getLastStepOccupancy(self, lane_id: str) -> float:
        return self.counts[lane_id] / 16.0 * 100.0

    def getLastStepMeanSpeed(self, _lane_id: str) -> float:
        return 0.0


class FakeVehicleDomain:
    def getIDList(self) -> tuple[str, ...]:
        return ()


class FakeTrafficLightDomain:
    def __init__(self) -> None:
        self.phase = 0
        self.spent = 12.0
        self.phase_durations: list[tuple[str, float]] = []

    def getPhase(self, _tls_id: str) -> int:
        return self.phase

    def getSpentDuration(self, _tls_id: str) -> float:
        return self.spent

    def setPhase(self, _tls_id: str, phase: int) -> None:
        self.phase = phase

    def setPhaseDuration(self, tls_id: str, duration: float) -> None:
        self.phase_durations.append((tls_id, duration))


class FakeTraci:
    def __init__(self) -> None:
        self.lane = FakeLaneDomain()
        self.vehicle = FakeVehicleDomain()
        self.trafficlight = FakeTrafficLightDomain()


class AreaSignalControllerTest(unittest.TestCase):
    def test_entering_clearance_applies_real_sumo_phase_duration(self) -> None:
        traci = FakeTraci()
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls",
                    position=(0.0, 0.0),
                    phases=("Gr", "yr", "rG", "ry"),
                    links=(
                        ControlledLink("north", "south", 0),
                        ControlledLink("east", "west", 1),
                    ),
                    phase_durations=(6.0, 3.0, 6.0, 3.0),
                ),
            )
        )
        controller = AreaSignalController(
            traci,
            area,
            "local",
            ControlConfig(clearance_seconds=5),
        )

        controller.step(12.0)

        self.assertEqual(traci.trafficlight.phase, 1)
        self.assertEqual(traci.trafficlight.phase_durations, [("tls", 3.0)])


if __name__ == "__main__":
    unittest.main()
