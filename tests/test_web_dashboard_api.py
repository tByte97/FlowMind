from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api import web_dashboard


class WebDashboardApiTests(unittest.TestCase):
    def test_jobs_payload_reports_dataset_quality_and_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset"
            dataset.mkdir()
            (dataset / "dataset_index.csv").write_text(
                "status,elapsed_seconds\ncompleted,10\ncompleted,20\n",
                encoding="utf-8",
            )
            (dataset / "quality_report.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "accepted_run_count": 1,
                        "rejected_run_count": 1,
                    }
                ),
                encoding="utf-8",
            )

            payload = web_dashboard.build_jobs_payload(root, root / "models")

        self.assertEqual(payload["dataset"]["completed_runs"], 2)
        self.assertEqual(payload["dataset"]["quality_status"], "completed")
        self.assertGreater(payload["disk"]["total_gb"], 0)

    def test_model_registry_exposes_contract_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory)
            (models / "queue_lgbm_60s_decision_metadata.json").write_text(
                json.dumps(
                    {
                        "forecast_contract": "observational_action_conditioned",
                        "artifact_sha256": "abc",
                        "known_tls_ids": ["a", "b"],
                        "known_lane_ids": ["x", "y", "z"],
                        "metrics": {"validation": {"mae": 1.25}},
                    }
                ),
                encoding="utf-8",
            )
            (models / "queue_control_approval.json").write_text(
                json.dumps(
                    {"status": "approved", "model_artifact_sha256": ["abc"]}
                ),
                encoding="utf-8",
            )

            payload = web_dashboard.build_model_registry(models)

        model = payload["models"][0]
        self.assertEqual(model["known_tls_count"], 2)
        self.assertEqual(model["known_lane_count"], 3)
        self.assertTrue(model["approved"])

    def test_mutation_auth_rejects_wrong_token(self) -> None:
        old_required = web_dashboard.REQUIRE_MUTATION_AUTH
        old_token = web_dashboard.MUTATION_TOKEN
        web_dashboard.REQUIRE_MUTATION_AUTH = True
        web_dashboard.MUTATION_TOKEN = "secret"
        request = type("Request", (), {"headers": {"x-flowmind-token": "wrong"}})()
        try:
            with self.assertRaises(web_dashboard.HTTPException) as context:
                web_dashboard.authorize_mutation(request)
        finally:
            web_dashboard.REQUIRE_MUTATION_AUTH = old_required
            web_dashboard.MUTATION_TOKEN = old_token

        self.assertEqual(context.exception.status_code, 401)

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
        self.assertIn('static fixed + FlowMind', page)
        self.assertIn('SUMO Actuated', web_dashboard.AVERAGES_PAGE)
        self.assertIn(
            "benchmark.emergency_route_evaluated",
            web_dashboard.AVERAGES_PAGE,
        )
        self.assertIn(
            "emergency route у цьому benchmark вимкнено",
            web_dashboard.AVERAGES_PAGE,
        )

    def test_zone_explorer_page_discloses_static_snapshot_semantics(self) -> None:
        script = web_dashboard.ZONE_EXPLORER_JS_PATH.read_text(encoding="utf-8")

        self.assertIn('/assets/zone-explorer.css', web_dashboard.ZONE_EXPLORER_PAGE)
        self.assertIn('/assets/zone-explorer.js', web_dashboard.ZONE_EXPLORER_PAGE)
        self.assertIn("static SUMO snapshot", script)
        self.assertIn("Статичний SUMO snapshot", script)
        self.assertIn("benchmark-level", script)
        self.assertNotIn("<strong>Ілюстративний replay</strong>", script)

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

    def test_averages_derive_spillback_metrics_and_flowmind_local_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flowmind = root / "flowmind_run"
            local = root / "local_run"
            flowmind.mkdir()
            local.mkdir()
            columns = (
                "mode,average_waiting_time,throughput,max_queue_length,"
                "blocked_outgoing_share,simulated_duration,zone_outflow,"
                "controller_decisions,phase_advances\n"
            )
            (flowmind / "summary.csv").write_text(
                columns + "flowmind,20,120,12,0.02,40,120,30,5\n",
                encoding="utf-8",
            )
            (local / "summary.csv").write_text(
                columns + "local,22,110,14,0.03,40,110,20,6\n",
                encoding="utf-8",
            )
            (flowmind / "flowmind_timeseries.csv").write_text(
                (
                    "time,blocked_outgoing_share\n"
                    "10,0\n"
                    "20,0\n"
                    "30,0\n"
                    "40,0\n"
                ),
                encoding="utf-8",
            )
            (local / "local_timeseries.csv").write_text(
                (
                    "time,blocked_outgoing_share\n"
                    "10,0\n"
                    "20,0.25\n"
                    "30,0.25\n"
                    "40,0\n"
                ),
                encoding="utf-8",
            )

            payload = web_dashboard.build_averages_payload(root)

        by_mode = {item["mode"]: item for item in payload["modes"]}
        self.assertEqual(
            by_mode["flowmind"]["metrics"]["spillback_free_time_share"][
                "average"
            ],
            1.0,
        )
        self.assertEqual(
            by_mode["flowmind"]["metrics"]["spillback_episode_count"]["average"],
            0.0,
        )
        self.assertEqual(
            by_mode["local"]["metrics"]["spillback_free_time_share"]["average"],
            0.5,
        )
        self.assertEqual(
            by_mode["local"]["metrics"]["spillback_episode_count"]["average"],
            1.0,
        )
        comparisons = {
            item["key"]: item for item in payload["flowmind_vs_local"]
        }
        self.assertEqual(
            comparisons["spillback_free_time_share"]["winner"],
            "flowmind",
        )
        self.assertGreater(
            comparisons["spillback_free_time_share"]["improvement_percent"],
            0.0,
        )
        self.assertEqual(
            comparisons["spillback_episode_count"]["winner"],
            "flowmind",
        )
        self.assertIn(
            "spillback_free_time_share",
            {item["key"] for item in payload["flowmind_zone_wins"]},
        )
        self.assertEqual(
            comparisons["outflow_per_control_action"]["status"],
            "neutral",
        )
        self.assertEqual(
            comparisons["outflow_per_control_action"]["winner"],
            "neutral",
        )
        self.assertIsNone(
            comparisons["outflow_per_control_action"]["improvement_percent"]
        )
        self.assertIsNotNone(
            comparisons["outflow_per_control_action"]["difference_percent"]
        )
        self.assertEqual(
            comparisons["clearance_action_share"]["status"],
            "neutral",
        )

    def test_spillback_backfill_excludes_unobserved_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flowmind_timeseries.csv"
            path.write_text(
                (
                    "time,blocked_outgoing_share\n"
                    "10,0.2\n"
                    "20,\n"
                    "30,0.3\n"
                    "40,0\n"
                ),
                encoding="utf-8",
            )

            metrics = web_dashboard._spillback_metrics_from_timeseries(
                path,
                simulated_duration=40,
            )

        self.assertEqual(metrics["spillback_free_time_share"], 0.3333)
        self.assertEqual(metrics["spillback_episode_count"], 2)

    def test_static_zone_catalog_contains_full_control_area(self) -> None:
        catalog = web_dashboard.build_static_zone_catalog()

        self.assertEqual(catalog["intersection_count"], 20)
        self.assertEqual(catalog["lane_count"], 221)
        self.assertEqual(len(catalog["intersections"]), 20)
        self.assertEqual(len(catalog["lanes"]), 221)
        self.assertTrue(all(item["name"] for item in catalog["intersections"]))
        self.assertTrue(all(item["shape"] for item in catalog["lanes"]))
        self.assertTrue(
            all(
                "phase_duration" in item
                and "incoming_queue" in item
                and "incoming_vehicles" in item
                for item in catalog["intersections"]
            )
        )

    def test_zone_result_id_rejects_path_like_input_without_discovery(self) -> None:
        with mock.patch.object(
            web_dashboard,
            "discover_result_sets",
            side_effect=AssertionError("invalid ids must be rejected early"),
        ):
            result = web_dashboard.find_result_dir_by_id(
                "../../etc/passwd",
                Path("/tmp"),
            )

        self.assertIsNone(result)

    def test_zone_default_prefers_completed_flowmind_real_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write_status(
                name: str,
                *,
                mode: str,
                status: str,
                snapshot: bool,
            ) -> Path:
                run_dir = root / name
                run_dir.mkdir()
                zone = {"status": status}
                if snapshot:
                    zone["intersections"] = [{"tls_id": "tls"}]
                    zone["lanes"] = [{"lane_id": "lane"}]
                (run_dir / "live_status.json").write_text(
                    json.dumps(
                        {
                            "mode": mode,
                            "system": {"simulation": {"status": status}},
                            "zone_simulation": zone,
                        }
                    ),
                    encoding="utf-8",
                )
                return run_dir

            flowmind = write_status(
                "flowmind_completed",
                mode="flowmind",
                status="completed",
                snapshot=True,
            )
            write_status(
                "local_completed",
                mode="local",
                status="completed",
                snapshot=True,
            )
            write_status(
                "newer_failed",
                mode="static_fixed",
                status="failed",
                snapshot=False,
            )

            selected = web_dashboard.find_preferred_zone_live_status(root)
            payload = web_dashboard.build_zone_explorer_payload(base_dir=root)
            running = write_status(
                "current_running",
                mode="flowmind",
                status="running",
                snapshot=False,
            )
            selected_running = web_dashboard.find_preferred_zone_live_status(
                root,
                running,
            )

        self.assertEqual(selected, flowmind / "live_status.json")
        self.assertEqual(payload["source"]["result_id"], web_dashboard._result_id(flowmind))
        self.assertEqual(payload["source"]["mode"], "flowmind")
        self.assertEqual(selected_running, running / "live_status.json")

    def test_zone_comparison_context_is_cached_for_live_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web_dashboard._ZONE_COMPARISON_CACHE.clear()
            original = web_dashboard.build_averages_payload
            with mock.patch.object(
                web_dashboard,
                "build_averages_payload",
                wraps=original,
            ) as build:
                first = web_dashboard._zone_comparison_context(root)
                second = web_dashboard._zone_comparison_context(root)

        self.assertIs(first, second)
        self.assertEqual(build.call_count, 1)

    def test_completed_zone_archive_stays_static_and_overlays_telemetry(
        self,
    ) -> None:
        catalog = web_dashboard.build_static_zone_catalog()
        tls_id = catalog["intersections"][0]["tls_id"]
        lane_id = catalog["lanes"][0]["lane_id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "completed_zone"
            run_dir.mkdir()
            (run_dir / "live_status.json").write_text(
                json.dumps(
                    {
                        "mode": "flowmind",
                        "system": {
                            "simulation": {
                                "status": "completed",
                            }
                        },
                        "zone_simulation": {
                            "status": "completed",
                            "simulated_time": 600,
                            "intersections": [
                                {
                                    "tls_id": tls_id,
                                    "phase": 2,
                                    "state": "rrGG",
                                    "phase_elapsed": 14.0,
                                    "vehicle_count": 11,
                                    "queue": 7,
                                    "outgoing_occupancy": 0.4,
                                }
                            ],
                            "lanes": [
                                {
                                    "lane_id": lane_id,
                                    "vehicle_count": 8,
                                    "queue": 5,
                                    "occupancy": 0.75,
                                    "mean_speed": 3.2,
                                }
                            ],
                            "vehicles": [
                                {
                                    "id": "vehicle-1",
                                    "lane_id": lane_id,
                                    "x": 1.0,
                                    "y": 2.0,
                                }
                            ],
                        },
                        "summary": {
                            "mode": "flowmind",
                            "throughput": 100,
                            "simulated_duration": 600,
                        },
                        "decision_log": [
                            {
                                "tls_id": tls_id,
                                "time": 590,
                                "title": "Зональна координація",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result_id = web_dashboard._result_id(run_dir)

            payload = web_dashboard.build_zone_explorer_payload(
                result_id=result_id,
                base_dir=root,
            )

        intersections = {
            item["tls_id"]: item for item in payload["scene"]["intersections"]
        }
        lanes = {item["lane_id"]: item for item in payload["scene"]["lanes"]}
        self.assertTrue(payload["available"])
        self.assertEqual(payload["source"]["kind"], "archive")
        self.assertEqual(payload["source"]["status"], "completed")
        self.assertTrue(payload["source"]["scene_is_static"])
        self.assertFalse(payload["source"]["illustrative"])
        self.assertEqual(payload["source"]["snapshot_time"], 600.0)
        self.assertTrue(payload["timeline"]["positions_are_static"])
        self.assertEqual(intersections[tls_id]["queue"], 7)
        self.assertEqual(intersections[tls_id]["vehicle_count"], 11)
        self.assertTrue(intersections[tls_id]["name"])
        self.assertEqual(lanes[lane_id]["queue"], 5)
        self.assertEqual(lanes[lane_id]["occupancy"], 0.75)
        self.assertTrue(lanes[lane_id]["shape"])
        self.assertEqual(payload["scene"]["vehicles"][0]["id"], "vehicle-1")
        self.assertEqual(payload["actions"][0]["tls_id"], tls_id)
        self.assertFalse(payload["comparison"]["snapshot_specific"])

    def test_running_without_snapshot_is_explicitly_network_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "starting_zone"
            run_dir.mkdir()
            (run_dir / "live_status.json").write_text(
                json.dumps(
                    {
                        "mode": "flowmind",
                        "system": {"simulation": {"status": "running"}},
                        "zone_simulation": {
                            "status": "running",
                            "intersections": [],
                            "lanes": [],
                            "vehicles": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = web_dashboard.build_zone_explorer_payload(
                result_id=web_dashboard._result_id(run_dir),
                base_dir=root,
            )

        self.assertEqual(payload["source"]["kind"], "static")
        self.assertTrue(payload["source"]["illustrative"])
        self.assertTrue(payload["source"]["scene_is_static"])
        self.assertTrue(payload["timeline"]["positions_are_static"])

    def test_averages_prefers_complete_paired_evaluation_over_old_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_run = root / "old_aggressive_flowmind"
            old_run.mkdir()
            (old_run / "summary.csv").write_text(
                "mode,average_waiting_time,throughput\nflowmind,100,50\n",
                encoding="utf-8",
            )

            evaluation = root / "evaluation" / "defense_v1"
            pair_dir = evaluation / "pair_001_seed_42"
            pair = {
                "pair_id": "defense_v1:pair:001",
                "replicate": 1,
                "seed": 42,
                "pair_config_sha256": "paired-config",
                "emergency_route_sha256": "paired-route",
            }
            for mode, wait, throughput in (
                ("static_fixed", 40, 100),
                ("sumo_actuated", 35, 110),
                ("local", 32, 120),
                ("flowmind", 30, 125),
            ):
                mode_dir = pair_dir / mode
                mode_dir.mkdir(parents=True)
                summary = {
                    "mode": mode,
                    "seed": 42,
                    "evaluation_pair_id": pair["pair_id"],
                    "pair_config_sha256": pair["pair_config_sha256"],
                    "emergency_route_sha256": pair["emergency_route_sha256"],
                    "average_waiting_time": wait,
                    "throughput": throughput,
                }
                if mode == "sumo_actuated":
                    summary["actuated_detector_coverage_complete"] = False
                (mode_dir / f"{mode}_summary.json").write_text(
                    json.dumps(summary),
                    encoding="utf-8",
                )
            evaluation.mkdir(parents=True, exist_ok=True)
            (evaluation / "evaluation_report.json").write_text(
                json.dumps(
                    {
                        "evaluation_id": "defense_v1",
                        "required_modes": [
                            "static_fixed",
                            "sumo_actuated",
                            "local",
                            "flowmind",
                        ],
                        "valid_pair_count": 1,
                        "valid_pairs": [pair],
                        "emergency_route_evaluated": True,
                        "overall_status": "complete",
                    }
                ),
                encoding="utf-8",
            )

            payload = web_dashboard.build_averages_payload(root)

        by_mode = {item["mode"]: item for item in payload["modes"]}
        self.assertEqual(payload["scope"], "paired_evaluation")
        self.assertEqual(payload["benchmark"]["pair_count"], 1)
        self.assertEqual(payload["historical_result_count"], 1)
        self.assertEqual(set(by_mode), {"static_fixed", "local", "flowmind"})
        self.assertEqual(
            by_mode["flowmind"]["metrics"]["average_waiting_time"]["average"],
            30.0,
        )
        self.assertIn("sumo_actuated", payload["benchmark"]["excluded_modes"])

    def test_summary_context_prefers_static_fixed_over_legacy_fixed(self) -> None:
        context = web_dashboard.build_summary_context(
            {
                "summary_rows": [
                    {"mode": "fixed", "average_waiting_time": 40},
                    {"mode": "static_fixed", "average_waiting_time": 30},
                    {"mode": "flowmind", "average_waiting_time": 20},
                ],
                "summary": {"mode": "flowmind", "average_waiting_time": 20},
            }
        )

        self.assertEqual(context["fixed"]["mode"], "static_fixed")
        self.assertAlmostEqual(
            context["improvements"]["waiting_time_percent"],
            100 / 3,
        )


if __name__ == "__main__":
    unittest.main()
