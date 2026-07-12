from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .area_model import AreaModel
from .config import ControlConfig
from .traffic_state import TrafficStateReader


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


class MLDatasetCollector:
    """Collect lane/movement features for queue prediction models."""

    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        control: ControlConfig,
        config: MLDatasetConfig,
    ) -> None:
        self._traci = traci_connection
        self._area = area
        self._control = control
        self._config = config
        self._reader = TrafficStateReader(
            traci_connection,
            area,
            control.sensor_range_meters,
        )
        self._rows: list[dict[str, object]] = []

    @property
    def output_path(self) -> Path:
        return self._config.output_dir / f"{self._config.run_id}.csv"

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def collect(self, simulation_time: float) -> None:
        if int(simulation_time) % self._config.sample_interval:
            return

        state = self._reader.read(simulation_time)
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
                        "run_id": self._config.run_id,
                        "scenario": self._config.scenario,
                        "mode": self._config.mode,
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
                        "phase_state": phase_state,
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
                    }
                )

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
            enriched.append(enriched_row)
        return enriched

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
