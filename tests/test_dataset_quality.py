from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.dataset_quality import DatasetQualityThresholds, audit_dataset


class DatasetQualityTests(unittest.TestCase):
    def test_rejects_bad_teleports_and_incomplete_actuated_detectors(self) -> None:
        with TemporaryDirectory() as directory:
            dataset = Path(directory)
            samples = dataset / "samples"
            run_id = "rivne_sumo_actuated_r0001_seed_1"
            summary_dir = dataset / "summaries" / run_id
            raw = summary_dir / "raw"
            samples.mkdir(parents=True)
            raw.mkdir(parents=True)
            with (samples / f"{run_id}.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("tls_id", "signal_index"))
                writer.writeheader()
                writer.writerows(
                    ({"tls_id": "tls", "signal_index": index} for index in (0, 1))
                )
            (summary_dir / "sumo_actuated_summary.json").write_text(
                json.dumps(
                    {"mode": "sumo_actuated", "seed": 1, "departed_vehicles": 10}
                ),
                encoding="utf-8",
            )
            (raw / "sumo_actuated_sumo.log").write_text(
                "Warning: At actuated tlLogic 'tls', linkIndex 1 has no controlling detector.\n"
                "Warning: Teleporting vehicle 'a'; waited too long (jam), lane='x'.\n",
                encoding="utf-8",
            )

            report = audit_dataset(dataset, DatasetQualityThresholds())

        self.assertEqual(report["run_count"], 1)
        run = report["runs"][0]
        self.assertFalse(run["accepted"])
        self.assertEqual(run["teleport_reasons"], {"jam": 1})
        self.assertEqual(run["actuated_detector_coverage"], 0.5)
        self.assertIn("teleport_rate_exceeded", run["rejection_reasons"])
        self.assertIn(
            "actuated_detector_coverage_below_minimum", run["rejection_reasons"]
        )


if __name__ == "__main__":
    unittest.main()
