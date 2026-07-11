from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import traci

import flowmind.experiment as experiment
from flowmind.config import RunConfig
from flowmind.experiment import configure_projection_data


class ExperimentEnvironmentTest(unittest.TestCase):
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

            path = experiment.write_live_run_status(config, "starting")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["system"]["simulation"]["status"], "starting")
        self.assertEqual(payload["system"]["websocket"]["port"], 9876)
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


if __name__ == "__main__":
    unittest.main()
