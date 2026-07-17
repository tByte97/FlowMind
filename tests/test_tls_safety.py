from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.tls_safety import (
    ActivePlanExpectation,
    Movement,
    MovementConflict,
    SignalPhase,
    SignalPlan,
    TlsSafetyCatalog,
    TlsSafetyDefinition,
    validate_tls_catalog,
)
from flowmind.tls_safety_audit import write_tls_safety_startup_audit


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

    def test_runtime_plan_must_match_controller_model(self) -> None:
        item = definition(
            (
                SignalPhase("Gr", 10),
                SignalPhase("yr", 3),
                SignalPhase("rG", 10),
                SignalPhase("ry", 3),
            )
        )

        report = validate_tls_catalog(
            TlsSafetyCatalog((item,), source="controller"),
            {
                "I-01": ActivePlanExpectation(
                    program_id="another-plan",
                    phase_states=("Gr", "yr", "rG", "ry"),
                )
            },
        )

        self.assertIn(
            "active_program_mismatch",
            {issue.code for issue in report.issues},
        )

    def test_audit_contains_report_and_conflict_matrix(self) -> None:
        item = definition(
            (
                SignalPhase("Gr", 10),
                SignalPhase("yr", 3),
                SignalPhase("rG", 10),
                SignalPhase("ry", 3),
            )
        )
        catalog = TlsSafetyCatalog((item,), source="controller")
        report = validate_tls_catalog(catalog)

        with TemporaryDirectory() as directory:
            path = write_tls_safety_startup_audit(
                Path(directory),
                "flowmind",
                catalog,
                report,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(payload["report"]["valid"])
        self.assertEqual(
            payload["catalog"]["intersections"][0]["conflicts"][0]["reason"],
            "right_of_way_foe",
        )


if __name__ == "__main__":
    unittest.main()
