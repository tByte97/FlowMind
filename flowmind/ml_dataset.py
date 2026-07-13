from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .area_model import AreaModel
from .config import ControlConfig
from .traffic_state import TrafficStateReader
from .zone_graph import AreaGraph


ML_DATASET_SCHEMA_VERSION = 3
HISTORY_WINDOWS_SECONDS = (15, 30)


@dataclass(frozen=True)
class MLDatasetConfig:
    output_dir: Path
    run_id: str
    scenario: str
    mode: str
    seed: int
    duration: int
    sample_interval: int = 5
    target_horizons: tuple[int, ...] = (30, 60, 90)
    dataset_fingerprint: str = ""
    demand_profile: str = "normal"
    demand_scale: float = 1.0
    emergency_active: bool = False


class MLDatasetCollector:
    """Collect lane/movement features for queue prediction models."""

    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        control: ControlConfig,
        config: MLDatasetConfig,
        area_graph: AreaGraph | None = None,
    ) -> None:
        self._traci = traci_connection
        self._area = area
        self._control = control
        self._config = config
        self._area_graph = area_graph
        self._reader = TrafficStateReader(
            traci_connection,
            area,
            control.sensor_range_meters,
            area_graph.monitored_lane_ids if area_graph is not None else (),
        )
        self._rows: list[dict[str, object]] = []
        self._next_sample_at = 0.0
        self._lane_history: dict[str, list[tuple[float, int, int]]] = {}

    @property
    def output_path(self) -> Path:
        return self._config.output_dir / f"{self._config.run_id}.csv"

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def collect(self, simulation_time: float) -> None:
        if not self._sample_is_due(simulation_time):
            return

        state = self._reader.read(simulation_time)
        history_features = {
            lane_id: self._historical_features(
                lane_id,
                simulation_time,
                lane.queue,
                lane.vehicle_count,
            )
            for lane_id, lane in state.lanes.items()
        }
        for intersection in self._area.intersections:
            tls_id = intersection.tls_id
            current_phase = self._safe_int(
                self._traci.trafficlight.getPhase,
                tls_id,
                default=-1,
            )
            spent_duration = self._safe_float(
                self._traci.trafficlight.getSpentDuration,
                tls_id,
                default=0.0,
            )
            phase_state = (
                intersection.phases[current_phase]
                if 0 <= current_phase < len(intersection.phases)
                else ""
            )
            neighbour_features = self._neighbour_features(tls_id, state)

            for link in intersection.links:
                incoming = state.lane(link.incoming_lane)
                outgoing = state.lane(link.outgoing_lane)
                signal = (
                    phase_state[link.signal_index]
                    if link.signal_index < len(phase_state)
                    else ""
                )
                downstream_blocked = (
                    outgoing.occupancy >= self._control.blocked_occupancy
                )
                self._rows.append(
                    {
                        "dataset_schema_version": ML_DATASET_SCHEMA_VERSION,
                        "dataset_schema_sha256": ml_dataset_schema_sha256(),
                        "dataset_fingerprint": self._config.dataset_fingerprint,
                        "run_id": self._config.run_id,
                        "scenario": self._config.scenario,
                        "mode": self._config.mode,
                        "demand_profile": self._config.demand_profile,
                        "demand_scale": self._config.demand_scale,
                        "emergency_active": int(self._config.emergency_active),
                        "seed": self._config.seed,
                        "duration": self._config.duration,
                        "sample_interval": self._config.sample_interval,
                        "decision_interval": self._control.decision_interval,
                        "sensor_range_meters": self._control.sensor_range_meters,
                        "min_green": self._control.min_green,
                        "max_green": self._control.max_green,
                        "use_default_phase_timing": int(
                            self._control.use_default_phase_timing
                        ),
                        "default_green_extension": (
                            self._control.default_green_extension
                        ),
                        "blocked_occupancy": self._control.blocked_occupancy,
                        "downstream_weight": self._control.downstream_weight,
                        "area_pressure_weight": self._control.area_pressure_weight,
                        "queue_forecast_weight": self._control.queue_forecast_weight,
                        "empty_approach_penalty": (
                            self._control.empty_approach_penalty
                        ),
                        "empty_phase_penalty": self._control.empty_phase_penalty,
                        "congested_queue_threshold": (
                            self._control.congested_queue_threshold
                        ),
                        "congested_occupancy_threshold": (
                            self._control.congested_occupancy_threshold
                        ),
                        "congested_approach_bonus": (
                            self._control.congested_approach_bonus
                        ),
                        "demand_timer_seconds": self._control.demand_timer_seconds,
                        "demand_wait_weight": self._control.demand_wait_weight,
                        "max_demand_wait_bonus": (
                            self._control.max_demand_wait_bonus
                        ),
                        "hysteresis": self._control.hysteresis,
                        "priority_distance": self._control.priority_distance,
                        "max_priority_override": self._control.max_priority_override,
                        "clearance_seconds": self._control.clearance_seconds,
                        "time": int(simulation_time),
                        "tls_id": tls_id,
                        "movement_id": self._movement_id(tls_id, link),
                        "incoming_lane": link.incoming_lane,
                        "outgoing_lane": link.outgoing_lane,
                        "signal_index": link.signal_index,
                        "signal_state": signal,
                        "is_green": int(signal in "Gg"),
                        "current_phase": current_phase,
                        "candidate_phase": current_phase,
                        "action_phase": current_phase,
                        "action_is_observed": 1,
                        "phase_state": phase_state,
                        "candidate_phase_state": phase_state,
                        "phase_elapsed": round(spent_duration, 3),
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
                        "downstream_blocked": int(downstream_blocked),
                        **history_features.get(link.incoming_lane, {}),
                        **neighbour_features,
                    }
                )

        for lane_id, lane in state.lanes.items():
            history = self._lane_history.setdefault(lane_id, [])
            history.append(
                (float(simulation_time), int(lane.queue), int(lane.vehicle_count))
            )
            cutoff = float(simulation_time) - max(HISTORY_WINDOWS_SECONDS) - 5.0
            while len(history) > 1 and history[1][0] < cutoff:
                history.pop(0)

    def _sample_is_due(self, simulation_time: float) -> bool:
        now = float(simulation_time)
        if now + 1e-9 < self._next_sample_at:
            return False
        interval = float(self._config.sample_interval)
        elapsed_intervals = int((now - self._next_sample_at) // interval) + 1
        self._next_sample_at += elapsed_intervals * interval
        return True

    def write(self) -> dict[str, object]:
        self._config.output_dir.mkdir(parents=True, exist_ok=True)
        rows = self._with_targets()
        fieldnames = self._fieldnames(rows)
        with self.output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return {
            "dataset_csv": str(self.output_path),
            "dataset_rows": len(rows),
            "dataset_sample_interval": self._config.sample_interval,
            "dataset_target_horizons": ",".join(
                str(value) for value in self._config.target_horizons
            ),
            "dataset_schema_version": ML_DATASET_SCHEMA_VERSION,
            "dataset_schema_sha256": ml_dataset_schema_sha256(),
            "dataset_fingerprint": self._config.dataset_fingerprint,
        }

    def _with_targets(self) -> list[dict[str, object]]:
        by_movement_time = {
            (str(row["movement_id"]), int(row["time"])): row
            for row in self._rows
        }
        enriched: list[dict[str, object]] = []
        for row in self._rows:
            enriched_row = dict(row)
            movement_id = str(row["movement_id"])
            time = int(row["time"])
            for horizon in self._config.target_horizons:
                future = by_movement_time.get((movement_id, time + horizon))
                suffix = f"{horizon}s"
                enriched_row[f"target_incoming_queue_{suffix}"] = (
                    future["incoming_queue"] if future is not None else ""
                )
                enriched_row[f"target_incoming_occupancy_{suffix}"] = (
                    future["incoming_occupancy"] if future is not None else ""
                )
                enriched_row[f"target_outgoing_occupancy_{suffix}"] = (
                    future["outgoing_occupancy"] if future is not None else ""
                )
                enriched_row[f"target_downstream_blocked_{suffix}"] = (
                    future["downstream_blocked"] if future is not None else ""
                )
                enriched_row[f"target_delta_queue_{suffix}"] = (
                    int(future["incoming_queue"]) - int(row["incoming_queue"])
                    if future is not None
                    else ""
                )
                enriched_row[f"target_queue_reduction_{suffix}"] = (
                    int(row["incoming_queue"]) - int(future["incoming_queue"])
                    if future is not None
                    else ""
                )
                enriched_row[f"target_future_waiting_{suffix}"] = (
                    future["incoming_queue"] if future is not None else ""
                )
                enriched_row[f"target_discharged_vehicles_{suffix}"] = (
                    max(
                        int(row["incoming_vehicle_count"])
                        - int(future["incoming_vehicle_count"]),
                        0,
                    )
                    if future is not None
                    else ""
                )
            enriched.append(enriched_row)
        return enriched

    def _historical_features(
        self,
        lane_id: str,
        simulation_time: float,
        queue: int,
        vehicle_count: int,
    ) -> dict[str, float]:
        history = self._lane_history.get(lane_id, ())
        features: dict[str, float] = {}
        for window in HISTORY_WINDOWS_SECONDS:
            previous = next(
                (
                    item
                    for item in reversed(history)
                    if item[0] <= float(simulation_time) - window + 1e-9
                ),
                None,
            )
            previous_queue = int(previous[1]) if previous is not None else int(queue)
            previous_count = (
                int(previous[2]) if previous is not None else int(vehicle_count)
            )
            elapsed = max(
                float(simulation_time) - float(previous[0])
                if previous is not None
                else float(window),
                1.0,
            )
            queue_growth = int(queue) - previous_queue
            count_delta = int(vehicle_count) - previous_count
            features[f"incoming_queue_growth_{window}s"] = float(queue_growth)
            features[f"arrival_rate_{window}s"] = max(count_delta, 0) / elapsed
            features[f"discharge_rate_{window}s"] = max(-count_delta, 0) / elapsed
        return features

    def _neighbour_features(
        self,
        tls_id: str,
        state: Any,
    ) -> dict[str, float]:
        if self._area_graph is None:
            return {
                "upstream_neighbour_queue": 0.0,
                "downstream_neighbour_occupancy": 0.0,
                "downstream_storage_slots": 0.0,
                "platoon_arrival_30s": 0.0,
            }
        incoming = self._area_graph.incoming_segments(tls_id)
        outgoing = self._area_graph.outgoing_segments(tls_id)
        upstream_queue = sum(
            state.lane(lane_id).queue
            for segment in incoming
            for lane_id in segment.lane_ids
        )
        downstream_lanes = tuple(
            lane_id
            for segment in outgoing
            for lane_id in segment.lane_ids
        )
        downstream_occupancy = max(
            (state.lane(lane_id).occupancy for lane_id in downstream_lanes),
            default=0.0,
        )
        downstream_storage = sum(
            max(segment.capacity_slots, 0.0)
            for segment in outgoing
        )
        platoon = sum(
            state.lane(lane_id).vehicle_count
            for segment in incoming
            if segment.length_meters / 13.9 <= 30.0
            for lane_id in segment.lane_ids
        )
        return {
            "upstream_neighbour_queue": float(upstream_queue),
            "downstream_neighbour_occupancy": float(downstream_occupancy),
            "downstream_storage_slots": float(downstream_storage),
            "platoon_arrival_30s": float(platoon),
        }

    @staticmethod
    def _fieldnames(rows: list[dict[str, object]]) -> list[str]:
        if not rows:
            return []
        return list(rows[0].keys())

    @staticmethod
    def _movement_id(tls_id: str, link: Any) -> str:
        return (
            f"{tls_id}|{link.incoming_lane}|{link.outgoing_lane}|"
            f"{link.signal_index}"
        )

    @staticmethod
    def _safe_float(function: Any, *args: object, default: float) -> float:
        try:
            return float(function(*args))
        except Exception:
            return default

    @staticmethod
    def _safe_int(function: Any, *args: object, default: int) -> int:
        try:
            return int(function(*args))
        except Exception:
            return default


def ml_dataset_schema_sha256() -> str:
    payload = {
        "version": ML_DATASET_SCHEMA_VERSION,
        "history_windows": HISTORY_WINDOWS_SECONDS,
        "action_contract": "observed_action_candidate_phase",
        "targets": (
            "incoming_queue",
            "incoming_occupancy",
            "outgoing_occupancy",
            "downstream_blocked",
            "delta_queue",
            "queue_reduction",
            "future_waiting",
            "discharged_vehicles",
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
