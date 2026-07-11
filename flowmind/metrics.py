from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from .area_model import AreaModel
from .config import ControlConfig
from .live_transport import LiveTelemetryPublisher
from .traffic_state import TrafficStateReader


@dataclass(frozen=True)
class MetricSample:
    time: float
    active_vehicles: int
    departed: int
    arrived: int
    inflow_per_minute: float
    outflow_per_minute: float
    mean_speed: float
    waiting_time: float
    queue_length: int
    max_queue_length: int
    throughput: int
    stops_count: int
    gridlock_risk: float


@dataclass(frozen=True)
class EmergencyTraceSample:
    time: float
    vehicle_id: str
    edge_id: str
    lane_id: str
    route_index: int
    route_edge_count: int
    remaining_edges: int
    speed: float
    lane_position: float
    x: float
    y: float


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
        self._reader = TrafficStateReader(
            traci_connection,
            area,
            control.sensor_range_meters,
        )
        self._previously_stopped: set[str] = set()
        self._departed_at: dict[str, float] = {}
        self._travel_times: list[float] = []
        self._throughput = 0
        self._stops = 0
        self._priority_departed: float | None = None
        self._priority_arrived: float | None = None
        self._priority_eta: float | None = None
        self._departed_count = 0
        self._peak_active_vehicles = 0
        self.samples: list[MetricSample] = []
        self.emergency_trace: list[EmergencyTraceSample] = []
        self._last_live_status: dict[str, Any] | None = None
        self._publisher: LiveTelemetryPublisher | None = None
        self._visual_lanes = area.visual_lanes

    def collect(self, simulation_time: float) -> None:
        departed = self._traci.simulation.getDepartedIDList()
        self._departed_count += len(departed)
        for vehicle_id in departed:
            self._departed_at[vehicle_id] = simulation_time
            if vehicle_id == self._priority_vehicle:
                self._priority_departed = simulation_time

        arrived = self._traci.simulation.getArrivedIDList()
        self._throughput += len(arrived)
        self._peak_active_vehicles = max(
            self._peak_active_vehicles,
            len(self._departed_at),
        )
        for vehicle_id in arrived:
            departed_at = self._departed_at.pop(vehicle_id, None)
            if departed_at is not None:
                duration = simulation_time - departed_at
                self._travel_times.append(duration)
                if vehicle_id == self._priority_vehicle:
                    self._priority_eta = duration
                    self._priority_arrived = simulation_time

        self._collect_priority_trace(simulation_time)

        if int(simulation_time) % self._control.decision_interval:
            return

        traffic = self._reader.read()
        lane_ids = set(self._area.incoming_lanes) | set(self._area.outgoing_lanes)
        queues = [traffic.lane(lane_id).queue for lane_id in lane_ids]
        outgoing_occupancies = [
            traffic.lane(lane_id).occupancy for lane_id in self._area.outgoing_lanes
        ]
        zone_vehicles = {
            vehicle_id
            for lane_id in lane_ids
            for vehicle_id in traffic.lane(lane_id).vehicle_ids
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
        previous = self.samples[-1] if self.samples else None
        elapsed = simulation_time - previous.time if previous is not None else 0.0
        inflow_per_minute = (
            (self._departed_count - previous.departed) * 60.0 / elapsed
            if previous is not None and elapsed > 0
            else 0.0
        )
        outflow_per_minute = (
            (self._throughput - previous.arrived) * 60.0 / elapsed
            if previous is not None and elapsed > 0
            else 0.0
        )
        self.samples.append(
            MetricSample(
                time=simulation_time,
                active_vehicles=len(zone_vehicles),
                departed=self._departed_count,
                arrived=self._throughput,
                inflow_per_minute=round(inflow_per_minute, 3),
                outflow_per_minute=round(outflow_per_minute, 3),
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
            "departed_vehicles": self._departed_count,
            "peak_active_vehicles": self._peak_active_vehicles,
            "stops_count": self._stops,
            "gridlock_risk": round(
                fmean(sample.gridlock_risk for sample in self.samples), 4
            )
            if self.samples
            else 0.0,
            "emergency_departure_time": self._priority_departed,
            "emergency_arrival_time": self._priority_arrived,
            "emergency_eta": self._priority_eta,
            "emergency_trace_samples": len(self.emergency_trace),
            "sensor_range_meters": self._control.sensor_range_meters,
            "sensor_lanes": len(
                set(self._area.incoming_lanes) | set(self._area.outgoing_lanes)
            ),
        }

    def _collect_priority_trace(self, simulation_time: float) -> None:
        if self._priority_vehicle is None:
            return
        active = set(self._traci.vehicle.getIDList())
        if self._priority_vehicle not in active:
            return

        route = tuple(self._traci.vehicle.getRoute(self._priority_vehicle))
        route_index = int(self._traci.vehicle.getRouteIndex(self._priority_vehicle))
        position = self._traci.vehicle.getPosition(self._priority_vehicle)
        self.emergency_trace.append(
            EmergencyTraceSample(
                time=simulation_time,
                vehicle_id=self._priority_vehicle,
                edge_id=str(self._traci.vehicle.getRoadID(self._priority_vehicle)),
                lane_id=str(self._traci.vehicle.getLaneID(self._priority_vehicle)),
                route_index=route_index,
                route_edge_count=len(route),
                remaining_edges=max(len(route) - route_index - 1, 0),
                speed=round(
                    max(float(self._traci.vehicle.getSpeed(self._priority_vehicle)), 0.0),
                    3,
                ),
                lane_position=round(
                    max(
                        float(
                            self._traci.vehicle.getLanePosition(
                                self._priority_vehicle
                            )
                        ),
                        0.0,
                    ),
                    3,
                ),
                x=round(float(position[0]), 3),
                y=round(float(position[1]), 3),
            )
        )

    def set_live_publisher(self, publisher: LiveTelemetryPublisher | None) -> None:
        self._publisher = publisher

    def write_live_status(
        self,
        results_dir: Path,
        mode: str,
        simulation_time: float,
        system_status: dict[str, Any] | None = None,
        decision_log: list[dict[str, object]] | None = None,
    ) -> Path:
        results_dir.mkdir(parents=True, exist_ok=True)
        summary = self.summary(mode, simulation_time)
        latest_sample = asdict(self.samples[-1]) if self.samples else None
        lane_status = self._lane_status()
        intersection_status = self._intersection_status(lane_status)
        vehicles = self._vehicle_positions()
        active_network = len(self._departed_at)
        normalized_system_status = system_status or {}
        payload = {
            "schema_version": 2,
            "mode": mode,
            "results_dir": str(results_dir.resolve()),
            "simulated_time": round(float(simulation_time), 3),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "latest_sample": latest_sample,
            "metric_history": [
                asdict(sample) for sample in self.samples[-120:]
            ],
            "traffic_flow": {
                "active_network": active_network,
                "active_zone": (
                    int(latest_sample["active_vehicles"])
                    if latest_sample is not None
                    else 0
                ),
                "departed_total": self._departed_count,
                "arrived_total": self._throughput,
                "inflow_per_minute": (
                    float(latest_sample["inflow_per_minute"])
                    if latest_sample is not None
                    else 0.0
                ),
                "outflow_per_minute": (
                    float(latest_sample["outflow_per_minute"])
                    if latest_sample is not None
                    else 0.0
                ),
            },
            "sensor_model": {
                "type": "intersection_camera_detector",
                "coverage": "controlled_intersections_only",
                "range_meters": self._control.sensor_range_meters,
                "incoming_lanes": len(self._area.incoming_lanes),
                "outgoing_lanes": len(self._area.outgoing_lanes),
            },
            "lanes": lane_status,
            "intersections": intersection_status,
            "vehicles": vehicles,
            "zone_simulation": self._zone_simulation_payload(
                mode,
                simulation_time,
                intersection_status,
                lane_status,
                vehicles,
                normalized_system_status,
            ),
            "system": normalized_system_status,
            "decision_log": list(decision_log or ())[-120:],
            "emergency_trace": [
                asdict(item) for item in self.emergency_trace[-20:]
            ],
        }
        output_path = results_dir / "live_status.json"
        temporary_path = output_path.with_suffix(".json.tmp")
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary_path.replace(output_path)
        self._last_live_status = payload
        if self._publisher is not None:
            self._publisher.publish(payload)
        return output_path

    @staticmethod
    def _safe_call(callable_object: object, default: Any, *args: object) -> Any:
        try:
            return callable_object(*args)  # type: ignore[operator]
        except Exception:
            # Live diagnostics must never stop the simulation when an entity
            # disappears between two TraCI calls.
            return default

    @staticmethod
    def _color_hex(value: Any, fallback: str = "#4ddfd4") -> str:
        try:
            values = list(value) if isinstance(value, (list, tuple)) else []
            if len(values) < 3:
                return fallback
            return "#{:02x}{:02x}{:02x}".format(
                max(0, min(255, int(values[0]))),
                max(0, min(255, int(values[1]))),
                max(0, min(255, int(values[2]))),
            )
        except (TypeError, ValueError, OverflowError):
            return fallback

    def _lane_status(self) -> list[dict[str, Any]]:
        incoming = set(self._area.incoming_lanes)
        outgoing = set(self._area.outgoing_lanes)
        traffic = self._reader.read()
        rows: list[dict[str, Any]] = []
        for lane_id in sorted(incoming | outgoing):
            lane = traffic.lane(lane_id)
            rows.append(
                {
                    "lane_id": lane_id,
                    "direction": (
                        "both"
                        if lane_id in incoming and lane_id in outgoing
                        else "incoming"
                        if lane_id in incoming
                        else "outgoing"
                    ),
                    "sensor_range_meters": self._control.sensor_range_meters,
                    "vehicle_count": lane.vehicle_count,
                    "queue": lane.queue,
                    "occupancy": round(lane.occupancy, 4),
                    "mean_speed": round(lane.mean_speed, 3),
                }
            )
        return rows

    def _intersection_status(
        self,
        lane_status: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_lane = {str(item["lane_id"]): item for item in lane_status}
        trafficlight = getattr(self._traci, "trafficlight", None)
        rows: list[dict[str, Any]] = []
        for intersection in self._area.intersections:
            incoming_ids = sorted({link.incoming_lane for link in intersection.links})
            outgoing_ids = sorted({link.outgoing_lane for link in intersection.links})
            incoming_rows = [by_lane[lane_id] for lane_id in incoming_ids if lane_id in by_lane]
            outgoing_rows = [by_lane[lane_id] for lane_id in outgoing_ids if lane_id in by_lane]
            phase = int(
                self._safe_call(
                    getattr(trafficlight, "getPhase", None),
                    0,
                    intersection.tls_id,
                )
            )
            fallback_state = (
                intersection.phases[phase]
                if 0 <= phase < len(intersection.phases)
                else ""
            )
            state_value = self._safe_call(
                getattr(trafficlight, "getRedYellowGreenState", None),
                fallback_state,
                intersection.tls_id,
            )
            state = state_value if isinstance(state_value, str) else fallback_state
            state_lower = state.lower()
            signal = (
                "yellow"
                if "y" in state_lower
                else "green"
                if any(item in "Gg" for item in state)
                else "red"
            )
            rows.append(
                {
                    "tls_id": intersection.tls_id,
                    "x": round(float(intersection.position[0]), 3),
                    "y": round(float(intersection.position[1]), 3),
                    "phase": phase,
                    "phase_elapsed": round(
                        max(
                            float(
                                self._safe_call(
                                    getattr(trafficlight, "getSpentDuration", None),
                                    0.0,
                                    intersection.tls_id,
                                )
                            ),
                            0.0,
                        ),
                        2,
                    ),
                    "signal": signal,
                    "state": state,
                    "incoming_vehicles": sum(
                        int(item["vehicle_count"]) for item in incoming_rows
                    ),
                    "incoming_queue": sum(int(item["queue"]) for item in incoming_rows),
                    "outgoing_occupancy": round(
                        fmean(float(item["occupancy"]) for item in outgoing_rows)
                        if outgoing_rows
                        else 0.0,
                        4,
                    ),
                    "active_now": any(
                        int(item["vehicle_count"]) > 0
                        for item in [*incoming_rows, *outgoing_rows]
                    ),
                    "movements": [
                        {
                            "incoming_lane": link.incoming_lane,
                            "outgoing_lane": link.outgoing_lane,
                            "signal_index": link.signal_index,
                            "state": (
                                state[link.signal_index]
                                if 0 <= link.signal_index < len(state)
                                else "r"
                            ),
                        }
                        for link in intersection.links
                    ],
                }
            )
        return rows

    def _zone_simulation_payload(
        self,
        mode: str,
        simulation_time: float,
        intersections: list[dict[str, Any]],
        lane_status: list[dict[str, Any]],
        vehicles: list[dict[str, Any]],
        system_status: dict[str, Any],
    ) -> dict[str, Any]:
        simulation_value = system_status.get("simulation")
        simulation = (
            simulation_value if isinstance(simulation_value, dict) else {}
        )
        status = str(simulation.get("status", "waiting"))
        controlled_intersections = list(intersections)
        traffic_by_lane = {
            str(row.get("lane_id", "")): row
            for row in lane_status
            if row.get("lane_id")
        }
        visual_lanes = []
        for lane in self._visual_lanes:
            traffic = traffic_by_lane.get(str(lane.get("lane_id", "")), {})
            visual_lanes.append(
                {
                    **lane,
                    "vehicle_count": int(traffic.get("vehicle_count", 0)),
                    "queue": int(traffic.get("queue", 0)),
                    "occupancy": round(float(traffic.get("occupancy", 0.0)), 4),
                    "mean_speed": round(float(traffic.get("mean_speed", 0.0)), 3),
                }
            )
        return {
            "schema_version": 1,
            "source": "sumo",
            "status": status,
            "active": status == "running" and bool(controlled_intersections),
            "mode": mode,
            "simulated_time": round(float(simulation_time), 3),
            "sensor_range_meters": self._control.sensor_range_meters,
            "intersections": controlled_intersections,
            "lanes": visual_lanes,
            "vehicles": vehicles,
        }

    def _vehicle_positions(self, limit: int = 250) -> list[dict[str, Any]]:
        traffic = self._reader.read()
        lane_ids = sorted(
            set(self._area.incoming_lanes) | set(self._area.outgoing_lanes)
        )
        vehicle_ids: set[str] = set()
        for lane_id in lane_ids:
            vehicle_ids.update(traffic.lane(lane_id).vehicle_ids)
        controlled_vehicle_ids = set(vehicle_ids)
        network_vehicle_ids = self._safe_call(
            getattr(self._traci.vehicle, "getIDList", None),
            (),
        )
        if isinstance(network_vehicle_ids, (list, tuple, set)):
            vehicle_ids.update(str(vehicle_id) for vehicle_id in network_vehicle_ids)

        rows: list[dict[str, Any]] = []
        ordered_vehicle_ids = sorted(
            vehicle_ids,
            key=lambda vehicle_id: (
                vehicle_id != self._priority_vehicle,
                vehicle_id,
            ),
        )
        for vehicle_id in ordered_vehicle_ids:
            position = self._safe_call(
                getattr(self._traci.vehicle, "getPosition", None),
                (0.0, 0.0),
                vehicle_id,
            )
            point = (float(position[0]), float(position[1]))
            if (
                vehicle_id not in controlled_vehicle_ids
                and vehicle_id != self._priority_vehicle
                and not self._near_controlled_intersection(point)
            ):
                continue
            color_value = self._safe_call(
                getattr(self._traci.vehicle, "getColor", None),
                (77, 223, 212, 255),
                vehicle_id,
            )
            color = self._color_hex(color_value)
            rows.append(
                {
                    "vehicle_id": vehicle_id,
                    "x": round(float(position[0]), 3),
                    "y": round(float(position[1]), 3),
                    "speed": round(
                        max(
                            float(
                                self._safe_call(
                                    getattr(self._traci.vehicle, "getSpeed", None),
                                    0.0,
                                    vehicle_id,
                                )
                            ),
                            0.0,
                        ),
                        3,
                    ),
                    "angle": round(
                        float(
                            self._safe_call(
                                getattr(self._traci.vehicle, "getAngle", None),
                                0.0,
                                vehicle_id,
                            )
                        ),
                        2,
                    ),
                    "lane_id": str(
                        self._safe_call(
                            getattr(self._traci.vehicle, "getLaneID", None),
                            "",
                            vehicle_id,
                        )
                    ),
                    "color": color,
                    "edge_id": str(
                        self._safe_call(
                            getattr(self._traci.vehicle, "getRoadID", None),
                            "",
                            vehicle_id,
                        )
                    ),
                    "is_priority": vehicle_id == self._priority_vehicle,
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _near_controlled_intersection(self, point: tuple[float, float]) -> bool:
        if not self._area.intersections:
            return False
        maximum_distance = max(float(self._control.sensor_range_meters), 90.0)
        maximum_squared = maximum_distance * maximum_distance
        px, py = point
        return any(
            (px - intersection.position[0]) ** 2
            + (py - intersection.position[1]) ** 2
            <= maximum_squared
            for intersection in self._area.intersections
        )

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

        if self.emergency_trace:
            trace_path = results_dir / f"{mode}_emergency_trace.csv"
            with trace_path.open("w", newline="", encoding="utf-8") as handle:
                fieldnames = list(asdict(self.emergency_trace[0]).keys())
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(asdict(sample) for sample in self.emergency_trace)

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
