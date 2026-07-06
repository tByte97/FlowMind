from __future__ import annotations

import hashlib
import math
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .area_model import ControlledLink, Intersection
from .config import ControlConfig
from .traffic_state import TrafficState

os.environ.setdefault("MPLCONFIGDIR", "/tmp/flowmind_matplotlib")
warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names.*",
    category=UserWarning,
)


@dataclass(frozen=True)
class QueueForecastStats:
    model_path: str
    target: str
    feature_count: int
    model_count: int = 1
    horizons: str = ""
    horizon_weights: str = ""


class QueueForecastModel:
    """Runtime adapter for trained queue-forecast artifacts."""

    def __init__(
        self,
        pipeline: Any,
        dataframe_factory: Any,
        model_path: Path,
        metadata: dict[str, Any],
    ) -> None:
        self._pipeline = pipeline
        self._dataframe_factory = dataframe_factory
        self._model_path = model_path
        self._metadata = metadata
        self._feature_columns = tuple(
            str(column) for column in metadata.get("feature_columns", ())
        )
        if not self._feature_columns:
            feature_names = getattr(pipeline, "feature_names_in_", ())
            self._feature_columns = tuple(str(column) for column in feature_names)
        if not self._feature_columns:
            raise ValueError("Queue forecast artifact does not expose feature_columns")
        self._target = str(metadata.get("target", "target_incoming_queue_60s"))
        self._horizon_seconds = horizon_from_target(self._target)

    @classmethod
    def load(cls, model_path: Path) -> QueueForecastModel:
        import joblib
        import pandas as pd

        artifact = joblib.load(model_path)
        if isinstance(artifact, dict):
            pipeline = artifact.get("pipeline")
            metadata = artifact.get("metadata", {})
        else:
            pipeline = artifact
            metadata = {}
        if pipeline is None:
            raise ValueError(f"Queue forecast artifact has no pipeline: {model_path}")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(pipeline, pd.DataFrame, model_path, metadata)

    @property
    def stats(self) -> QueueForecastStats:
        return QueueForecastStats(
            model_path=str(self._model_path),
            target=self._target,
            feature_count=len(self._feature_columns),
            horizons=(str(self._horizon_seconds) if self._horizon_seconds else ""),
        )

    @property
    def horizon_seconds(self) -> int | None:
        return self._horizon_seconds

    def predict_intersection(
        self,
        *,
        mode: str,
        simulation_time: float,
        intersection: Intersection,
        state: TrafficState,
        current_phase: int,
        phase_elapsed: float,
        control: ControlConfig,
        sample_interval: int,
    ) -> dict[tuple[int, int], float]:
        rows: list[dict[str, Any]] = []
        keys: list[tuple[int, int]] = []
        for phase_index in intersection.green_phase_indices:
            phase_state = intersection.phases[phase_index]
            for link_index, link in enumerate(intersection.links):
                if link.signal_index >= len(phase_state):
                    continue
                signal_state = phase_state[link.signal_index]
                if signal_state not in "Gg":
                    continue
                rows.append(
                    self._feature_row(
                        mode=mode,
                        simulation_time=simulation_time,
                        intersection=intersection,
                        link=link,
                        signal_state=signal_state,
                        phase_index=phase_index,
                        phase_state=phase_state,
                        phase_elapsed=(
                            phase_elapsed if phase_index == current_phase else 0.0
                        ),
                        state=state,
                        control=control,
                        sample_interval=sample_interval,
                    )
                )
                keys.append((phase_index, link_index))

        if not rows:
            return {}

        frame = self._dataframe_factory(rows)
        frame = frame.reindex(columns=self._feature_columns, fill_value=0)
        predictions = self._pipeline.predict(frame)
        return {
            key: sanitized_prediction(prediction)
            for key, prediction in zip(keys, predictions, strict=False)
        }

    def _feature_row(
        self,
        *,
        mode: str,
        simulation_time: float,
        intersection: Intersection,
        link: ControlledLink,
        signal_state: str,
        phase_index: int,
        phase_state: str,
        phase_elapsed: float,
        state: TrafficState,
        control: ControlConfig,
        sample_interval: int,
    ) -> dict[str, Any]:
        incoming = state.lane(link.incoming_lane)
        outgoing = state.lane(link.outgoing_lane)
        movement_id = movement_id_for(intersection.tls_id, link)
        return {
            "mode": mode,
            "sample_interval": sample_interval,
            "decision_interval": control.decision_interval,
            "min_green": control.min_green,
            "max_green": control.max_green,
            "blocked_occupancy": control.blocked_occupancy,
            "downstream_weight": control.downstream_weight,
            "area_pressure_weight": control.area_pressure_weight,
            "queue_forecast_weight": control.queue_forecast_weight,
            "hysteresis": control.hysteresis,
            "priority_distance": control.priority_distance,
            "max_priority_override": control.max_priority_override,
            "clearance_seconds": control.clearance_seconds,
            "time": int(simulation_time),
            "tls_id": intersection.tls_id,
            "movement_hash": stable_hash(movement_id),
            "incoming_lane_hash": stable_hash(link.incoming_lane),
            "outgoing_lane_hash": stable_hash(link.outgoing_lane),
            "signal_index": link.signal_index,
            "signal_state": signal_state,
            "is_green": int(signal_state in "Gg"),
            "current_phase": phase_index,
            "phase_state": phase_state,
            "phase_elapsed": round(phase_elapsed, 3),
            "phase_count": len(intersection.phases),
            "incoming_queue": incoming.queue,
            "incoming_vehicle_count": incoming.vehicle_count,
            "incoming_occupancy": round(incoming.occupancy, 5),
            "incoming_mean_speed": round(incoming.mean_speed, 5),
            "incoming_free_slots": round(incoming.free_slots, 5),
            "outgoing_queue": outgoing.queue,
            "outgoing_vehicle_count": outgoing.vehicle_count,
            "outgoing_occupancy": round(outgoing.occupancy, 5),
            "outgoing_mean_speed": round(outgoing.mean_speed, 5),
            "outgoing_free_slots": round(outgoing.free_slots, 5),
            "downstream_blocked": int(outgoing.occupancy >= control.blocked_occupancy),
        }


