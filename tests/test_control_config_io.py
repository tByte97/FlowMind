from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.control_config_io import load_control_config


class ControlConfigIOTest(unittest.TestCase):
    def test_loads_tuning_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "best.json"
            path.write_text(
                json.dumps(
                    {
                        "control_config": {
                            "zone_coordination_weight": 2.75,
                            "queue_forecast_horizon_weights": [
                                [30, 0.6],
                                [60, 0.4],
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            control = load_control_config(path)

        self.assertEqual(control.zone_coordination_weight, 2.75)
        self.assertEqual(
            control.queue_forecast_horizon_weights,
            ((30, 0.6), (60, 0.4)),
        )


if __name__ == "__main__":
    unittest.main()
