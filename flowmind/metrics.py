from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import fmean

from .area_model import AreaModel
from .config import ControlConfig


@dataclass(frozen=True)
class MetricSample:
    time: float
    active_vehicles: int
    mean_speed: float
    waiting_time: float
    queue_length: int
    max_queue_length: int
    throughput: int
    stops_count: int
    gridlock_risk: float


class MetricsCollector:
    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        control: ControlConfig,
        priority_vehicle: str | None = None,
    ) -> None:
        self._traci = traci_connection
        self._area = area
        self._control = control
        self._priority_vehicle = priority_vehicle
        self._previously_stopped: set[str] = set()
        self._departed_at: dict[str, float] = {}
        self._travel_times: list[float] = []
        self._throughput = 0
        self._stops = 0
        self._priority_departed: float | None = None
        self._priority_arrived: float | None = None
        self._priority_eta: float | None = None
        self.samples: list[MetricSample] = []

    def collect(self, simulation_time: float) -> None:
        departed = self._traci.simulation.getDepartedIDList()
        for vehicle_id in departed:
            self._departed_at[vehicle_id] = simulation_time
            if vehicle_id == self._priority_vehicle:
                self._priority_departed = simulation_time

        arrived = self._traci.simulation.getArrivedIDList()
        self._throughput += len(arrived)
        for vehicle_id in arrived:
            departed_at = self._departed_at.pop(vehicle_id, None)
            if departed_at is not None:
                duration = simulation_time - departed_at
                self._travel_times.append(duration)
                if vehicle_id == self._priority_vehicle:
                    self._priority_eta = duration
                    self._priority_arrived = simulation_time

        if int(simulation_time) % self._control.decision_interval:
            return

        lane_ids = set(self._area.incoming_lanes) | set(self._area.outgoing_lanes)
        queues = [
            int(self._traci.lane.getLastStepHaltingNumber(lane_id))
            for lane_id in lane_ids
        ]
        outgoing_occupancies = [
            min(
                max(
                    float(self._traci.lane.getLastStepOccupancy(lane_id)) / 100.0,
                    0.0,
                ),
                1.0,
            )
            for lane_id in self._area.outgoing_lanes
        ]
        zone_vehicles = {
            vehicle_id
            for lane_id in lane_ids
            for vehicle_id in self._traci.lane.getLastStepVehicleIDs(lane_id)
        }
        speeds = {
            vehicle_id: max(
                float(self._traci.vehicle.getSpeed(vehicle_id)), 0.0
            )
            for vehicle_id in zone_vehicles
        }
        waiting = [
            max(
                float(
                    self._traci.vehicle.getAccumulatedWaitingTime(vehicle_id)
                ),
                0.0,
            )
            for vehicle_id in zone_vehicles
        ]
        stopped = {
            vehicle_id
            for vehicle_id, speed in speeds.items()
            if speed < 0.1
        }
        self._stops += len(stopped - self._previously_stopped)
        self._previously_stopped = stopped
        blocked = sum(
            occupancy >= self._control.blocked_occupancy
            for occupancy in outgoing_occupancies
        )
        self.samples.append(
            MetricSample(
                time=simulation_time,
                active_vehicles=len(zone_vehicles),
                mean_speed=fmean(speeds.values()) if speeds else 0.0,
                waiting_time=fmean(waiting) if waiting else 0.0,
                queue_length=sum(queues),
                max_queue_length=max(queues, default=0),
                throughput=self._throughput,
                stops_count=self._stops,
                gridlock_risk=(
                    blocked / len(outgoing_occupancies)
                    if outgoing_occupancies
                    else 0.0
                ),
            )
        )

    def summary(self, mode: str, simulated_duration: float) -> dict[str, object]:
        return {
            "mode": mode,
            "simulated_duration": round(simulated_duration, 2),
            "controlled_tls": len(self._area.tls_ids),
            "tls_ids": ",".join(self._area.tls_ids),
            "average_travel_time": round(fmean(self._travel_times), 3)
            if self._travel_times
            else 0.0,
            "average_waiting_time": round(
                fmean(sample.waiting_time for sample in self.samples), 3
            )
            if self.samples
            else 0.0,
            "average_queue_length": round(
                fmean(sample.queue_length for sample in self.samples), 3
            )
            if self.samples
            else 0.0,
            "max_queue_length": max(
                (sample.max_queue_length for sample in self.samples), default=0
            ),
            "throughput": self._throughput,
            "stops_count": self._stops,
            "gridlock_risk": round(
                fmean(sample.gridlock_risk for sample in self.samples), 4
            )
            if self.samples
            else 0.0,
            "emergency_departure_time": self._priority_departed,
            "emergency_arrival_time": self._priority_arrived,
            "emergency_eta": self._priority_eta,
        }

    def write(self, results_dir: Path, summary: dict[str, object]) -> None:
        results_dir.mkdir(parents=True, exist_ok=True)
        mode = str(summary["mode"])
        timeseries_path = results_dir / f"{mode}_timeseries.csv"
        with timeseries_path.open("w", newline="", encoding="utf-8") as handle:
            fieldnames = list(asdict(self.samples[0]).keys()) if self.samples else []
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if fieldnames:
                writer.writeheader()
                writer.writerows(asdict(sample) for sample in self.samples)

        with (results_dir / f"{mode}_summary.json").open(
            "w", encoding="utf-8"
        ) as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)

        combined_path = results_dir / "summary.csv"
        rows: dict[str, dict[str, object]] = {}
        if combined_path.exists():
            with combined_path.open(newline="", encoding="utf-8") as handle:
                rows = {row["mode"]: row for row in csv.DictReader(handle)}
        rows[mode] = summary
        fieldnames = list(summary.keys())
        with combined_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row_mode in ("fixed", "local", "flowmind"):
                if row_mode in rows:
                    writer.writerow(rows[row_mode])
