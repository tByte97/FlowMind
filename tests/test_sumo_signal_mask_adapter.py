from __future__ import annotations

import unittest
from dataclasses import dataclass

from flowmind.sumo_signal_mask_adapter import SumoSignalMaskAdapter


@dataclass
class FakePhase:
    duration: float
    state: str


@dataclass
class FakeLogic:
    programID: str
    currentPhaseIndex: int
    phases: list[FakePhase]


class FakeTrafficLight:
    def __init__(self) -> None:
        self.logic = FakeLogic(
            "validated",
            0,
            [
                FakePhase(10.0, "GG"),
                FakePhase(3.0, "yy"),
                FakePhase(2.0, "rr"),
            ],
        )
        self.installed: list[FakeLogic] = []
        self.phases: list[int] = []

    def getProgram(self, _tls_id: str) -> str:
        return "validated"

    def getAllProgramLogics(self, _tls_id: str) -> tuple[FakeLogic, ...]:
        return (self.logic,)

    def setProgramLogic(self, _tls_id: str, logic: FakeLogic) -> None:
        self.installed.append(logic)

    def setPhase(self, _tls_id: str, phase: int) -> None:
        self.phases.append(phase)


class FakeTraci:
    def __init__(self) -> None:
        self.trafficlight = FakeTrafficLight()


class SumoSignalMaskAdapterTest(unittest.TestCase):
    def test_mask_only_removes_green_and_restore_keeps_timing(self) -> None:
        traci = FakeTraci()
        adapter = SumoSignalMaskAdapter(traci)

        self.assertTrue(adapter.synchronize("tls", 0, {0: (0,)}))
        masked = traci.trafficlight.installed[-1]
        self.assertEqual([phase.state for phase in masked.phases], ["rG", "yy", "rr"])
        self.assertEqual([phase.duration for phase in masked.phases], [10.0, 3.0, 2.0])

        self.assertTrue(adapter.synchronize("tls", 0, {}))
        restored = traci.trafficlight.installed[-1]
        self.assertEqual([phase.state for phase in restored.phases], ["GG", "yy", "rr"])
        self.assertEqual(traci.trafficlight.phases, [0, 0])

    def test_restore_reinstalls_base_logic_after_mask(self) -> None:
        traci = FakeTraci()
        adapter = SumoSignalMaskAdapter(traci)

        self.assertTrue(adapter.synchronize("tls", 0, {0: (1,)}))
        self.assertTrue(adapter.restore("tls", 2))

        restored = traci.trafficlight.installed[-1]
        self.assertEqual([phase.state for phase in restored.phases], ["GG", "yy", "rr"])
        self.assertEqual(restored.currentPhaseIndex, 2)
        self.assertEqual(traci.trafficlight.phases[-1], 2)

    def test_non_green_mask_is_rejected(self) -> None:
        traci = FakeTraci()
        adapter = SumoSignalMaskAdapter(traci)

        self.assertFalse(adapter.synchronize("tls", 0, {1: (0,)}))
        self.assertEqual(traci.trafficlight.installed, [])


if __name__ == "__main__":
    unittest.main()
