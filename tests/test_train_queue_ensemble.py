from __future__ import annotations

import sys
import unittest
from pathlib import Path

from experiments.train_queue_ensemble import training_command


class TrainQueueEnsembleTests(unittest.TestCase):
    def test_training_command_targets_requested_horizon(self) -> None:
        command = training_command(
            dataset_dir=Path("/data"),
            output_dir=Path("/models"),
            horizon=60,
            rows_per_file=5000,
            jobs=4,
            n_estimators=700,
        )

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(
            command[command.index("--target") + 1],
            "target_queue_reduction_60s",
        )
        self.assertEqual(
            command[command.index("--output") + 1],
            "/models/queue_lgbm_60s_decision.joblib",
        )
        self.assertEqual(command[command.index("--rows-per-file") + 1], "5000")


if __name__ == "__main__":
    unittest.main()
