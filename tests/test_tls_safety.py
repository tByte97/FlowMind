from __future__ import annotations

import unittest

from flowmind.tls_safety import (
    Movement,
    MovementConflict,
    SignalPhase,
    SignalPlan,
    TlsSafetyCatalog,
    TlsSafetyDefinition,
    validate_tls_catalog,
)


def definition(
    phases: tuple[SignalPhase, ...],
    *,
    plans: tuple[SignalPlan, ...] | None = None,
) -> TlsSafetyDefinition:
    movements = (
        Movement("north_to_south", 0, "north", "south"),
        Movement("east_to_west", 1, "east", "west"),
    )
    return TlsSafetyDefinition(
        tls_id="I-01",
        signal_count=2,
        movements=movements,
        conflicts=(
            MovementConflict("north_to_south", "east_to_west"),
        ),
        plans=plans or (SignalPlan("day", "actuated", phases),),
        active_program_id="day",
        current_phase=0,
    )


class TlsSafetyValidationTest(unittest.TestCase):
    def test_safe_protected_plan_is_valid(self) -> None:
        report = validate_tls_catalog(
            TlsSafetyCatalog(
                (
                    definition(
                        (
                            SignalPhase("Gr", 10),
                            SignalPhase("yr", 3),
                            SignalPhase("rG", 10),
                            SignalPhase("ry", 3),
                        )
                    ),
                ),
                source="test",
            )
        )

        self.assertTrue(report.valid)
        self.assertEqual(report.error_count, 0)

    def test_conflicting_protected_greens_are_rejected(self) -> None:
        report = validate_tls_catalog(
            TlsSafetyCatalog(
                (definition((SignalPhase("GG", 10),)),),
                source="test",
            )
        )

        self.assertFalse(report.valid)
        self.assertIn(
            "conflicting_protected_greens",
            {issue.code for issue in report.issues},
        )

    def test_permissive_green_can_coexist_with_protected_green(self) -> None:
        report = validate_tls_catalog(
            TlsSafetyCatalog(
                (
                    definition(
                        (
                            SignalPhase("Gg", 10),
                            SignalPhase("yr", 3),
                            SignalPhase("rG", 10),
                            SignalPhase("ry", 3),
                        )
                    ),
                ),
                source="test",
            )
        )

        codes = {issue.code for issue in report.issues}
        self.assertNotIn("conflicting_protected_greens", codes)

    def test_every_plan_is_validated_not_only_active_one(self) -> None:
        safe = SignalPlan(
            "day",
            "actuated",
            (
                SignalPhase("Gr", 10),
                SignalPhase("yr", 3),
                SignalPhase("rG", 10),
                SignalPhase("ry", 3),
            ),
        )
        broken = SignalPlan("night", "static", (SignalPhase("GG", 10),))
        item = definition(safe.phases, plans=(safe, broken))

        report = validate_tls_catalog(
            TlsSafetyCatalog((item,), source="test")
        )

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                issue.program_id == "night"
                and issue.code == "conflicting_protected_greens"
                for issue in report.issues
            )
        )

    def test_invalid_state_width_and_next_phase_are_rejected(self) -> None:
        report = validate_tls_catalog(
            TlsSafetyCatalog(
                (
                    definition(
                        (
                            SignalPhase("G", 10, next_phases=(8,)),
                            SignalPhase("yr", 3),
                        )
                    ),
                ),
                source="test",
            )
        )

        codes = {issue.code for issue in report.issues}
        self.assertIn("state_length_mismatch", codes)
        self.assertIn("next_phase_out_of_range", codes)

    def test_direct_protected_green_to_red_is_rejected(self) -> None:
        report = validate_tls_catalog(
            TlsSafetyCatalog(
                (
                    definition(
                        (
                            SignalPhase("Gr", 10),
                            SignalPhase("rG", 10),
                        )
                    ),
                ),
                source="test",
            )
        )

        codes = {issue.code for issue in report.issues}
        self.assertIn("protected_green_without_yellow", codes)

    def test_green_during_conflicting_yellow_is_rejected(self) -> None:
        report = validate_tls_catalog(
            TlsSafetyCatalog(
                (
                    definition(
                        (
                            SignalPhase("Gr", 10),
                            SignalPhase("yG", 3),
                            SignalPhase("rG", 10),
                            SignalPhase("ry", 3),
                        )
                    ),
                ),
                source="test",
            )
        )

        self.assertIn(
            "green_during_conflicting_clearance",
            {issue.code for issue in report.issues},
        )


if __name__ == "__main__":
    unittest.main()
