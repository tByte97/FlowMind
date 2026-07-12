from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from flowmind.area_model import ControlledLink, Intersection
from flowmind.config import ControlConfig
from flowmind.queue_forecast import (
    QueueForecastEnsemble,
    QueueForecastModel,
    feature_schema_sha256,
    file_sha256,
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
                    "current_phase",
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
                "current_phase",
                "incoming_queue",
                "movement_hash",
            ],
        )
        self.assertEqual(
            int(pipeline.last_frame.iloc[0]["movement_hash"]),
            stable_hash(movement_id_for("tls", intersection.links[0])),
        )
        self.assertEqual(set(pipeline.last_frame["current_phase"]), {0})
        self.assertEqual(set(pipeline.last_frame["phase_state"]), {"Gr"})

    def test_counterfactual_contract_keeps_actual_and_candidate_phase_separate(
        self,
    ) -> None:
        pipeline = FakePipeline()
        columns = [
            "time",
            "tls_id",
            "incoming_lane",
            "outgoing_lane",
            "current_phase",
            "candidate_phase",
            "phase_state",
            "signal_state",
        ]
        with TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "model.joblib"
            artifact_path.write_bytes(b"artifact")
            numeric = ["time", "current_phase", "candidate_phase"]
            categorical = [
                "tls_id",
                "incoming_lane",
                "outgoing_lane",
                "phase_state",
                "signal_state",
            ]
            model = QueueForecastModel(
                pipeline,
                pd.DataFrame,
                artifact_path,
                {
                    "target": "target_incoming_queue_60s",
                    "forecast_contract": "counterfactual",
                    "feature_columns": columns,
                    "numeric_features": numeric,
                    "categorical_features": categorical,
                    "known_tls_ids": ["tls"],
                    "known_lane_ids": ["north", "south", "east", "west"],
                    "feature_ranges": {"time": [0, 100]},
                    "dataset_sha256": "dataset",
                    "network_sha256": "network",
                    "artifact_sha256": file_sha256(artifact_path),
                    "feature_schema_sha256": feature_schema_sha256(
                        columns, numeric, categorical, "counterfactual"
                    ),
                },
            )
            intersection, state = self._two_direction_state()

            predictions = model.predict_intersection(
                mode="flowmind",
                simulation_time=12.0,
                intersection=intersection,
                state=state,
                current_phase=0,
                phase_elapsed=11.0,
                control=ControlConfig(),
                sample_interval=5,
            )

        self.assertEqual(predictions, {(0, 0): 3.5, (2, 1): 3.5})
        self.assertEqual(set(pipeline.last_frame["current_phase"]), {0})
        self.assertEqual(set(pipeline.last_frame["candidate_phase"]), {0, 2})
        self.assertEqual(set(pipeline.last_frame["phase_state"]), {"Gr"})
        self.assertFalse(model.last_diagnostics.ood)
        self.assertTrue(model.last_diagnostics.influence_allowed)

    def test_prediction_is_bounded_by_incoming_lane_capacity(self) -> None:
        model = QueueForecastModel(
            FakePipeline(prediction=99.0),
            pd.DataFrame,
            Path("model.joblib"),
            {
                "target": "target_incoming_queue_60s",
                "feature_columns": ["time", "incoming_queue"],
            },
        )
        intersection, state = self._two_direction_state()

        predictions = model.predict_intersection(
            mode="flowmind",
            simulation_time=12.0,
            intersection=intersection,
            state=state,
            current_phase=0,
            phase_elapsed=11.0,
            control=ControlConfig(),
            sample_interval=5,
        )

        self.assertEqual(predictions, {(0, 0): 12.0, (2, 1): 12.0})

    def test_unseen_lane_disables_model_influence(self) -> None:
        pipeline = FakePipeline()
        columns = ["time", "tls_id", "incoming_lane"]
        with TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "model.joblib"
            artifact_path.write_bytes(b"artifact")
            numeric = ["time"]
            categorical = ["tls_id", "incoming_lane"]
            model = QueueForecastModel(
                pipeline,
                pd.DataFrame,
                artifact_path,
                {
                    "feature_columns": columns,
                    "numeric_features": numeric,
                    "categorical_features": categorical,
                    "known_tls_ids": ["tls"],
                    "known_lane_ids": ["north", "south"],
                    "feature_ranges": {"time": [0, 100]},
                    "dataset_sha256": "dataset",
                    "network_sha256": "network",
                    "artifact_sha256": file_sha256(artifact_path),
                    "feature_schema_sha256": feature_schema_sha256(
                        columns, numeric, categorical, "current_policy"
                    ),
                },
            )
            intersection, state = self._two_direction_state()

            model.predict_intersection(
                mode="flowmind",
                simulation_time=12.0,
                intersection=intersection,
                state=state,
                current_phase=0,
                phase_elapsed=11.0,
                control=ControlConfig(),
                sample_interval=5,
            )

        self.assertTrue(model.last_diagnostics.ood)
        self.assertIn("unseen_lane", model.last_diagnostics.reasons)
        self.assertFalse(model.last_diagnostics.influence_allowed)

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

    @staticmethod
    def _two_direction_state() -> tuple[Intersection, TrafficState]:
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
        return intersection, state


if __name__ == "__main__":
    unittest.main()
