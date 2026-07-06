from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from experiments.run_dataset import INDEX_COLUMNS, successful_run_ids


class RunDatasetTests(unittest.TestCase):
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
