from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from flowmind.config import CONTROL_MODES
from experiments.run_dataset import (
    INDEX_COLUMNS,
    build_parser,
    default_scenario_file,
    successful_run_ids,
    validate_resume_fingerprint,
)


class RunDatasetTests(unittest.TestCase):
    def test_defaults_are_pinned_to_rivne_area(self) -> None:
        self.assertEqual(
            default_scenario_file("focused.sumocfg").parent.name,
            "rivne_area",
        )

    def test_resume_refuses_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "dataset_plan.json"
            manifest.write_text(
                '{"dataset_fingerprint": "zone6"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                validate_resume_fingerprint(manifest, "zone20")

    def test_default_plan_contains_all_canonical_modes(self) -> None:
        self.assertEqual(tuple(build_parser().parse_args([]).modes), CONTROL_MODES)

    def test_successful_run_ids_returns_only_ok_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "dataset_index.csv"
            with index_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
                writer.writeheader()
                writer.writerow({"run_id": "complete", "status": "ok"})
                writer.writerow({"run_id": "interrupted", "status": "failed"})
                writer.writerow({"run_id": "", "status": "ok"})

            result = successful_run_ids(index_path)

        self.assertEqual(result, {"complete"})

    def test_successful_run_ids_handles_missing_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "missing.csv"

            result = successful_run_ids(index_path)

        self.assertEqual(result, set())


if __name__ == "__main__":
    unittest.main()
