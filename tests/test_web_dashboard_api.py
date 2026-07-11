from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import web_dashboard


class WebDashboardApiTests(unittest.TestCase):
    def test_dashboard_includes_live_only_sumo_zone_simulation(self) -> None:
        page = web_dashboard.DESIGN_PAGE

        self.assertIn('id="zoneSimulation"', page)
        self.assertIn('/assets/zone-simulation.css', page)
        self.assertIn('/assets/zone-simulation.js', page)
        self.assertIn('FlowMindZoneSimulation?.setSnapshot(payload)', page)
        self.assertIn('Mock-режим вимкнено.', page)
        self.assertIn('Кожне контрольоване SUMO-перехрестя показане окремо', page)
        self.assertIn('щоб відстежувати його live', page)
        self.assertIn('Авто: &lt;5', page)
        self.assertIn('Авто: ≥10', page)

    def test_stop_marker_disables_live_sumo_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_dir = Path(directory)
            status_path = result_dir / "live_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "system": {"simulation": {"status": "running"}},
                        "zone_simulation": {"status": "running", "active": True},
                    }
                ),
                encoding="utf-8",
            )

            web_dashboard.mark_live_snapshot_inactive(result_dir)

            payload = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["system"]["simulation"]["status"], "stopped")
        self.assertEqual(payload["zone_simulation"]["status"], "stopped")
        self.assertFalse(payload["zone_simulation"]["active"])

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

    def test_archive_payload_exposes_result_ids_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run_03"
            run_dir.mkdir()
            (run_dir / "summary.csv").write_text(
                (
                    "mode,average_waiting_time,average_queue_length,"
                    "throughput,sensor_range_meters\n"
                    "flowmind,12.5,4.25,80,120\n"
                ),
                encoding="utf-8",
            )

            payload = web_dashboard.build_archive_payload(root)

        self.assertEqual(payload["total"], 1)
        result = payload["results"][0]
        self.assertTrue(result["id"])
        self.assertEqual(result["mode"], "flowmind")
        self.assertEqual(result["summary"]["average_waiting_time"], 12.5)
        self.assertEqual(result["summary"]["throughput"], 80)

    def test_result_detail_reads_archived_live_status_by_id(self) -> None:
        old_results_dir = web_dashboard.RESULTS_DIR
        old_web_results_dir = web_dashboard.WEB_RESULTS_DIR
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run_04"
            run_dir.mkdir()
            (run_dir / "live_status.json").write_text(
                json.dumps(
                    {
                        "mode": "local",
                        "summary": {"mode": "local", "throughput": 33},
                        "metric_history": [{"time": 1}],
                    }
                ),
                encoding="utf-8",
            )
            result_id = web_dashboard._result_id(run_dir)
            web_dashboard.RESULTS_DIR = root
            web_dashboard.WEB_RESULTS_DIR = root / "web"
            try:
                detail = web_dashboard.build_result_detail_payload(result_id)
            finally:
                web_dashboard.RESULTS_DIR = old_results_dir
                web_dashboard.WEB_RESULTS_DIR = old_web_results_dir

        self.assertTrue(detail["available"])
        self.assertEqual(detail["mode"], "local")
        self.assertEqual(detail["summary"]["throughput"], 33)
        self.assertEqual(detail["metric_history"], [{"time": 1}])

    def test_averages_payload_groups_numeric_metrics_by_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "run_05"
            second = root / "run_06"
            third = root / "run_07"
            first.mkdir()
            second.mkdir()
            third.mkdir()
            (first / "summary.csv").write_text(
                "mode,average_waiting_time,throughput\nflowmind,10,100\n",
                encoding="utf-8",
            )
            (second / "summary.csv").write_text(
                "mode,average_waiting_time,throughput\nflowmind,20,140\n",
                encoding="utf-8",
            )
            (third / "summary.csv").write_text(
                "mode,average_waiting_time,throughput\nfixed,30,90\n",
                encoding="utf-8",
            )

            payload = web_dashboard.build_averages_payload(root)

        by_mode = {item["mode"]: item for item in payload["modes"]}
        self.assertEqual(payload["total_results"], 3)
        self.assertEqual(payload["total_rows"], 3)
        self.assertEqual(by_mode["flowmind"]["count"], 2)
        self.assertEqual(
            by_mode["flowmind"]["metrics"]["average_waiting_time"]["average"],
            15.0,
        )
        self.assertEqual(
            by_mode["flowmind"]["metrics"]["throughput"]["average"],
            120.0,
        )
        self.assertEqual(by_mode["fixed"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
