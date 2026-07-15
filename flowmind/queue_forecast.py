from __future__ import annotations

import hashlib
import json
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
    forecast_contract: str = "current_policy"
    artifact_sha256: str = ""
    dataset_sha256: str = ""
    network_sha256: str = ""
    feature_schema_sha256: str = ""
    zone_sha256: str = ""
    dataset_fingerprint: str = ""
    dataset_schema_version: str = ""
    dataset_schema_sha256: str = ""
    known_tls_count: int = 0
    known_lane_count: int = 0
    feature_schema_valid: bool = False
    artifact_hash_valid: bool = False


@dataclass(frozen=True)
class ForecastDiagnostics:
    forecast_contract: str
    confidence: float
    ood: bool
    reasons: tuple[str, ...]
    influence_allowed: bool


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
        self._forecast_contract = str(
            metadata.get("forecast_contract", "current_policy")
        )
        if self._forecast_contract not in {
            "current_policy",
            "counterfactual",
            "observational_action_conditioned",
        }:
            raise ValueError(
                f"Unsupported queue forecast contract: {self._forecast_contract}"
            )
        self._known_tls_ids = frozenset(
            str(value) for value in metadata.get("known_tls_ids", ())
        )
        self._known_lane_ids = frozenset(
            str(value) for value in metadata.get("known_lane_ids", ())
        )
        ranges = metadata.get("feature_ranges", {})
        self._feature_ranges = ranges if isinstance(ranges, dict) else {}
        domains = metadata.get("categorical_domains", {})
        self._categorical_domains = (
            {
                str(feature): frozenset(str(value) for value in values)
                for feature, values in domains.items()
                if isinstance(values, (list, tuple))
            }
            if isinstance(domains, dict)
            else {}
        )
        support = metadata.get("action_support_by_tls", {})
        self._action_support_by_tls = (
            {
                str(tls_id): frozenset(int(value) for value in values)
                for tls_id, values in support.items()
                if isinstance(values, (list, tuple))
            }
            if isinstance(support, dict)
            else {}
        )
        self._artifact_sha256 = _file_sha256(model_path)
        self._declared_artifact_sha256 = str(metadata.get("artifact_sha256", ""))
        self._dataset_sha256 = str(metadata.get("dataset_sha256", ""))
        self._network_sha256 = str(metadata.get("network_sha256", ""))
        self._zone_sha256 = str(metadata.get("zone_sha256", ""))
        self._dataset_fingerprint = str(metadata.get("dataset_fingerprint", ""))
        self._dataset_schema_version = str(
            metadata.get("dataset_schema_version", "")
        )
        self._dataset_schema_sha256 = str(
            metadata.get("dataset_schema_sha256", "")
        )
        self._feature_schema_sha256 = str(
            metadata.get("feature_schema_sha256", "")
        )
        self._computed_feature_schema_sha256 = feature_schema_sha256(
            self._feature_columns,
            tuple(str(value) for value in metadata.get("numeric_features", ())),
            tuple(
                str(value) for value in metadata.get("categorical_features", ())
            ),
            self._forecast_contract,
        )
        self._legacy_hash_schema = any(
            column in self._feature_columns
            for column in ("movement_hash", "incoming_lane_hash", "outgoing_lane_hash")
        )
        self._last_diagnostics = ForecastDiagnostics(
            forecast_contract=self._forecast_contract,
            confidence=0.0,
            ood=True,
            reasons=("not_evaluated",),
            influence_allowed=False,
        )

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
        sidecar_path = model_path.with_name(f"{model_path.stem}_metadata.json")
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            sidecar = {}
        if isinstance(sidecar, dict):
            metadata = {**metadata, **sidecar}
        return cls(pipeline, pd.DataFrame, model_path, metadata)

    @property
    def stats(self) -> QueueForecastStats:
        return QueueForecastStats(
            model_path=str(self._model_path),
            target=self._target,
            feature_count=len(self._feature_columns),
            horizons=(str(self._horizon_seconds) if self._horizon_seconds else ""),
            forecast_contract=self._forecast_contract,
            artifact_sha256=self._artifact_sha256,
            dataset_sha256=self._dataset_sha256,
            network_sha256=self._network_sha256,
            feature_schema_sha256=self._feature_schema_sha256,
            zone_sha256=self._zone_sha256,
            dataset_fingerprint=self._dataset_fingerprint,
            dataset_schema_version=self._dataset_schema_version,
            dataset_schema_sha256=self._dataset_schema_sha256,
            known_tls_count=len(self._known_tls_ids),
            known_lane_count=len(self._known_lane_ids),
            feature_schema_valid=(
                bool(self._feature_schema_sha256)
                and self._feature_schema_sha256
                == self._computed_feature_schema_sha256
            ),
            artifact_hash_valid=(
                bool(self._declared_artifact_sha256)
                and bool(self._artifact_sha256)
                and self._declared_artifact_sha256 == self._artifact_sha256
            ),
        )

    @property
    def known_tls_ids(self) -> frozenset[str]:
        return self._known_tls_ids

    @property
    def known_lane_ids(self) -> frozenset[str]:
        return self._known_lane_ids

    @property
    def horizon_seconds(self) -> int | None:
        return self._horizon_seconds

    @property
    def prediction_target(self) -> str:
        return self._target

    @property
    def evaluation_horizon_seconds(self) -> int:
        return self._horizon_seconds or 0

    @property
    def last_diagnostics(self) -> ForecastDiagnostics:
        return self._last_diagnostics

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
        demand_profile: str = "normal",
        context_by_lane: dict[str, dict[str, float]] | None = None,
        area_context_by_lane: dict[str, dict[str, float]] | None = None,
    ) -> dict[tuple[int, int], float]:
        if not 0 <= current_phase < len(intersection.phases):
            self._last_diagnostics = ForecastDiagnostics(
                self._forecast_contract,
                0.0,
                True,
                ("current_phase_out_of_range",),
                False,
            )
            return {}
        current_state = intersection.phases[current_phase]
        rows: list[dict[str, Any]] = []
        row_keys: list[tuple[tuple[int, int], ...]] = []
        if self._forecast_contract == "current_policy":
            for link_index, link in enumerate(intersection.links):
                signal_state = (
                    current_state[link.signal_index]
                    if link.signal_index < len(current_state)
                    else ""
                )
                candidate_keys = tuple(
                    (phase_index, link_index)
                    for phase_index in intersection.green_phase_indices
                    if link.signal_index < len(intersection.phases[phase_index])
                    and intersection.phases[phase_index][link.signal_index] in "Gg"
                )
                if not candidate_keys:
                    continue
                rows.append(
                    self._feature_row(
                        mode=mode,
                        simulation_time=simulation_time,
                        intersection=intersection,
                        link=link,
                        signal_state=signal_state,
                        current_phase=current_phase,
                        phase_state=current_state,
                        phase_elapsed=phase_elapsed,
                        candidate_phase=None,
                        state=state,
                        control=control,
                        sample_interval=sample_interval,
                        demand_profile=demand_profile,
                        context_by_lane=context_by_lane,
                        area_context_by_lane=area_context_by_lane,
                    )
                )
                row_keys.append(candidate_keys)
        else:
            for phase_index in intersection.green_phase_indices:
                candidate_state = intersection.phases[phase_index]
                for link_index, link in enumerate(intersection.links):
                    if link.signal_index >= len(candidate_state):
                        continue
                    if candidate_state[link.signal_index] not in "Gg":
                        continue
                    signal_state = (
                        candidate_state[link.signal_index]
                        if link.signal_index < len(candidate_state)
                        else ""
                    )
                    rows.append(
                        self._feature_row(
                            mode=mode,
                            simulation_time=simulation_time,
                            intersection=intersection,
                            link=link,
                            signal_state=signal_state,
                            current_phase=current_phase,
                            phase_state=current_state,
                            phase_elapsed=phase_elapsed,
                            candidate_phase=phase_index,
                            state=state,
                            control=control,
                            sample_interval=sample_interval,
                            demand_profile=demand_profile,
                            context_by_lane=context_by_lane,
                            area_context_by_lane=area_context_by_lane,
                        )
                    )
                    row_keys.append(((phase_index, link_index),))

        if not rows:
            return {}

        frame = self._dataframe_factory(rows)
        frame = frame.reindex(columns=self._feature_columns, fill_value=0)
        predictions = self._pipeline.predict(frame)
        diagnostics = self._diagnostics(intersection, rows)
        self._last_diagnostics = diagnostics
        result: dict[tuple[int, int], float] = {}
        for keys, prediction in zip(row_keys, predictions, strict=False):
            link_index = keys[0][1]
            incoming = state.lane(intersection.links[link_index].incoming_lane)
            capacity = max(
                float(incoming.vehicle_count) + float(incoming.free_slots),
                float(incoming.queue),
                1.0,
            )
            bounded = sanitized_prediction(
                prediction,
                upper_bound=capacity,
                allow_negative="queue_reduction" in self._target
                or "delta_queue" in self._target,
            )
            for key in keys:
                result[key] = bounded
        return result

    def _feature_row(
        self,
        *,
        mode: str,
        simulation_time: float,
        intersection: Intersection,
        link: ControlledLink,
        signal_state: str,
        current_phase: int,
        phase_state: str,
        phase_elapsed: float,
        candidate_phase: int | None,
        state: TrafficState,
        control: ControlConfig,
        sample_interval: int,
        demand_profile: str,
        context_by_lane: dict[str, dict[str, float]] | None,
        area_context_by_lane: dict[str, dict[str, float]] | None,
    ) -> dict[str, Any]:
        incoming = state.lane(link.incoming_lane)
        outgoing = state.lane(link.outgoing_lane)
        movement_id = movement_id_for(intersection.tls_id, link)
        context = (context_by_lane or {}).get(link.incoming_lane, {})
        incoming_area = (area_context_by_lane or {}).get(link.incoming_lane, {})
        outgoing_area = (area_context_by_lane or {}).get(link.outgoing_lane, {})
        return {
            "mode": mode,
            "demand_profile": demand_profile,
            "demand_scale": 1.0,
            "sample_interval": sample_interval,
            "decision_interval": control.decision_interval,
            "sensor_range_meters": control.sensor_range_meters,
            "min_green": control.min_green,
            "max_green": control.max_green,
            "use_default_phase_timing": int(control.use_default_phase_timing),
            "default_green_extension": control.default_green_extension,
            "blocked_occupancy": control.blocked_occupancy,
            "downstream_weight": control.downstream_weight,
            "area_pressure_weight": control.area_pressure_weight,
            "queue_forecast_weight": control.queue_forecast_weight,
            "empty_approach_penalty": control.empty_approach_penalty,
            "empty_phase_penalty": control.empty_phase_penalty,
            "congested_queue_threshold": control.congested_queue_threshold,
            "congested_occupancy_threshold": control.congested_occupancy_threshold,
            "congested_approach_bonus": control.congested_approach_bonus,
            "demand_timer_seconds": control.demand_timer_seconds,
            "demand_wait_weight": control.demand_wait_weight,
            "max_demand_wait_bonus": control.max_demand_wait_bonus,
            "hysteresis": control.hysteresis,
            "priority_distance": control.priority_distance,
            "max_priority_override": control.max_priority_override,
            "clearance_seconds": control.clearance_seconds,
            "time": int(simulation_time),
            "tls_id": intersection.tls_id,
            "movement_id": movement_id,
            "incoming_lane": link.incoming_lane,
            "outgoing_lane": link.outgoing_lane,
            "movement_hash": stable_hash(movement_id),
            "incoming_lane_hash": stable_hash(link.incoming_lane),
            "outgoing_lane_hash": stable_hash(link.outgoing_lane),
            "signal_index": link.signal_index,
            "current_signal_state": (
                phase_state[link.signal_index]
                if link.signal_index < len(phase_state)
                else ""
            ),
            "signal_state": signal_state,
            "is_green": int(signal_state in "Gg"),
            "current_phase": current_phase,
            "candidate_phase": (
                candidate_phase if candidate_phase is not None else current_phase
            ),
            "action_phase": (
                candidate_phase if candidate_phase is not None else current_phase
            ),
            "action_is_observed": int(candidate_phase is None),
            "phase_state": phase_state,
            "candidate_phase_state": intersection.phases[
                candidate_phase if candidate_phase is not None else current_phase
            ],
            "action_phase_state": intersection.phases[
                candidate_phase if candidate_phase is not None else current_phase
            ],
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
            "incoming_queue_growth_15s": context.get(
                "incoming_queue_growth_15s", 0.0
            ),
            "incoming_queue_growth_30s": context.get(
                "incoming_queue_growth_30s", 0.0
            ),
            "arrival_rate_15s": context.get("arrival_rate_15s", 0.0),
            "arrival_rate_30s": context.get("arrival_rate_30s", 0.0),
            "discharge_rate_15s": context.get("discharge_rate_15s", 0.0),
            "discharge_rate_30s": context.get("discharge_rate_30s", 0.0),
            "upstream_neighbour_queue": incoming_area.get(
                "upstream_neighbour_queue", 0.0
            ),
            "downstream_neighbour_occupancy": outgoing_area.get(
                "downstream_neighbour_occupancy", 0.0
            ),
            "downstream_storage_slots": outgoing_area.get(
                "downstream_storage_slots", 0.0
            ),
            "platoon_arrival_30s": incoming_area.get(
                "platoon_arrival_30s", 0.0
            ),
        }

    def _diagnostics(
        self,
        intersection: Intersection,
        rows: list[dict[str, Any]],
    ) -> ForecastDiagnostics:
        reasons: list[str] = []
        if not self._known_tls_ids or not self._known_lane_ids:
            reasons.append("artifact_training_domain_missing")
        else:
            if intersection.tls_id not in self._known_tls_ids:
                reasons.append("unseen_tls")
            lanes = {
                lane_id
                for link in intersection.links
                for lane_id in (link.incoming_lane, link.outgoing_lane)
            }
            if not lanes.issubset(self._known_lane_ids):
                reasons.append("unseen_lane")
        if not self._feature_ranges:
            reasons.append("feature_ranges_missing")
        else:
            for row in rows:
                for feature, bounds in self._feature_ranges.items():
                    if feature not in row or not isinstance(bounds, (list, tuple)):
                        continue
                    if len(bounds) != 2:
                        continue
                    try:
                        value = float(row[feature])
                        minimum = float(bounds[0])
                        maximum = float(bounds[1])
                    except (TypeError, ValueError):
                        continue
                    if value < minimum or value > maximum:
                        reasons.append(f"feature_out_of_range:{feature}")
                        break
        if (
            self._forecast_contract == "observational_action_conditioned"
            and not self._categorical_domains
        ):
            reasons.append("categorical_domains_missing")
        elif self._categorical_domains:
            for row in rows:
                for feature, domain in self._categorical_domains.items():
                    if feature in row and str(row[feature]) not in domain:
                        reasons.append(f"categorical_ood:{feature}")
        if self._forecast_contract == "observational_action_conditioned":
            supported_actions = self._action_support_by_tls.get(intersection.tls_id)
            if not supported_actions:
                reasons.append("action_support_missing")
            else:
                for row in rows:
                    try:
                        action = int(row["action_phase"])
                    except (KeyError, TypeError, ValueError):
                        reasons.append("action_phase_invalid")
                        continue
                    if action not in supported_actions:
                        reasons.append("unsupported_action")
        if self._legacy_hash_schema:
            reasons.append("legacy_numeric_hash_schema")
        if not self._dataset_sha256:
            reasons.append("dataset_hash_missing")
        if not self._network_sha256:
            reasons.append("network_hash_missing")
        if not self._feature_schema_sha256:
            reasons.append("feature_schema_hash_missing")
        elif self._feature_schema_sha256 != self._computed_feature_schema_sha256:
            reasons.append("feature_schema_hash_mismatch")
        if not self._declared_artifact_sha256:
            reasons.append("artifact_hash_missing")
        elif self._artifact_sha256 and self._declared_artifact_sha256 != self._artifact_sha256:
            reasons.append("artifact_hash_mismatch")
        unique_reasons = tuple(dict.fromkeys(reasons))
        confidence = max(0.0, 1.0 - 0.2 * len(unique_reasons))
        ood = bool(unique_reasons)
        return ForecastDiagnostics(
            forecast_contract=self._forecast_contract,
            confidence=confidence,
            ood=ood,
            reasons=unique_reasons,
            influence_allowed=not ood,
        )


