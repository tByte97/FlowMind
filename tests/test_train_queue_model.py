from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from experiments.train_queue_model import (
    CATEGORICAL_FEATURES,
    add_optional_feature_defaults,
    filter_paths_by_quality_report,
)


class TrainQueueModelTests(unittest.TestCase):
    def test_schema_v3_rows_upgrade_to_observational_feature_names(self) -> None:
        frame = pd.DataFrame(
            [{"signal_state": "G", "phase_state": "Gr", "action_phase": 0}]
        )

        upgraded = add_optional_feature_defaults(frame)

        self.assertEqual(upgraded.iloc[0]["current_signal_state"], "G")
        self.assertEqual(upgraded.iloc[0]["action_phase_state"], "Gr")
        self.assertNotIn("mode", CATEGORICAL_FEATURES)

    def test_quality_report_filters_rejected_sample_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.csv"
            bad = root / "bad.csv"
            good.touch()
            bad.touch()
            report = root / "quality_report.json"
            report.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "accepted_sample_files": [str(good)],
                        "runs": [
                            {"sample_file": str(good), "accepted": True},
                            {"sample_file": str(bad), "accepted": False},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            selected = filter_paths_by_quality_report([good, bad], report)

        self.assertEqual(selected, [good])


if __name__ == "__main__":
    unittest.main()
