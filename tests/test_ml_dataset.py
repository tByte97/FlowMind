from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.ml_dataset import MLDatasetCollector, MLDatasetConfig


class FakeLane:
    def __init__(self) -> None:
        self.queue = {"in_0": 2, "out_0": 0}

    def getLastStepVehicleNumber(self, lane_id: str) -> int:
        return self.queue[lane_id]

    def getLastStepVehicleIDs(self, _lane_id: str) -> tuple[str, ...]:
        return ()

    def getLength(self, _lane_id: str) -> float:
        return 100.0

    def getLastStepHaltingNumber(self, lane_id: str) -> int:
        return self.queue[lane_id]

    def getLastStepOccupancy(self, lane_id: str) -> float:
        return float(self.queue[lane_id] * 10)

    def getLastStepMeanSpeed(self, lane_id: str) -> float:
        return 0.0 if self.queue[lane_id] else 10.0


class FakeTrafficLight:
    def getPhase(self, _tls_id: str) -> int:
        return 0

    def getSpentDuration(self, _tls_id: str) -> float:
        return 12.0


class FakeTraci:
    def __init__(self) -> None:
        self.lane = FakeLane()
        self.trafficlight = FakeTrafficLight()


class MLDatasetCollectorTest(unittest.TestCase):
    def test_writes_future_queue_targets(self) -> None:
        traci = FakeTraci()
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls",
                    position=(0.0, 0.0),
                    phases=("G", "y", "r"),
                    links=(ControlledLink("in_0", "out_0", 0),),
                ),
            )
        )
        with TemporaryDirectory() as directory:
            collector = MLDatasetCollector(
                traci,
                area,
                ControlConfig(),
                MLDatasetConfig(
                    output_dir=Path(directory),
                    run_id="run",
                    scenario="test",
                    mode="flowmind",
                    seed=1,
                    duration=30,
                    sample_interval=5,
                    target_horizons=(30,),
                ),
            )

            collector.collect(0.0)
            traci.lane.queue["in_0"] = 7
            collector.collect(30.0)
            metadata = collector.write()

            with Path(metadata["dataset_csv"]).open(
                newline="",
                encoding="utf-8",
            ) as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["incoming_queue"], "2")
        self.assertEqual(rows[0]["target_incoming_queue_30s"], "7")
        self.assertEqual(rows[0]["target_delta_queue_30s"], "5")
        self.assertEqual(rows[0]["target_queue_reduction_30s"], "-5")
        self.assertEqual(rows[0]["candidate_phase"], "")
        self.assertEqual(rows[0]["action_phase"], "0")
        self.assertEqual(rows[0]["action_phase_state"], "G")
        self.assertEqual(rows[0]["dataset_schema_version"], "4")
        self.assertEqual(rows[0]["sample_interval"], "5")
        self.assertEqual(rows[0]["queue_forecast_weight"], "0.75")
        self.assertEqual(rows[1]["target_incoming_queue_30s"], "")

    def test_fractional_steps_do_not_duplicate_samples(self) -> None:
        collector = MLDatasetCollector(
            FakeTraci(),
            AreaModel(
                (
                    Intersection(
                        tls_id="tls",
                        position=(0.0, 0.0),
                        phases=("G",),
                        links=(ControlledLink("in_0", "out_0", 0),),
                    ),
                )
            ),
            ControlConfig(),
            MLDatasetConfig(
                output_dir=Path("unused"),
                run_id="fractional",
                scenario="test",
                mode="flowmind",
                seed=1,
                duration=10,
                sample_interval=5,
            ),
        )

        for simulation_time in (0.0, 0.2, 4.9, 5.1):
            collector.collect(simulation_time)

        self.assertEqual(collector.row_count, 2)


if __name__ == "__main__":
    unittest.main()
