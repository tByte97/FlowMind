from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import web_dashboard


class WebDashboardApiTests(unittest.TestCase):
    def test_discover_result_sets_reads_live_status_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run_01"
            run_dir.mkdir()
            (run_dir / "live_status.json").write_text(
                json.dumps({"mode": "flowmind"}),
                encoding="utf-8",
            )
            (run_dir / "summary.csv").write_text(
                "mode,average_waiting_time\nfixed,30\nflowmind,18\n",
                encoding="utf-8",
            )

            results = web_dashboard.discover_result_sets(root)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["mode"], "flowmind")
        self.assertEqual(results[0]["modes"], ["fixed", "flowmind"])
        self.assertTrue(results[0]["has_live_status"])
        self.assertTrue(results[0]["has_summary"])

    def test_build_status_payload_trims_large_live_arrays(self) -> None:
        old_results_dir = web_dashboard.RESULTS_DIR
        old_web_results_dir = web_dashboard.WEB_RESULTS_DIR
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run_02"
            run_dir.mkdir()
            payload = {
                "mode": "flowmind",
                "metric_history": [{"time": index} for index in range(220)],
                "decision_log": [{"time": index} for index in range(100)],
                "vehicles": [{"id": index} for index in range(150)],
                "lanes": [{"id": index} for index in range(150)],
                "intersections": [{"id": index} for index in range(150)],
            }
            (run_dir / "live_status.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            web_dashboard.RESULTS_DIR = root
            web_dashboard.WEB_RESULTS_DIR = root / "web"
            try:
                status = web_dashboard.build_status_payload(
                    web_dashboard.DemoProcessManager()
                )
            finally:
                web_dashboard.RESULTS_DIR = old_results_dir
                web_dashboard.WEB_RESULTS_DIR = old_web_results_dir

        self.assertTrue(status["available"])
        self.assertEqual(len(status["metric_history"]), 180)
        self.assertEqual(status["metric_history"][0]["time"], 40)
        self.assertEqual(len(status["decision_log"]), 80)
        self.assertEqual(len(status["vehicles"]), 120)
        self.assertEqual(len(status["lanes"]), 120)
        self.assertEqual(len(status["intersections"]), 120)

    def test_build_demo_command_uses_headless_web_defaults(self) -> None:
        command, results_dir = web_dashboard.build_demo_command(
            {
                "duration": 30,
                "seed": 7,
                "sensor_range": 900,
                "emergency_depart": 999,
                "baseline": False,
            }
        )

        self.assertIn("--headless", command)
        self.assertIn("--no-dashboard", command)
        self.assertIn("--no-baseline", command)
        self.assertIn("--sensor-range", command)
        self.assertEqual(command[command.index("--duration") + 1], "60")
        self.assertEqual(command[command.index("--sensor-range") + 1], "500.0")
        self.assertEqual(command[command.index("--emergency-depart") + 1], "59.0")
        self.assertTrue(results_dir.name.endswith("_seed_7"))


if __name__ == "__main__":
    unittest.main()
