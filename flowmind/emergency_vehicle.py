from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class EmergencyPoint:
    name: str
    edge_id: str
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class EmergencyVehicleConfig:
    vehicle_id: str
    route_id: str
    vehicle_type_id: str
    base_vehicle_type_id: str
    depart_time: float
    start: EmergencyPoint
    destination: EmergencyPoint
    color: tuple[int, int, int, int] = (255, 35, 35, 255)
    max_speed: float = 25.0
    speed_factor: float = 1.15
    accel: float = 3.0
    decel: float = 6.0
    emergency_decel: float = 9.0

    def with_overrides(
        self,
        *,
        depart_time: float | None = None,
        start_edge: str | None = None,
        destination_edge: str | None = None,
    ) -> EmergencyVehicleConfig:
        return replace(
            self,
            depart_time=self.depart_time if depart_time is None else depart_time,
            start=(
                self.start
                if start_edge is None
                else replace(self.start, edge_id=start_edge)
            ),
            destination=(
                self.destination
                if destination_edge is None
                else replace(self.destination, edge_id=destination_edge)
            ),
        )


@dataclass(frozen=True)
class EmergencyRouteDetails:
    vehicle_id: str
    start_name: str
    start_edge: str
    destination_name: str
    destination_edge: str
    scheduled_departure: float
    route_edge_count: int
    route_length: float
    expected_travel_time: float

    def as_summary(self) -> dict[str, object]:
        return {
            "emergency_vehicle_id": self.vehicle_id,
            "emergency_start": self.start_name,
            "emergency_start_edge": self.start_edge,
            "emergency_destination": self.destination_name,
            "emergency_destination_edge": self.destination_edge,
            "emergency_scheduled_departure": self.scheduled_departure,
            "emergency_route_edge_count": self.route_edge_count,
            "emergency_route_length": round(self.route_length, 2),
            "emergency_expected_travel_time": round(
                self.expected_travel_time, 2
            ),
        }


def load_emergency_config(path: str | Path) -> EmergencyVehicleConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    start = payload["start"]
    destination = payload["destination"]
    color = tuple(int(value) for value in payload.get("color", (255, 35, 35, 255)))
    if len(color) != 4 or any(value < 0 or value > 255 for value in color):
        raise ValueError("Emergency color must contain four values between 0 and 255")
    config = EmergencyVehicleConfig(
        vehicle_id=str(payload["vehicle_id"]),
        route_id=str(payload["route_id"]),
        vehicle_type_id=str(payload["vehicle_type_id"]),
        base_vehicle_type_id=str(payload.get("base_vehicle_type_id", "DEFAULT_VEHTYPE")),
        depart_time=float(payload["depart_time"]),
        start=EmergencyPoint(
            name=str(start["name"]),
            edge_id=str(start["edge_id"]),
            latitude=float(start["latitude"]) if "latitude" in start else None,
            longitude=float(start["longitude"]) if "longitude" in start else None,
        ),
        destination=EmergencyPoint(
            name=str(destination["name"]),
            edge_id=str(destination["edge_id"]),
            latitude=(
                float(destination["latitude"])
                if "latitude" in destination
                else None
            ),
            longitude=(
                float(destination["longitude"])
                if "longitude" in destination
                else None
            ),
        ),
        color=color,
        max_speed=float(payload.get("max_speed", 25.0)),
        speed_factor=float(payload.get("speed_factor", 1.15)),
        accel=float(payload.get("accel", 3.0)),
        decel=float(payload.get("decel", 6.0)),
        emergency_decel=float(payload.get("emergency_decel", 9.0)),
    )
    if config.depart_time < 0:
        raise ValueError("Emergency departure time cannot be negative")
    if config.start.edge_id == config.destination.edge_id:
        raise ValueError("Emergency start and destination edges must differ")
    return config


class EmergencyVehicleManager:
    """Create and schedule an ambulance in a running TraCI simulation."""

    def __init__(
        self, traci_connection: object, config: EmergencyVehicleConfig
    ) -> None:
        self._traci = traci_connection
        self.config = config
        self.details: EmergencyRouteDetails | None = None

    def install(self) -> EmergencyRouteDetails:
        config = self.config
        type_ids = set(self._traci.vehicletype.getIDList())
        if config.base_vehicle_type_id not in type_ids:
            raise ValueError(
                f"Unknown base vehicle type: {config.base_vehicle_type_id}"
            )
        if config.vehicle_type_id not in type_ids:
            self._traci.vehicletype.copy(
                config.base_vehicle_type_id, config.vehicle_type_id
            )
        self._traci.vehicletype.setColor(config.vehicle_type_id, config.color)
        self._traci.vehicletype.setVehicleClass(
            config.vehicle_type_id, "emergency"
        )
        self._traci.vehicletype.setShapeClass(
            config.vehicle_type_id, "emergency"
        )
        self._traci.vehicletype.setMaxSpeed(
            config.vehicle_type_id, config.max_speed
        )
        self._traci.vehicletype.setSpeedFactor(
            config.vehicle_type_id, config.speed_factor
        )
        self._traci.vehicletype.setSpeedDeviation(config.vehicle_type_id, 0.0)
        self._traci.vehicletype.setAccel(config.vehicle_type_id, config.accel)
        self._traci.vehicletype.setDecel(config.vehicle_type_id, config.decel)
        self._traci.vehicletype.setEmergencyDecel(
            config.vehicle_type_id, config.emergency_decel
        )

        stage = self._traci.simulation.findRoute(
            config.start.edge_id,
            config.destination.edge_id,
            config.vehicle_type_id,
            config.depart_time,
        )
        edges = tuple(stage.edges)
        if len(edges) < 2:
            raise RuntimeError(
                "SUMO could not build an emergency route from "
                f"{config.start.edge_id} to {config.destination.edge_id}"
            )
        if config.route_id in self._traci.route.getIDList():
            raise ValueError(f"Emergency route already exists: {config.route_id}")
        if config.vehicle_id in self._traci.vehicle.getLoadedIDList():
            raise ValueError(
                f"Emergency vehicle already exists: {config.vehicle_id}"
            )

        self._traci.route.add(config.route_id, edges)
        self._traci.vehicle.add(
            config.vehicle_id,
            config.route_id,
            typeID=config.vehicle_type_id,
            depart=str(config.depart_time),
            departLane="best",
            departSpeed="max",
            arrivalLane="current",
            arrivalPos="max",
        )
        self.details = EmergencyRouteDetails(
            vehicle_id=config.vehicle_id,
            start_name=config.start.name,
            start_edge=config.start.edge_id,
            destination_name=config.destination.name,
            destination_edge=config.destination.edge_id,
            scheduled_departure=config.depart_time,
            route_edge_count=len(edges),
            route_length=float(stage.length),
            expected_travel_time=float(stage.travelTime),
        )
        return self.details