class QueueForecastEnsemble:
    """Blend multiple horizon-specific queue forecasts into one score signal."""

    def __init__(
        self,
        weighted_models: tuple[tuple[QueueForecastModel, float], ...],
    ) -> None:
        if not weighted_models:
            raise ValueError("QueueForecastEnsemble needs at least one model")
        self._weighted_models = weighted_models
        self._last_diagnostics = ForecastDiagnostics(
            "current_policy", 0.0, True, ("not_evaluated",), False
        )

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
            forecast_contract=";".join(
                sorted({model.stats.forecast_contract for model in models})
            ),
            artifact_sha256=";".join(model.stats.artifact_sha256 for model in models),
            dataset_sha256=";".join(model.stats.dataset_sha256 for model in models),
            network_sha256=";".join(model.stats.network_sha256 for model in models),
            feature_schema_sha256=";".join(
                model.stats.feature_schema_sha256 for model in models
            ),
            zone_sha256=";".join(model.stats.zone_sha256 for model in models),
            dataset_fingerprint=";".join(
                model.stats.dataset_fingerprint for model in models
            ),
            dataset_schema_version=";".join(
                model.stats.dataset_schema_version for model in models
            ),
            dataset_schema_sha256=";".join(
                model.stats.dataset_schema_sha256 for model in models
            ),
            known_tls_count=len(self.known_tls_ids),
            known_lane_count=len(self.known_lane_ids),
            feature_schema_valid=all(
                model.stats.feature_schema_valid for model in models
            ),
            artifact_hash_valid=all(
                model.stats.artifact_hash_valid for model in models
            ),
        )

    @property
    def known_tls_ids(self) -> frozenset[str]:
        domains = [
            model.known_tls_ids
            for model, weight in self._weighted_models
            if weight > 0.0
        ]
        if not domains:
            return frozenset()
        return frozenset.intersection(*domains)

    @property
    def known_lane_ids(self) -> frozenset[str]:
        domains = [
            model.known_lane_ids
            for model, weight in self._weighted_models
            if weight > 0.0
        ]
        if not domains:
            return frozenset()
        return frozenset.intersection(*domains)

    @property
    def last_diagnostics(self) -> ForecastDiagnostics:
        return self._last_diagnostics

    @property
    def evaluation_horizon_seconds(self) -> int:
        weighted = tuple(
            (model.horizon_seconds, weight)
            for model, weight in self._weighted_models
            if model.horizon_seconds is not None and weight > 0.0
        )
        total_weight = sum(weight for _horizon, weight in weighted)
        if not weighted or total_weight <= 0.0:
            return 0
        return int(
            round(
                sum(float(horizon) * weight for horizon, weight in weighted)
                / total_weight
            )
        )

    @property
    def prediction_target(self) -> str:
        targets = {
            model.prediction_target
            for model, weight in self._weighted_models
            if weight > 0.0
        }
        if not targets:
            return ""
        families = {_target_family(target) for target in targets}
        if len(families) != 1:
            return ";".join(sorted(targets))
        return next(iter(families))

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
        demand_profile: str = "normal",
        context_by_lane: dict[str, dict[str, float]] | None = None,
        area_context_by_lane: dict[str, dict[str, float]] | None = None,
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
                demand_profile=demand_profile,
                context_by_lane=context_by_lane,
                area_context_by_lane=area_context_by_lane,
            )
            for key, prediction in predictions.items():
                totals[key] = totals.get(key, 0.0) + prediction * weight
                weights[key] = weights.get(key, 0.0) + weight
        diagnostics = tuple(
            model.last_diagnostics for model, weight in self._weighted_models if weight > 0
        )
        reasons = tuple(
            dict.fromkeys(reason for item in diagnostics for reason in item.reasons)
        )
        self._last_diagnostics = ForecastDiagnostics(
            forecast_contract=";".join(
                sorted({item.forecast_contract for item in diagnostics})
            ),
            confidence=min((item.confidence for item in diagnostics), default=0.0),
            ood=any(item.ood for item in diagnostics),
            reasons=reasons,
            influence_allowed=bool(diagnostics)
            and all(item.influence_allowed for item in diagnostics),
        )
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


