from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import traci

import flowmind.experiment as experiment
from experiments.run_experiment import build_parser
from flowmind.config import CONTROL_MODES, RunConfig
from flowmind.experiment import configure_projection_data
from flowmind.tls_programs import ActiveTlsProgram
from flowmind.tls_safety import TlsSafetyReport


class ExperimentEnvironmentTest(unittest.TestCase):
    def test_telemetry_failures_are_isolated_from_control_loop(self) -> None:
        class BrokenPublisher:
            @staticmethod
            def start() -> None:
                raise RuntimeError("port busy")

        class BrokenMetrics:
            @staticmethod
            def write_live_status(*_args: object, **_kwargs: object) -> None:
                raise OSError("dashboard storage unavailable")

        self.assertEqual(
            experiment.start_publisher_resilient(BrokenPublisher()),  # type: ignore[arg-type]
            "port busy",
        )
        self.assertFalse(
            experiment.write_live_status_resilient(BrokenMetrics())  # type: ignore[arg-type]
        )

    def test_all_explicit_control_modes_are_accepted(self) -> None:
        self.assertEqual(
            tuple(RunConfig(mode=mode).mode for mode in CONTROL_MODES),
            CONTROL_MODES,
        )

    def test_legacy_fixed_mode_is_normalized_to_static_fixed(self) -> None:
        self.assertEqual(RunConfig(mode="fixed").mode, "static_fixed")

    def test_unknown_control_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Expected one of"):
            RunConfig(mode="automatic")

    def test_cli_exposes_all_canonical_modes_and_legacy_alias(self) -> None:
        parser = build_parser()
        for mode in (*CONTROL_MODES, "fixed"):
            with self.subTest(mode=mode):
                self.assertEqual(parser.parse_args([mode]).mode, mode)

    def test_ml_control_requires_explicit_cli_opt_in(self) -> None:
        parser = build_parser()

        self.assertFalse(parser.parse_args(["flowmind"]).enable_queue_control)
        self.assertTrue(
            parser.parse_args(
                ["flowmind", "--enable-queue-control"]
            ).enable_queue_control
        )

    def test_packaged_projection_database_is_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            projection_dir = configure_projection_data()

            self.assertIsNotNone(projection_dir)
            self.assertTrue((projection_dir / "proj.db").is_file())
            self.assertEqual(os.environ["PROJ_DATA"], str(projection_dir))
            self.assertEqual(os.environ["PROJ_LIB"], str(projection_dir))

    def test_sumo_start_uses_finite_retries(self) -> None:
        with patch("flowmind.experiment.traci.start") as start_mock:
            experiment.start_sumo(["sumo", "-c", "scenario.sumocfg"])

        start_mock.assert_called_once()
        self.assertEqual(
            start_mock.call_args.kwargs.get("numRetries"),
            experiment.SUMO_START_RETRIES,
        )

    def test_sumo_gui_start_uses_longer_retries(self) -> None:
        with patch("flowmind.experiment.traci.start") as start_mock:
            experiment.start_sumo(["/opt/sumo/bin/sumo-gui", "-c", "scenario.sumocfg"])

        start_mock.assert_called_once()
        self.assertEqual(
            start_mock.call_args.kwargs.get("numRetries"),
            experiment.SUMO_GUI_START_RETRIES,
        )

    def test_sumo_start_error_includes_log_tail(self) -> None:
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "sumo.log"
            log_path.write_text("first line\nfatal scenario error\n", encoding="utf-8")
            command = ["sumo", "--log", str(log_path)]

            with patch(
                "flowmind.experiment.traci.start",
                side_effect=traci.FatalTraCIError("could not connect"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "fatal scenario error",
                ):
                    experiment.start_sumo(command)

    def test_live_run_marker_hides_previous_completed_state(self) -> None:
        with TemporaryDirectory() as directory:
            config = RunConfig(
                mode="flowmind",
                results_dir=Path(directory),
                websocket_port=9876,
            )

            previous = config.results_dir / "live_status.json"
            previous.write_text(
                json.dumps(
                    {
                        "system": {
                            "tls_programs": {
                                "status": "audited",
                                "count": 1,
                                "items": [{"tls_id": "old"}],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            path = experiment.write_live_run_status(config, "starting")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["system"]["simulation"]["status"], "starting")
        self.assertEqual(payload["system"]["websocket"]["port"], 9876)
        self.assertEqual(payload["system"]["tls_programs"]["status"], "waiting")
        self.assertEqual(payload["system"]["tls_programs"]["items"], [])
        self.assertEqual(payload["metric_history"], [])

    def test_failed_run_marker_disables_existing_zone_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            config = RunConfig(
                mode="flowmind",
                results_dir=Path(directory),
            )
            live_status = config.results_dir / "live_status.json"
            live_status.write_text(
                json.dumps(
                    {
                        "zone_simulation": {
                            "status": "running",
                            "active": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            path = experiment.write_live_run_status(config, "failed", "test error")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["system"]["simulation"]["status"], "failed")
        self.assertEqual(payload["zone_simulation"]["status"], "failed")
        self.assertFalse(payload["zone_simulation"]["active"])

    def test_live_status_distinguishes_non_adaptive_baselines(self) -> None:
        class Simulation:
            @staticmethod
            def getMinExpectedNumber() -> int:
                return 12

        class Connection:
            simulation = Simulation()

        class Publisher:
            client_count = 0

        for mode in ("static_fixed", "sumo_actuated"):
            with self.subTest(mode=mode):
                status = experiment.build_live_system_status(
                    config=RunConfig(mode=mode),
                    connection=Connection(),
                    controller=None,
                    corridor_manager=None,
                    publisher=Publisher(),
                    queue_forecast=None,
                    running=True,
                )

                self.assertEqual(status["controller"]["status"], mode)
                self.assertEqual(status["controller"]["mode"], mode)

    def test_live_status_exposes_startup_tls_program_audit(self) -> None:
        class Simulation:
            @staticmethod
            def getMinExpectedNumber() -> int:
                return 1

        class Connection:
            simulation = Simulation()

        class Publisher:
            client_count = 0

        program = ActiveTlsProgram(
            tls_id="I-01",
            program_id="0",
            program_type=3,
            program_type_name="actuated",
            current_phase=1,
            phase_count=4,
        )
        safety_report = TlsSafetyReport(
            source="test",
            tls_count=1,
            plan_count=2,
            movement_count=8,
            conflict_count=12,
            issues=(),
        )

        status = experiment.build_live_system_status(
            config=RunConfig(mode="sumo_actuated"),
            connection=Connection(),
            controller=None,
            corridor_manager=None,
            publisher=Publisher(),
            queue_forecast=None,
            running=True,
            active_tls_programs=(program,),
            tls_safety_report=safety_report,
        )

        self.assertEqual(status["tls_programs"]["status"], "audited")
        self.assertEqual(status["tls_programs"]["items"][0]["tls_id"], "I-01")
        self.assertEqual(
            status["tls_programs"]["items"][0]["program_type_name"],
            "actuated",
        )
        self.assertEqual(status["tls_safety"]["status"], "valid")
        self.assertEqual(status["tls_safety"]["plans"], 2)
        self.assertEqual(status["tls_safety"]["conflicts"], 12)


if __name__ == "__main__":
    unittest.main()
