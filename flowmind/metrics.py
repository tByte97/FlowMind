from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from .area_model import AreaModel
from .config import CONTROL_MODES, LEGACY_CONTROL_MODE_ALIASES, ControlConfig
from .live_transport import LiveTelemetryPublisher
from .traffic_state import TrafficStateReader
from .zone_boundary import ZoneBoundary


@dataclass(frozen=True)
class MetricSample:
    time: float
    active_vehicles: int
    departed: int
    arrived: int
    zone_inflow: int | None
    zone_outflow: int | None
    zone_inflow_per_minute: float | None
    zone_outflow_per_minute: float | None
    mean_speed: float | None
    waiting_time: float | None
    queue_length: int | None
    max_queue_length: int | None
    throughput: int
    stops_count: int
    blocked_outgoing_share: float | None


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


@dataclass(frozen=True)
class CivilianImpactSample:
    time: float
    corridor_state: str
    vehicle_count: int
    stopped_vehicles: int
    mean_speed: float
    mean_waiting_time: float


@dataclass(frozen=True)
class TripOutcome:
    vehicle_id: str
    status: str
    departure_time: float
    observation_end_time: float
    travel_time: float | None
    censored_lower_bound: float | None


class MetricsCollector:
    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        control: ControlConfig,
        priority_vehicle: str | None = None,
        zone_boundary: ZoneBoundary | None = None,
    ) -> None:
        self._traci = traci_connection
        self._area = area
        self._control = control
        self._priority_vehicle = priority_vehicle
        self._zone_boundary = zone_boundary
        self._reader = TrafficStateReader(
            traci_connection,
            area,
            control.sensor_range_meters,
        )
        self._previously_stopped: set[str] = set()
        self._departed_at: dict[str, float] = {}
        self._travel_times: list[float] = []
        self._completed_trip_outcomes: list[TripOutcome] = []
        self._throughput = 0
        self._stops = 0
        self._priority_departed: float | None = None
        self._priority_arrived: float | None = None
        self._priority_eta: float | None = None
        self._departed_count = 0
        self._zone_inflow_count = 0
        self._zone_outflow_count = 0
        self._previous_boundary_incoming: set[str] = set()
        self._previous_boundary_outgoing: set[str] = set()
        self._peak_active_vehicles = 0
        self.samples: list[MetricSample] = []
        self.emergency_trace: list[EmergencyTraceSample] = []
        self.civilian_impact_samples: list[CivilianImpactSample] = []
        self._corridor_state = "NORMAL"
        self._last_live_status: dict[str, Any] | None = None
        self._publisher: LiveTelemetryPublisher | None = None
        self._visual_lanes = area.visual_lanes
        self._next_sample_at = float(control.decision_interval)

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
                self._completed_trip_outcomes.append(
                    TripOutcome(
                        vehicle_id=vehicle_id,
                        status="completed",
                        departure_time=departed_at,
                        observation_end_time=simulation_time,
                        travel_time=duration,
                        censored_lower_bound=None,
                    )
                )
                if vehicle_id == self._priority_vehicle:
                    self._priority_eta = duration
                    self._priority_arrived = simulation_time

        self._collect_zone_boundary_crossings()
        self._collect_priority_trace(simulation_time)

        if not self._sample_is_due(simulation_time):
            return

        traffic = self._reader.read(simulation_time)
        lane_ids = set(self._area.incoming_lanes) | set(self._area.outgoing_lanes)
        valid_lanes = [
            traffic.lane(lane_id)
            for lane_id in lane_ids
            if traffic.lane(lane_id).valid
        ]
        queues = [lane.queue for lane in valid_lanes]
        outgoing_occupancies = [
            traffic.lane(lane_id).occupancy
            for lane_id in self._area.outgoing_lanes
            if traffic.lane(lane_id).valid
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
        waiting_by_vehicle = {
            vehicle_id: max(
                float(
                    self._traci.vehicle.getAccumulatedWaitingTime(vehicle_id)
                ),
                0.0,
            )
            for vehicle_id in zone_vehicles
        }
        waiting = list(waiting_by_vehicle.values())
        stopped = {
            vehicle_id
            for vehicle_id, speed in speeds.items()
            if speed < 0.1
        }
        self._stops += len(stopped - self._previously_stopped)
        self._previously_stopped = stopped
        civilian_ids = {
            vehicle_id
            for vehicle_id in zone_vehicles
            if vehicle_id != self._priority_vehicle
        }
        civilian_speeds = [speeds[vehicle_id] for vehicle_id in civilian_ids]
        civilian_waiting = [
            waiting_by_vehicle[vehicle_id] for vehicle_id in civilian_ids
        ]
        self.civilian_impact_samples.append(
            CivilianImpactSample(
                time=simulation_time,
                corridor_state=self._corridor_state,
                vehicle_count=len(civilian_ids),
                stopped_vehicles=sum(
                    speeds[vehicle_id] < 0.1 for vehicle_id in civilian_ids
                ),
                mean_speed=(fmean(civilian_speeds) if civilian_speeds else 0.0),
                mean_waiting_time=(
                    fmean(civilian_waiting) if civilian_waiting else 0.0
                ),
            )
        )
        blocked = sum(
            occupancy >= self._control.blocked_occupancy
            for occupancy in outgoing_occupancies
        )
        previous = self.samples[-1] if self.samples else None
        elapsed = simulation_time - previous.time if previous is not None else 0.0
        boundary_available = self._boundary_available
        inflow_per_minute = (
            (self._zone_inflow_count - int(previous.zone_inflow or 0))
            * 60.0
            / elapsed
            if boundary_available and previous is not None and elapsed > 0
            else None
        )
        outflow_per_minute = (
            (self._zone_outflow_count - int(previous.zone_outflow or 0))
            * 60.0
            / elapsed
            if boundary_available and previous is not None and elapsed > 0
            else None
        )
        effective_throughput = (
            self._zone_outflow_count if boundary_available else self._throughput
        )
        self.samples.append(
            MetricSample(
                time=simulation_time,
                active_vehicles=len(zone_vehicles),
                departed=self._departed_count,
                arrived=self._throughput,
                zone_inflow=(self._zone_inflow_count if boundary_available else None),
                zone_outflow=(self._zone_outflow_count if boundary_available else None),
                zone_inflow_per_minute=(
                    round(inflow_per_minute, 3)
                    if inflow_per_minute is not None
                    else None
                ),
                zone_outflow_per_minute=(
                    round(outflow_per_minute, 3)
                    if outflow_per_minute is not None
                    else None
                ),
                mean_speed=fmean(speeds.values()) if speeds else None,
                waiting_time=fmean(waiting) if waiting else None,
                queue_length=sum(queues) if valid_lanes else None,
                max_queue_length=max(queues) if queues else None,
                throughput=effective_throughput,
                stops_count=self._stops,
                blocked_outgoing_share=(
                    blocked / len(outgoing_occupancies)
                    if outgoing_occupancies
                    else None
                ),
            )
        )

    def _sample_is_due(self, simulation_time: float) -> bool:
        now = float(simulation_time)
        if now + 1e-9 < self._next_sample_at:
            return False
        interval = float(self._control.decision_interval)
        elapsed_intervals = int((now - self._next_sample_at) // interval) + 1
        self._next_sample_at += elapsed_intervals * interval
        return True

    def summary(self, mode: str, simulated_duration: float) -> dict[str, object]:
        waiting_values = [
            sample.waiting_time
            for sample in self.samples
            if sample.waiting_time is not None
        ]
        queue_values = [
            sample.queue_length
            for sample in self.samples
            if sample.queue_length is not None
        ]
        max_queue_values = [
            sample.max_queue_length
            for sample in self.samples
            if sample.max_queue_length is not None
        ]
        blocked_values = [
            sample.blocked_outgoing_share
            for sample in self.samples
            if sample.blocked_outgoing_share is not None
        ]
        censored = self._censored_trip_outcomes(simulated_duration)
        boundary_available = self._boundary_available
        result = {
            "mode": mode,
            "simulated_duration": round(simulated_duration, 2),
            "controlled_tls": len(self._area.tls_ids),
            "tls_ids": ",".join(self._area.tls_ids),
            "average_travel_time": round(fmean(self._travel_times), 3)
            if self._travel_times
            else None,
            "completed_travel_time_mean": round(fmean(self._travel_times), 3)
            if self._travel_times
            else None,
            "completed_trips": len(self._completed_trip_outcomes),
            "unfinished_trips": len(censored),
            "unfinished_travel_time_lower_bound_mean": (
                round(
                    fmean(
                        float(item.censored_lower_bound)
                        for item in censored
                        if item.censored_lower_bound is not None
                    ),
                    3,
                )
                if censored
                else None
            ),
            "average_waiting_time": round(
                fmean(waiting_values), 3
            )
            if waiting_values
            else None,
            "average_queue_length": round(
                fmean(queue_values), 3
            )
            if queue_values
            else None,
            "max_queue_length": max(max_queue_values) if max_queue_values else None,
            "throughput": (
                self._zone_outflow_count if boundary_available else self._throughput
            ),
            "zone_boundary_available": boundary_available,
            "zone_inflow": self._zone_inflow_count if boundary_available else None,
            "zone_outflow": self._zone_outflow_count if boundary_available else None,
            "zone_net_flow": (
                self._zone_inflow_count - self._zone_outflow_count
                if boundary_available
                else None
            ),
            "departed_vehicles": self._departed_count,
            "network_arrived_vehicles": self._throughput,
            "peak_active_vehicles": self._peak_active_vehicles,
            "stops_count": self._stops,
            "blocked_outgoing_share": round(
                fmean(blocked_values), 4
            )
            if blocked_values
            else None,
            "emergency_departure_time": self._priority_departed,
            "emergency_arrival_time": self._priority_arrived,
            "emergency_eta": self._priority_eta,
            "emergency_trace_samples": len(self.emergency_trace),
            "sensor_range_meters": self._control.sensor_range_meters,
            "sensor_lanes": len(
                set(self._area.incoming_lanes) | set(self._area.outgoing_lanes)
            ),
        }
        if self._zone_boundary is not None:
            result.update(self._zone_boundary.as_summary())
        result.update(self._civilian_impact_summary())
        return result

    @property
    def _boundary_available(self) -> bool:
        return self._zone_boundary is not None and self._zone_boundary.valid

    def trip_outcomes(self, simulated_duration: float) -> tuple[TripOutcome, ...]:
        return tuple(
            [
                *self._completed_trip_outcomes,
                *self._censored_trip_outcomes(simulated_duration),
            ]
        )

    def _censored_trip_outcomes(
        self,
        simulated_duration: float,
    ) -> list[TripOutcome]:
        observation_end = float(simulated_duration)
        return [
            TripOutcome(
                vehicle_id=vehicle_id,
                status="unfinished_censored",
                departure_time=departed_at,
                observation_end_time=observation_end,
                travel_time=None,
                censored_lower_bound=max(observation_end - departed_at, 0.0),
            )
            for vehicle_id, departed_at in sorted(self._departed_at.items())
        ]

    def _collect_zone_boundary_crossings(self) -> None:
        if not self._boundary_available or self._zone_boundary is None:
            return
        edge_domain = getattr(self._traci, "edge", None)
        getter = getattr(edge_domain, "getLastStepVehicleIDs", None)
        if not callable(getter):
            return
        incoming = {
            str(vehicle_id)
            for edge_id in self._zone_boundary.incoming_edge_ids
            for vehicle_id in self._safe_call(getter, (), edge_id)
        }
        outgoing = {
            str(vehicle_id)
            for edge_id in self._zone_boundary.outgoing_edge_ids
            for vehicle_id in self._safe_call(getter, (), edge_id)
        }
        self._zone_inflow_count += len(incoming - self._previous_boundary_incoming)
        self._zone_outflow_count += len(outgoing - self._previous_boundary_outgoing)
        self._previous_boundary_incoming = incoming
        self._previous_boundary_outgoing = outgoing

    def set_corridor_state(self, state: object) -> None:
        self._corridor_state = getattr(state, "name", str(state)).upper()

    def _civilian_impact_summary(self) -> dict[str, object]:
        normal = self._civilian_state_mean({"NORMAL"})
        priority = self._civilian_state_mean({"PREPARE", "GREEN_WINDOW"})
        recovery = self._civilian_state_mean({"CLEARANCE", "RECOVERY"})
        reference = normal if normal is not None else recovery
        reference_state = (
            "NORMAL"
            if normal is not None
            else "RECOVERY"
            if recovery is not None
            else None
        )
        return {
            "civilian_priority_samples": sum(
                sample.corridor_state in {"PREPARE", "GREEN_WINDOW"}
                for sample in self.civilian_impact_samples
            ),
            "civilian_normal_mean_waiting_time": normal,
            "civilian_priority_mean_waiting_time": priority,
            "civilian_recovery_mean_waiting_time": recovery,
            "civilian_priority_reference_state": reference_state,
            "civilian_priority_waiting_delta": (
                round(priority - reference, 3)
                if priority is not None and reference is not None
                else None
            ),
        }

    def _civilian_state_mean(self, states: set[str]) -> float | None:
        values = [
            sample.mean_waiting_time
            for sample in self.civilian_impact_samples
            if sample.corridor_state in states and sample.vehicle_count > 0
        ]
        return round(fmean(values), 3) if values else None

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
                "zone_boundary_available": self._boundary_available,
                "zone_inflow_total": (
                    self._zone_inflow_count if self._boundary_available else None
                ),
                "zone_outflow_total": (
                    self._zone_outflow_count if self._boundary_available else None
                ),
                "inflow_per_minute": (
                    latest_sample.get("zone_inflow_per_minute")
                    if latest_sample is not None
                    else None
                ),
                "outflow_per_minute": (
                    latest_sample.get("zone_outflow_per_minute")
                    if latest_sample is not None
                    else None
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
                    "program_id": intersection.program_id,
                    "program_type": intersection.program_type,
                    "phase_duration": intersection.default_phase_duration(phase),
                    "phase_min_duration": intersection.phase_min_duration(phase),
                    "phase_max_duration": intersection.phase_max_duration(phase),
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

        if self.civilian_impact_samples:
            impact_path = results_dir / f"{mode}_civilian_priority_impact.csv"
            with impact_path.open("w", newline="", encoding="utf-8") as handle:
                fieldnames = list(asdict(self.civilian_impact_samples[0]).keys())
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(
                    asdict(sample) for sample in self.civilian_impact_samples
                )

        duration_value = summary.get("simulated_duration")
        outcomes = (
            self.trip_outcomes(float(duration_value))
            if duration_value is not None
            else ()
        )
        if outcomes:
            outcomes_path = results_dir / f"{mode}_trip_outcomes.csv"
            with outcomes_path.open("w", newline="", encoding="utf-8") as handle:
                fieldnames = list(asdict(outcomes[0]).keys())
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(asdict(outcome) for outcome in outcomes)

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
            result_mode_order = (
                CONTROL_MODES[0],
                *LEGACY_CONTROL_MODE_ALIASES,
                *CONTROL_MODES[1:],
            )
            for row_mode in result_mode_order:
                if row_mode in rows:
                    writer.writerow(rows[row_mode])
