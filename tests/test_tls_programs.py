from __future__ import annotations

import unittest

from sumolib.net import Phase
from traci import constants as tc
from traci._trafficlight import Logic

from flowmind.tls_programs import (
    STATIC_FIXED_PROGRAM_ID,
    activate_static_fixed_programs,
)


class _FakeTrafficLightDomain:
    def __init__(
        self,
        programs: dict[str, tuple[Logic, ...]],
        active_programs: dict[str, str],
        phases: dict[str, int],
    ) -> None:
        self.programs = programs
        self.active_programs = active_programs
        self.phases = phases
        self.installed: list[tuple[str, Logic]] = []
        self.activated: list[tuple[str, str]] = []

    def getProgram(self, tls_id: str) -> str:
        return self.active_programs[tls_id]

    def getAllProgramLogics(self, tls_id: str) -> tuple[Logic, ...]:
        return self.programs[tls_id]

    def getPhase(self, tls_id: str) -> int:
        return self.phases[tls_id]

    def setProgramLogic(self, tls_id: str, logic: Logic) -> None:
        existing = tuple(
            item
            for item in self.programs[tls_id]
            if item.programID != logic.programID
        )
        self.programs[tls_id] = (*existing, logic)
        self.installed.append((tls_id, logic))

    def setProgram(self, tls_id: str, program_id: str) -> None:
        self.active_programs[tls_id] = program_id
        self.activated.append((tls_id, program_id))


class _FakeConnection:
    def __init__(self, trafficlight: _FakeTrafficLightDomain) -> None:
        self.trafficlight = trafficlight


def _actuated_logic(program_id: str = "actuated") -> Logic:
    return Logic(
        program_id,
        tc.TRAFFICLIGHT_TYPE_ACTUATED,
        0,
        (
            Phase(6, "GGrr", minDur=13, maxDur=50, name="east-west"),
            Phase(3, "yyrr", minDur=-1, maxDur=-1, name="yellow"),
            Phase(42, "rrGG", minDur=5, maxDur=50, name="north-south"),
        ),
    )


class StaticFixedProgramTest(unittest.TestCase):
    def test_actuated_program_is_cloned_as_deterministic_static_program(self) -> None:
        trafficlight = _FakeTrafficLightDomain(
            {"I-01": (_actuated_logic(),)},
            {"I-01": "actuated"},
            {"I-01": 2},
        )

        activations = activate_static_fixed_programs(
            _FakeConnection(trafficlight),
            ("I-01",),
        )

        self.assertEqual(len(activations), 1)
        activation = activations[0]
        self.assertEqual(activation.source_program_id, "actuated")
        self.assertEqual(activation.source_program_type, tc.TRAFFICLIGHT_TYPE_ACTUATED)
        self.assertEqual(activation.program_id, STATIC_FIXED_PROGRAM_ID)
        self.assertEqual(activation.program_type, tc.TRAFFICLIGHT_TYPE_STATIC)
        self.assertEqual(activation.current_phase, 2)
        self.assertEqual(activation.phase_durations, (13.0, 3.0, 42.0))
        self.assertEqual(
            trafficlight.activated,
            [("I-01", STATIC_FIXED_PROGRAM_ID)],
        )

        installed = trafficlight.installed[0][1]
        self.assertEqual(installed.type, tc.TRAFFICLIGHT_TYPE_STATIC)
        self.assertEqual(installed.currentPhaseIndex, 2)
        self.assertEqual(
            tuple(phase.state for phase in installed.getPhases()),
            ("GGrr", "yyrr", "rrGG"),
        )
        for phase in installed.getPhases():
            self.assertEqual(phase.minDur, phase.duration)
            self.assertEqual(phase.maxDur, phase.duration)
            self.assertEqual(phase.next, ())

    def test_all_programs_are_validated_before_any_tls_is_changed(self) -> None:
        trafficlight = _FakeTrafficLightDomain(
            {
                "I-01": (_actuated_logic(),),
                "I-02": (_actuated_logic("other"),),
            },
            {"I-01": "actuated", "I-02": "missing"},
            {"I-01": 0, "I-02": 0},
        )

        with self.assertRaisesRegex(RuntimeError, "I-02.*matched 0"):
            activate_static_fixed_programs(
                _FakeConnection(trafficlight),
                ("I-01", "I-02"),
            )

        self.assertEqual(trafficlight.installed, [])
        self.assertEqual(trafficlight.activated, [])

    def test_invalid_phase_safety_envelope_is_rejected(self) -> None:
        invalid = Logic(
            "actuated",
            tc.TRAFFICLIGHT_TYPE_ACTUATED,
            0,
            (Phase(10, "GGrr", minDur=20, maxDur=15),),
        )
        trafficlight = _FakeTrafficLightDomain(
            {"I-01": (invalid,)},
            {"I-01": "actuated"},
            {"I-01": 0},
        )

        with self.assertRaisesRegex(RuntimeError, "minDur 20.0 exceeds maxDur 15.0"):
            activate_static_fixed_programs(
                _FakeConnection(trafficlight),
                ("I-01",),
            )


if __name__ == "__main__":
    unittest.main()
