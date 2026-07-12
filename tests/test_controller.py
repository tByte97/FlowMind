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
    @staticmethod
    def area() -> AreaModel:
        return AreaModel(
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

    def test_entering_clearance_applies_real_sumo_phase_duration(self) -> None:
        traci = FakeTraci()
        controller = AreaSignalController(
            traci,
            self.area(),
            "local",
            ControlConfig(clearance_seconds=5),
        )

        controller.step(12.0)

        self.assertEqual(traci.trafficlight.phase, 1)
        self.assertEqual(traci.trafficlight.phase_durations, [("tls", 3.0)])

    def test_all_blocked_candidates_close_current_green(self) -> None:
        traci = FakeTraci()
        traci.lane.counts = {
            "north": 10,
            "south": 16,
            "east": 10,
            "west": 16,
        }
        controller = AreaSignalController(
            traci,
            self.area(),
            "flowmind",
            ControlConfig(),
        )

        controller.step(12.0)

        self.assertEqual(traci.trafficlight.phase, 1)
        self.assertEqual(controller.stats.scoreless_skips, 1)
        self.assertEqual(controller.stats.advances, 1)

    def test_all_intersections_are_prepared_before_first_tls_write(self) -> None:
        class MultiTrafficLightDomain:
            def __init__(self) -> None:
                self.events: list[str] = []
                self.phases = {"tls-a": 0, "tls-b": 0}

            def getPhase(self, tls_id: str) -> int:
                self.events.append(f"read:{tls_id}")
                return self.phases[tls_id]

            def getSpentDuration(self, _tls_id: str) -> float:
                return 12.0

            def setPhase(self, tls_id: str, phase: int) -> None:
                self.events.append(f"write:{tls_id}")
                self.phases[tls_id] = phase

            def setPhaseDuration(self, _tls_id: str, _duration: float) -> None:
                return None

        traci = FakeTraci()
        traci.trafficlight = MultiTrafficLightDomain()
        traci.lane.counts = {
            "a-north": 0,
            "a-south": 0,
            "a-east": 10,
            "a-west": 0,
            "b-north": 0,
            "b-south": 0,
            "b-east": 10,
            "b-west": 0,
        }
        area = AreaModel(
            tuple(
                Intersection(
                    tls_id=f"tls-{suffix}",
                    position=(0.0, 0.0),
                    phases=("Gr", "yr", "rG", "ry"),
                    links=(
                        ControlledLink(f"{suffix}-north", f"{suffix}-south", 0),
                        ControlledLink(f"{suffix}-east", f"{suffix}-west", 1),
                    ),
                )
                for suffix in ("a", "b")
            )
        )
        controller = AreaSignalController(
            traci,
            area,
            "flowmind",
            ControlConfig(),
        )

        controller.step(12.0)

        events = traci.trafficlight.events
        self.assertLess(events.index("read:tls-b"), events.index("write:tls-a"))
        self.assertEqual(traci.trafficlight.phases, {"tls-a": 1, "tls-b": 1})


if __name__ == "__main__":
    unittest.main()
