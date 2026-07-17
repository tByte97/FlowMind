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
    route_edges: tuple[str, ...]
    route_edge_count: int
    route_length: float
    expected_travel_time: float
    predicted_eta: float

    def as_summary(self) -> dict[str, object]:
        return {
            "emergency_vehicle_id": self.vehicle_id,
            "emergency_start": self.start_name,
            "emergency_start_edge": self.start_edge,
            "emergency_destination": self.destination_name,
            "emergency_destination_edge": self.destination_edge,
            "emergency_scheduled_departure": self.scheduled_departure,
            "emergency_route_edges": " ".join(self.route_edges),
            "emergency_route_edge_count": self.route_edge_count,
            "emergency_route_length": round(self.route_length, 2),
            "emergency_expected_travel_time": round(
                self.expected_travel_time, 2
            ),
            "emergency_predicted_eta": round(self.predicted_eta, 2),
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
        self._route_overlay_ids: list[str] = []
        self._corridor_visual_state = ""

    def install(
        self,
        precalculated_edges: tuple[str, ...] | None = None,
        route_length: float | None = None,
        expected_travel_time: float | None = None,
        predicted_eta: float | None = None,
    ) -> EmergencyRouteDetails:
        config = self.config
        type_ids = set(self._traci.vehicletype.getIDList())
        if config.base_vehicle_type_id not in type_ids:
            raise ValueError(
                f"Unknown base vehicle type: {config.base_vehicle_type_id}"
            )
        edge_ids = set(self._traci.edge.getIDList())
        unknown_edges = [
            edge_id
            for edge_id in (
                config.start.edge_id,
                config.destination.edge_id,
            )
            if edge_id not in edge_ids
        ]
        if unknown_edges:
            raise ValueError(
                "Emergency edge IDs are absent from the current SUMO map: "
                + ", ".join(unknown_edges)
                + ". Update the selected emergency config for this map."
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

        if precalculated_edges is not None:
            edges = precalculated_edges
            stage = None
        else:
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
        self._set_vehicle_appearance()
        self._draw_route_overlay(edges)
        self.details = EmergencyRouteDetails(
            vehicle_id=config.vehicle_id,
            start_name=config.start.name,
            start_edge=config.start.edge_id,
            destination_name=config.destination.name,
            destination_edge=config.destination.edge_id,
            scheduled_departure=config.depart_time,
            route_edges=edges,
            route_edge_count=len(edges),
            route_length=(
                float(stage.length)
                if stage
                else float(route_length or 0.0)
            ),
            expected_travel_time=(
                float(stage.travelTime)
                if stage
                else float(expected_travel_time or 0.0)
            ),
            predicted_eta=(
                float(stage.travelTime)
                if stage
                else float(predicted_eta or expected_travel_time or 0.0)
            ),
        )
        return self.details

    def _set_vehicle_appearance(self) -> None:
        """Make the ambulance visually distinct in SUMO GUI when available."""

        config = self.config
        try:
            self._traci.vehicle.setColor(config.vehicle_id, config.color)
        except Exception:
            pass

    def replace_scheduled_route(
        self,
        edges: tuple[str, ...],
        *,
        route_length: float,
        expected_travel_time: float,
        predicted_eta: float,
    ) -> bool:
        """Apply a fresh route assessment before the vehicle departs."""

        if self.details is None:
            raise RuntimeError("Emergency vehicle must be installed before rerouting")
        if len(edges) < 2:
            raise ValueError("Emergency reroute must contain at least two edges")
        if edges == self.details.route_edges:
            return False
        self._traci.vehicle.setRoute(self.config.vehicle_id, edges)
        self.details = replace(
            self.details,
            route_edges=edges,
            route_edge_count=len(edges),
            route_length=float(route_length),
            expected_travel_time=float(expected_travel_time),
            predicted_eta=float(predicted_eta),
        )
        self._draw_route_overlay(edges)
        return True
        try:
            self._traci.vehicle.highlight(
                config.vehicle_id,
                color=config.color,
                size=35,
                alphaMax=255,
                duration=-1,
            )
        except Exception:
            pass

    def _draw_route_overlay(self, edges: tuple[str, ...]) -> None:
        """Draw the selected emergency route, ready for live corridor coloring."""

        if not hasattr(self._traci, "lane") or not hasattr(self._traci, "polygon"):
            return
        polygon = getattr(self._traci, "polygon", None)
        if polygon is not None:
            for polygon_id in self._route_overlay_ids:
                try:
                    polygon.remove(polygon_id)
                except Exception:
                    pass
        self._route_overlay_ids.clear()
        try:
            lane_by_edge = {
                self._traci.lane.getEdgeID(lane_id): lane_id
                for lane_id in self._traci.lane.getIDList()
            }
        except Exception:
            return

        first_shape: tuple[tuple[float, float], ...] | None = None
        last_shape: tuple[tuple[float, float], ...] | None = None
        for index, edge_id in enumerate(edges):
            lane_id = lane_by_edge.get(edge_id)
            if lane_id is None:
                continue
            try:
                shape = tuple(self._traci.lane.getShape(lane_id))
            except Exception:
                continue
            if len(shape) < 2:
                continue
            if first_shape is None:
                first_shape = shape
            last_shape = shape
            polygon_id = f"{self.config.route_id}_overlay_{index:03d}"
            try:
                if polygon_id in self._traci.polygon.getIDList():
                    self._traci.polygon.remove(polygon_id)
            except Exception:
                pass
            try:
                self._traci.polygon.add(
                    polygon_id,
                    shape,
                    (45, 125, 255, 190),
                    fill=False,
                    polygonType="emergency_route",
                    layer=100,
                    lineWidth=4,
                )
                self._route_overlay_ids.append(polygon_id)
            except Exception:
                pass

        self._draw_route_marker("start", first_shape[0] if first_shape else None)
        self._draw_route_marker(
            "destination", last_shape[-1] if last_shape else None
        )

    def update_corridor_visualization(
        self,
        corridor_state: object,
        active_tls: str | None = None,
    ) -> None:
        """Recolor the route overlay as the green-corridor state changes."""

        state_name = getattr(corridor_state, "name", str(corridor_state)).upper()
        visual_key = f"{state_name}:{active_tls or ''}"
        if visual_key == self._corridor_visual_state:
            return
        self._corridor_visual_state = visual_key

        if state_name == "GREEN_WINDOW":
            color = (20, 255, 90, 255)
            line_width = 9.0
        elif state_name == "PREPARE":
            color = (255, 190, 35, 235)
            line_width = 7.0
        elif state_name in {"CLEARANCE", "RECOVERY"}:
            color = (50, 220, 255, 220)
            line_width = 6.0
        else:
            color = (45, 125, 255, 190)
            line_width = 4.0

        polygon = getattr(self._traci, "polygon", None)
        if polygon is None:
            return
        for polygon_id in self._route_overlay_ids:
            try:
                polygon.setColor(polygon_id, color)
            except Exception:
                pass
            try:
                polygon.setLineWidth(polygon_id, line_width)
            except Exception:
                pass

    def _draw_route_marker(
        self, suffix: str, position: tuple[float, float] | None
    ) -> None:
        if position is None or not hasattr(self._traci, "poi"):
            return
        marker_id = f"{self.config.route_id}_{suffix}"
        try:
            if marker_id in self._traci.poi.getIDList():
                self._traci.poi.remove(marker_id)
        except Exception:
            pass
        try:
            self._traci.poi.add(
                marker_id,
                position[0],
                position[1],
                self.config.color,
                poiType=f"emergency_{suffix}",
                layer=120,
                width=18,
                height=18,
            )
        except Exception:
            pass
