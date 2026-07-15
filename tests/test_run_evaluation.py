from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from experiments.run_evaluation import (
    _validate_resume_manifest,
    emergency_route_edges,
    select_evaluation_modes,
    validate_replicate_count,
)


class EvaluationRunnerTests(unittest.TestCase):
    def test_full_run_requires_30_to_50_replicates(self) -> None:
        validate_replicate_count(30, False)
        validate_replicate_count(50, False)
        with self.assertRaises(ValueError):
            validate_replicate_count(29, False)
        with self.assertRaises(ValueError):
            validate_replicate_count(51, False)

    def test_smoke_profile_allows_small_positive_run(self) -> None:
        validate_replicate_count(1, True)
        with self.assertRaises(ValueError):
            validate_replicate_count(0, True)

    def test_emergency_route_is_parsed_from_summary(self) -> None:
        self.assertEqual(
            emergency_route_edges({"emergency_route_edges": "edge-a edge-b"}),
            ("edge-a", "edge-b"),
        )
        self.assertEqual(emergency_route_edges({}), ())

    def test_selected_baselines_define_modes_with_flowmind_last(self) -> None:
        baselines, modes = select_evaluation_modes(("static_fixed", "local"))

        self.assertEqual(baselines, ("static_fixed", "local"))
        self.assertEqual(modes, ("static_fixed", "local", "flowmind"))

    def test_selected_baselines_must_be_known_and_unique(self) -> None:
        with self.assertRaises(ValueError):
            select_evaluation_modes(())
        with self.assertRaises(ValueError):
            select_evaluation_modes(("local", "local"))
        with self.assertRaises(ValueError):
            select_evaluation_modes(("flowmind",))

    def test_resume_treats_json_lists_and_config_tuples_as_equal(self) -> None:
        requested = {
            "evaluation_runner_schema_version": 1,
            "control_config": {
                "queue_forecast_horizon_weights": (
                    (30, 0.5),
                    (60, 0.35),
                    (90, 0.15),
                )
            },
        }
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "evaluation_manifest.json"
            manifest.write_text(json.dumps(requested), encoding="utf-8")

            _validate_resume_manifest(manifest, requested)


if __name__ == "__main__":
    unittest.main()
