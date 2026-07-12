from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.safety_validator import SafetyValidator


class FakeTrafficLight:
    def __init__(self, spent: float = 12.0) -> None:
        self.spent = spent

    def getSpentDuration(self, _tls_id: str) -> float:
        return self.spent


class FakeTraci:
    def __init__(self, spent: float = 12.0) -> None:
        self.trafficlight = FakeTrafficLight(spent)


class SafetyValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.area = AreaModel(
            (
                Intersection(
                    tls_id="tls_0",
                    position=(0.0, 0.0),
                    phases=("Gr", "yr", "rG", "ry"),
                    links=(
                        ControlledLink("north_0", "south_0", 0),
                        ControlledLink("east_0", "west_0", 1),
                    ),
                ),
            )
        )
        self.config = ControlConfig()

    def test_allows_only_next_phase(self) -> None:
        validator = SafetyValidator(FakeTraci(), self.area, self.config)

        skipped = validator.validate_transition("tls_0", 0, 2, 12.0)
        next_phase = validator.validate_transition("tls_0", 0, 1, 12.0)

        self.assertFalse(skipped.allowed)
        self.assertEqual(skipped.reason, "phase skip is not allowed")
        self.assertTrue(next_phase.allowed)

    def test_rejects_advance_before_min_green(self) -> None:
        validator = SafetyValidator(FakeTraci(spent=4.0), self.area, self.config)

        decision = validator.validate_transition(
            "tls_0",
            0,
            1,
            4.0,
            spent_duration=4.0,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "min green not satisfied")

    def test_rejects_extension_after_max_green(self) -> None:
        validator = SafetyValidator(FakeTraci(spent=50.0), self.area, self.config)

        decision = validator.validate_extension(
            "tls_0",
            0,
            50.0,
            spent_duration=50.0,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max green reached")

    def test_limits_priority_override_duration(self) -> None:
        validator = SafetyValidator(
            FakeTraci(),
            self.area,
            ControlConfig(max_priority_override=5),
        )

        first = validator.validate_extension("tls_0", 0, 10.0, priority=True)
        expired = validator.validate_extension("tls_0", 0, 16.0, priority=True)

        self.assertTrue(first.allowed)
        self.assertFalse(expired.allowed)
        self.assertEqual(expired.reason, "priority override timeout")

    def test_uses_default_phase_duration_for_short_programs(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls_0",
                    position=(0.0, 0.0),
                    phases=("G", "y"),
                    links=(ControlledLink("north_0", "south_0", 0),),
                    phase_durations=(6.0, 3.0),
                ),
            )
        )
        validator = SafetyValidator(FakeTraci(spent=7.0), area, self.config)

        early = validator.validate_transition(
            "tls_0",
            0,
            1,
            5.0,
            spent_duration=5.0,
        )
        expired = validator.validate_extension(
            "tls_0",
            0,
            15.0,
            spent_duration=15.0,
        )

        self.assertFalse(early.allowed)
        self.assertEqual(early.reason, "min green not satisfied")
        self.assertFalse(expired.allowed)
        self.assertEqual(expired.reason, "max green reached")

    def test_sumo_min_dur_blocks_normal_and_priority_transitions(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls_0",
                    position=(0.0, 0.0),
                    phases=("G", "y"),
                    links=(ControlledLink("north_0", "south_0", 0),),
                    phase_durations=(6.0, 3.0),
                    phase_min_durations=(13.0, None),
                    phase_max_durations=(50.0, None),
                ),
            )
        )
        validator = SafetyValidator(FakeTraci(), area, self.config)

        normal = validator.validate_transition(
            "tls_0",
            0,
            1,
            12.0,
            spent_duration=12.0,
        )
        priority = validator.validate_transition(
            "tls_0",
            0,
            1,
            12.0,
            spent_duration=12.0,
            priority=True,
        )
        ready = validator.validate_transition(
            "tls_0",
            0,
            1,
            13.0,
            spent_duration=13.0,
        )

        self.assertFalse(normal.allowed)
        self.assertEqual(normal.reason, "SUMO minDur not satisfied")
        self.assertFalse(priority.allowed)
        self.assertEqual(priority.reason, "SUMO minDur not satisfied")
        self.assertTrue(ready.allowed)

    def test_uses_real_clearance_duration_before_config_fallback(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls_0",
                    position=(0.0, 0.0),
                    phases=("G", "y", "r"),
                    links=(ControlledLink("north_0", "south_0", 0),),
                    phase_durations=(6.0, 3.0, 4.0),
                ),
            )
        )
        validator = SafetyValidator(
            FakeTraci(spent=2.0),
            area,
            ControlConfig(clearance_seconds=5),
        )

        blocked = validator.validate_transition(
            "tls_0",
            1,
            2,
            2.0,
            spent_duration=2.0,
        )
        allowed = validator.validate_transition(
            "tls_0",
            1,
            2,
            3.0,
            spent_duration=3.0,
        )

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "yellow clearance not satisfied")
        self.assertTrue(allowed.allowed)


if __name__ == "__main__":
    unittest.main()
