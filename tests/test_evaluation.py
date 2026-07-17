from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from flowmind.config import CONTROL_MODES
from flowmind.evaluation import analyze_paired_summaries, write_evaluation_report


class PairedEvaluationTests(unittest.TestCase):
    def test_full_paired_evaluation_passes_with_improvements(self) -> None:
        report = analyze_paired_summaries(_summaries(30))

        self.assertEqual(report["valid_pair_count"], 30)
        self.assertEqual(report["pair_count_status"], "complete")
        self.assertTrue(report["emergency_route_evaluated"])
        self.assertEqual(report["overall_status"], "pass")
        comparison = report["comparisons"]["static_fixed"]
        waiting = comparison["metrics"]["average_waiting_time"]
        self.assertEqual(waiting["sample_count"], 30)
        self.assertLess(waiting["mean_delta_contender_minus_baseline"], 0)
        self.assertEqual(
            comparison["regression_gates"]["zone_outflow"]["status"],
            "pass",
        )

    def test_pair_with_different_seed_is_rejected(self) -> None:
        summaries = _summaries(30)
        summaries[-1]["seed"] = 999

        report = analyze_paired_summaries(summaries)

        self.assertEqual(report["valid_pair_count"], 29)
        self.assertEqual(report["invalid_pair_count"], 1)
        self.assertEqual(report["overall_status"], "invalid_pairs")
        self.assertIn("seed differs", report["invalid_pairs"][0]["errors"][0])

    def test_null_metric_is_not_converted_to_zero(self) -> None:
        summaries = _summaries(30)
        flowmind_rows = [row for row in summaries if row["mode"] == "flowmind"]
        flowmind_rows[0]["average_waiting_time"] = None

        report = analyze_paired_summaries(summaries)

        waiting = report["comparisons"]["static_fixed"]["metrics"][
            "average_waiting_time"
        ]
        gate = report["comparisons"]["static_fixed"]["regression_gates"][
            "average_waiting_time"
        ]
        self.assertEqual(waiting["sample_count"], 29)
        self.assertEqual(waiting["missing_pair_count"], 1)
        self.assertEqual(gate["status"], "insufficient_data")
        self.assertEqual(report["overall_status"], "insufficient_data")

    def test_regression_gate_uses_upper_confidence_bound(self) -> None:
        summaries = _summaries(30)
        for row in summaries:
            if row["mode"] == "flowmind":
                row["average_waiting_time"] = 20.0

        report = analyze_paired_summaries(summaries)

        gate = report["comparisons"]["static_fixed"]["regression_gates"][
            "average_waiting_time"
        ]
        self.assertEqual(gate["status"], "fail")
        self.assertGreater(gate["degradation_ci95_upper"], gate["tolerance"])
        self.assertEqual(report["overall_status"], "regression")

    def test_report_without_emergency_is_explicitly_partial(self) -> None:
        summaries = _summaries(30)
        for row in summaries:
            row["emergency_route_sha256"] = ""
            row["emergency_eta"] = None

        report = analyze_paired_summaries(summaries)

        self.assertFalse(report["emergency_route_evaluated"])
        self.assertEqual(report["overall_status"], "partial_missing_emergency")
        gate = report["comparisons"]["local"]["regression_gates"][
            "emergency_eta"
        ]
        self.assertEqual(gate["status"], "not_applicable")

    def test_report_artifacts_are_written(self) -> None:
        report = analyze_paired_summaries(_summaries(30))
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            artifacts = write_evaluation_report(report, output_dir)

            loaded = json.loads(
                Path(artifacts["evaluation_report"]).read_text(encoding="utf-8")
            )
            self.assertEqual(loaded["overall_status"], "pass")
            with Path(artifacts["paired_metrics"]).open(
                encoding="utf-8", newline=""
            ) as handle:
                metric_rows = list(csv.DictReader(handle))
            self.assertEqual(len(metric_rows), 15)
            self.assertTrue(Path(artifacts["regression_gates"]).is_file())
            self.assertTrue(Path(artifacts["validated_pairs"]).is_file())


def _summaries(pair_count: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for replicate in range(1, pair_count + 1):
        seed = 41 + replicate
        pair_id = f"evaluation:pair:{replicate:03d}"
        for mode in CONTROL_MODES:
            contender = mode == "flowmind"
            rows.append(
                {
                    "mode": mode,
                    "evaluation_id": "evaluation",
                    "evaluation_pair_id": pair_id,
                    "evaluation_replicate": replicate,
                    "seed": seed,
                    "scenario_sha256": "scenario",
                    "network_sha256": "network",
                    "route_files_sha256": "demand",
                    "zone_sha256": "zone",
                    "pair_config_sha256": f"config-{replicate}",
                    "demand_vehicles_per_hour": 3600.0,
                    "demand_duration_seconds": 1800.0,
                    "emergency_route_sha256": f"route-{replicate}",
                    "average_waiting_time": (
                        8.0 + replicate / 100 if contender else 10.0 + replicate / 100
                    ),
                    "zone_outflow": 110 + replicate if contender else 100 + replicate,
                    "stops_count": 40 + replicate if contender else 50 + replicate,
                    "blocked_outgoing_share": 0.1 if contender else 0.2,
                    "emergency_eta": 90.0 + replicate / 10 if contender else 100.0 + replicate / 10,
                }
            )
    return rows


if __name__ == "__main__":
    unittest.main()