def sanitized_prediction(
    value: object,
    upper_bound: float | None = None,
    *,
    allow_negative: bool = False,
) -> float:
    prediction = float(value)
    if not math.isfinite(prediction):
        return 0.0
    if upper_bound is not None:
        bound = max(float(upper_bound), 0.0)
        lower = -bound if allow_negative else 0.0
        prediction = min(max(prediction, lower), bound)
    elif not allow_negative:
        prediction = max(prediction, 0.0)
    return prediction


def _target_family(target: str) -> str:
    for family in (
        "queue_reduction",
        "delta_queue",
        "discharged_vehicles",
        "future_waiting",
        "incoming_queue",
    ):
        if family in target:
            return family
    return target


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def file_sha256(path: Path) -> str:
    """Return a reproducible content hash for an artifact or network file."""

    return _file_sha256(path)


def dataset_sha256(paths: list[Path] | tuple[Path, ...]) -> str:
    """Hash dataset contents and stable relative names, independent of ordering."""

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        digest.update(b"\0")
    return digest.hexdigest() if paths else ""


def feature_schema_sha256(
    feature_columns: tuple[str, ...] | list[str],
    numeric_features: tuple[str, ...] | list[str],
    categorical_features: tuple[str, ...] | list[str],
    forecast_contract: str,
) -> str:
    payload = {
        "forecast_contract": forecast_contract,
        "feature_columns": list(feature_columns),
        "numeric_features": list(numeric_features),
        "categorical_features": list(categorical_features),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def horizon_from_target(target: str) -> int | None:
    match = re.search(r"_(\d+)s$", target)
    if match is None:
        return None
    return int(match.group(1))


def horizon_weight_label(horizon: int | None, weight: float) -> str:
    prefix = f"{horizon}s" if horizon is not None else "unknown"
    return f"{prefix}:{weight:g}"
