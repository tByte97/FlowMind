from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from flowmind.area_model import ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.queue_forecast import (
    QueueForecastEnsemble,
    QueueForecastModel,
    horizon_from_target,
    movement_id_for,
    stable_hash,
)
from flowmind.traffic_state import LaneState, TrafficState


class FakePipeline:
    def __init__(self, prediction: float = 3.5) -> None:
        self.last_frame = None
        self._prediction = prediction

    def predict(self, frame: object) -> list[float]:
        self.last_frame = frame
        return [self._prediction for _index in range(len(frame))]


class QueueForecastModelTest(unittest.TestCase):
    def test_predicts_candidate_green_movements(self) -> None:
        pipeline = FakePipeline()
        model = QueueForecastModel(
            pipeline,
            pd.DataFrame,
            Path("model.joblib"),
            {
                "target": "target_incoming_queue_60s",
                "feature_columns": [
                    "time",
                    "mode",
                    "tls_id",
                    "signal_state",
                    "phase_state",
                    "incoming_queue",
                    "movement_hash",
                ],
            },
        )
        intersection = Intersection(
            tls_id="tls",
            position=(0.0, 0.0),
            phases=("Gr", "yr", "rG", "ry"),
            links=(
                ControlledLink("north", "south", 0),
                ControlledLink("east", "west", 1),
            ),
        )
        state = TrafficState(
            {
                "north": LaneState(4, 5, 0.4, 2.0, 7.0),
                "south": LaneState(0, 0, 0.0, 10.0, 10.0),
                "east": LaneState(2, 3, 0.2, 3.0, 9.0),
                "west": LaneState(0, 0, 0.0, 10.0, 10.0),
            }
        )

        predictions = model.predict_intersection(
            mode="flowmind",
            simulation_time=12.7,
            intersection=intersection,
            state=state,
            current_phase=0,
            phase_elapsed=11.0,
            control=ControlConfig(),
            sample_interval=5,
        )

        self.assertEqual(predictions, {(0, 0): 3.5, (2, 1): 3.5})
        self.assertIsNotNone(pipeline.last_frame)
        self.assertEqual(
            list(pipeline.last_frame.columns),
            [
                "time",
                "mode",
                "tls_id",
                "signal_state",
                "phase_state",
                "incoming_queue",
                "movement_hash",
            ],
        )
        self.assertEqual(
            int(pipeline.last_frame.iloc[0]["movement_hash"]),
            stable_hash(movement_id_for("tls", intersection.links[0])),
        )

    def test_blends_multiple_horizons(self) -> None:
        model_30 = QueueForecastModel(
            FakePipeline(prediction=2.0),
            pd.DataFrame,
            Path("model_30.joblib"),
            {
                "target": "target_incoming_queue_30s",
                "feature_columns": ["time", "mode", "incoming_queue"],
            },
        )
        model_90 = QueueForecastModel(
            FakePipeline(prediction=8.0),
            pd.DataFrame,
            Path("model_90.joblib"),
            {
                "target": "target_incoming_queue_90s",
                "feature_columns": ["time", "mode", "incoming_queue"],
            },
        )
        ensemble = QueueForecastEnsemble(((model_30, 0.75), (model_90, 0.25)))
        intersection = Intersection(
            tls_id="tls",
            position=(0.0, 0.0),
            phases=("G", "y"),
            links=(ControlledLink("north", "south", 0),),
        )
        state = TrafficState(
            {
                "north": LaneState(4, 5, 0.4, 2.0, 7.0),
                "south": LaneState(0, 0, 0.0, 10.0, 10.0),
            }
        )

        predictions = ensemble.predict_intersection(
            mode="flowmind",
            simulation_time=12.0,
            intersection=intersection,
            state=state,
            current_phase=0,
            phase_elapsed=11.0,
            control=ControlConfig(),
            sample_interval=5,
        )

        self.assertEqual(predictions, {(0, 0): 3.5})
        self.assertEqual(ensemble.stats.model_count, 2)
        self.assertEqual(ensemble.stats.horizons, "30,90")
        self.assertEqual(ensemble.stats.horizon_weights, "30s:0.75,90s:0.25")

    def test_reads_horizon_from_target(self) -> None:
        self.assertEqual(horizon_from_target("target_incoming_queue_90s"), 90)
        self.assertIsNone(horizon_from_target("target_incoming_queue"))


if __name__ == "__main__":
    unittest.main()