class QueueForecastEnsemble:
    """Blend multiple horizon-specific queue forecasts into one score signal."""

    def __init__(
        self,
        weighted_models: tuple[tuple[QueueForecastModel, float], ...],
    ) -> None:
        if not weighted_models:
            raise ValueError("QueueForecastEnsemble needs at least one model")
        self._weighted_models = weighted_models

    @property
    def stats(self) -> QueueForecastStats:
        models = tuple(model for model, _weight in self._weighted_models)
        weights = tuple(weight for _model, weight in self._weighted_models)
        feature_counts = {model.stats.feature_count for model in models}
        return QueueForecastStats(
            model_path=";".join(model.stats.model_path for model in models),
            target=";".join(model.stats.target for model in models),
            feature_count=max(feature_counts) if feature_counts else 0,
            model_count=len(models),
            horizons=",".join(
                str(model.horizon_seconds)
                for model in models
                if model.horizon_seconds is not None
            ),
            horizon_weights=",".join(
                horizon_weight_label(model.horizon_seconds, weight)
                for model, weight in self._weighted_models
            ),
        )

    def predict_intersection(
        self,
        *,
        mode: str,
        simulation_time: float,
        intersection: Intersection,
        state: TrafficState,
        current_phase: int,
        phase_elapsed: float,
        control: ControlConfig,
        sample_interval: int,
    ) -> dict[tuple[int, int], float]:
        totals: dict[tuple[int, int], float] = {}
        weights: dict[tuple[int, int], float] = {}
        for model, weight in self._weighted_models:
            if weight <= 0.0:
                continue
            predictions = model.predict_intersection(
                mode=mode,
                simulation_time=simulation_time,
                intersection=intersection,
                state=state,
                current_phase=current_phase,
                phase_elapsed=phase_elapsed,
                control=control,
                sample_interval=sample_interval,
            )
            for key, prediction in predictions.items():
                totals[key] = totals.get(key, 0.0) + prediction * weight
                weights[key] = weights.get(key, 0.0) + weight
        return {
            key: value / weights[key]
            for key, value in totals.items()
            if weights.get(key, 0.0) > 0.0
        }


def load_queue_forecast_models(
    model_paths: tuple[Path, ...],
    horizon_weights: tuple[tuple[int, float], ...],
) -> QueueForecastModel | QueueForecastEnsemble | None:
    weights_by_horizon = dict(horizon_weights)
    weighted_models: list[tuple[QueueForecastModel, float]] = []
    for path in model_paths:
        model_path = path.resolve()
        if not model_path.is_file():
            continue
        model = QueueForecastModel.load(model_path)
        weight = weights_by_horizon.get(model.horizon_seconds, 1.0)
        if weight > 0.0:
            weighted_models.append((model, weight))

    if not weighted_models:
        return None
    if len(weighted_models) == 1:
        return weighted_models[0][0]
    return QueueForecastEnsemble(tuple(weighted_models))


def movement_id_for(tls_id: str, link: ControlledLink) -> str:
    return (
        f"{tls_id}|{link.incoming_lane}|{link.outgoing_lane}|"
        f"{link.signal_index}"
    )


def stable_hash(value: object) -> int:
    digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "little", signed=False)


def sanitized_prediction(value: object) -> float:
    prediction = float(value)
    if not math.isfinite(prediction):
        return 0.0
    return max(prediction, 0.0)


def horizon_from_target(target: str) -> int | None:
    match = re.search(r"_(\d+)s$", target)
    if match is None:
        return None
    return int(match.group(1))


def horizon_weight_label(horizon: int | None, weight: float) -> str:
    prefix = f"{horizon}s" if horizon is not None else "unknown"
    return f"{prefix}:{weight:g}"
