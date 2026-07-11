from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from dashboard import app


class DashboardAppTests(unittest.TestCase):
    def test_discover_result_sets_finds_live_status_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live_dir = root / "rivne_area"
            live_dir.mkdir(parents=True)
            (live_dir / "live_status.json").write_text("{}", encoding="utf-8")

            result_sets = app.discover_result_sets(root)

        self.assertEqual(result_sets, [live_dir.resolve()])

    def test_live_helpers_prepare_history_and_system_status(self) -> None:
        payload = {
            "metric_history": [
                {"time": 3.0, "departed": 2, "arrived": 1},
                {"time": 6.0, "departed": 4, "arrived": 3},
            ],
            "system": {
                "simulation": {"status": "running", "expected_vehicles": 5},
                "tls_programs": {
                    "status": "audited",
                    "count": 6,
                    "items": [{"tls_id": "tls_1", "program_type": 3}],
                },
                "corridor": {
                    "corridor_state": "GREEN_WINDOW",
                    "corridor_active_tls": "tls_1",
                },
            },
        }

        history = app.live_history_frame(payload)
        rows = app.live_system_rows(payload)

        self.assertEqual(history["departed"].tolist(), [2, 4])
        self.assertEqual(rows[0]["Стан"], "running")
        tls_row = next(row for row in rows if row["Компонент"] == "SUMO TLS programs")
        self.assertEqual(tls_row["Стан"], "audited")
        self.assertEqual(tls_row["Деталі"], "count=6")
        corridor_row = next(
            row for row in rows if row["Компонент"] == "Emergency corridor"
        )
        self.assertEqual(corridor_row["Стан"], "GREEN_WINDOW")

    def test_live_average_metrics_and_simulation_status(self) -> None:
        payload = {
            "metric_history": [
                {
                    "active_vehicles": 2,
                    "queue_length": 4,
                    "waiting_time": 6,
                    "mean_speed": 8,
                    "inflow_per_minute": 10,
                    "outflow_per_minute": 5,
                    "gridlock_risk": 0.1,
                },
                {
                    "active_vehicles": 4,
                    "queue_length": 8,
                    "waiting_time": 10,
                    "mean_speed": 12,
                    "inflow_per_minute": 20,
                    "outflow_per_minute": 15,
                    "gridlock_risk": 0.3,
                },
            ],
            "system": {"simulation": {"status": "running"}},
        }

        averages = app.live_average_metrics(payload)

        self.assertEqual(averages["active_vehicles"], 3.0)
        self.assertEqual(averages["queue_length"], 6.0)
        self.assertEqual(averages["mean_speed"], 10.0)
        self.assertEqual(app.simulation_status(payload), "running")

    def test_socket_payload_is_scoped_to_selected_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "selected"
            selected.mkdir()

            self.assertTrue(
                app.socket_payload_matches(
                    {"results_dir": str(selected)},
                    selected,
                )
            )
            self.assertFalse(
                app.socket_payload_matches(
                    {"results_dir": str(Path(directory) / "other")},
                    selected,
                )
            )

    def test_before_after_rows_report_improvement_in_plain_metrics(self) -> None:
        summary = pd.DataFrame(
            [
                {
                    "mode": "static_fixed",
                    "average_waiting_time": 40.0,
                    "average_queue_length": 20.0,
                    "stops_count": 100,
                    "throughput": 50,
                    "emergency_eta": 100.0,
                },
                {
                    "mode": "flowmind",
                    "average_waiting_time": 20.0,
                    "average_queue_length": 10.0,
                    "stops_count": 75,
                    "throughput": 60,
                    "emergency_eta": 70.0,
                },
            ]
        )

        rows = app.before_after_rows(summary)

        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["before"], 40.0)
        self.assertEqual(rows[0]["after"], 20.0)
        self.assertEqual(rows[0]["improvement"], 50.0)
        self.assertEqual(rows[3]["improvement"], 20.0)

    def test_legacy_fixed_results_remain_available_as_baseline(self) -> None:
        self.assertEqual(
            app.select_baseline_mode(("fixed", "local", "flowmind")),
            "fixed",
        )
        self.assertEqual(
            app.select_baseline_mode(("fixed", "static_fixed", "flowmind")),
            "static_fixed",
        )

    def test_decision_and_corridor_helpers_read_live_payload(self) -> None:
        payload = {
            "decision_log": [
                {
                    "time": 12.0,
                    "category": "corridor",
                    "title": "Зелений коридор активовано",
                }
            ],
            "system": {
                "corridor": {
                    "corridor_state": "GREEN_WINDOW",
                    "corridor_active_tls": "tls_1",
                }
            },
        }

        self.assertEqual(
            app.decision_log_rows(payload)[0]["category"],
            "corridor",
        )
        self.assertEqual(
            app.corridor_status(payload)["corridor_active_tls"],
            "tls_1",
        )

    def test_visual_comparison_uses_same_point_in_time(self) -> None:
        fixed = pd.DataFrame(
            [
                {
                    "time": 3,
                    "active_vehicles": 10,
                    "queue_length": 6,
                    "waiting_time": 12,
                    "mean_speed": 5,
                    "throughput": 1,
                    "stops_count": 8,
                },
                {
                    "time": 6,
                    "active_vehicles": 12,
                    "queue_length": 10,
                    "waiting_time": 20,
                    "mean_speed": 4,
                    "throughput": 2,
                    "stops_count": 12,
                },
            ]
        )
        flowmind = pd.DataFrame(
            [
                {
                    "time": 3,
                    "active_vehicles": 9,
                    "queue_length": 4,
                    "waiting_time": 8,
                    "mean_speed": 7,
                    "throughput": 2,
                    "stops_count": 5,
                },
                {
                    "time": 6,
                    "active_vehicles": 10,
                    "queue_length": 5,
                    "waiting_time": 11,
                    "mean_speed": 6,
                    "throughput": 4,
                    "stops_count": 7,
                },
            ]
        )

        comparison = app.visual_comparison_data(fixed, flowmind, 5.0)

        self.assertEqual(comparison["time"], 3.0)
        self.assertEqual(comparison["queue_difference"], 2.0)
        self.assertEqual(comparison["waiting_difference"], 4.0)
        self.assertEqual(comparison["throughput_difference"], 1.0)
        self.assertEqual(comparison["max_queue"], 10.0)

    def test_traffic_strip_is_bounded_and_contains_plain_counts(self) -> None:
        html = app.traffic_strip_html(
            {"queue_length": 30, "active_vehicles": 35},
            max_queue=20,
            accent="#10b981",
        )

        self.assertIn("width:100.0%", html)
        self.assertIn("30 авто в черзі", html)


if __name__ == "__main__":
    unittest.main()
