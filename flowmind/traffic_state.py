from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel


@dataclass(frozen=True)
class LaneState:
    queue: int
    vehicle_count: int
    occupancy: float
    mean_speed: float
    free_slots: float


@dataclass(frozen=True)
class TrafficState:
    lanes: dict[str, LaneState]

    def lane(self, lane_id: str) -> LaneState:
        return self.lanes.get(lane_id, LaneState(0, 0, 0.0, 0.0, 0.0))


class TrafficStateReader:
    def __init__(self, traci_connection: object, area: AreaModel) -> None:
        self._traci = traci_connection
        self._lane_ids = tuple(
            sorted(set(area.incoming_lanes) | set(area.outgoing_lanes))
        )

    def read(self) -> TrafficState:
        lanes: dict[str, LaneState] = {}
        for lane_id in self._lane_ids:
            vehicle_count = int(self._traci.lane.getLastStepVehicleNumber(lane_id))
            length = float(self._traci.lane.getLength(lane_id))
            capacity = max(length / 7.5, 1.0)
            lanes[lane_id] = LaneState(
                queue=int(self._traci.lane.getLastStepHaltingNumber(lane_id)),
                vehicle_count=vehicle_count,
                occupancy=min(
                    max(
                        float(self._traci.lane.getLastStepOccupancy(lane_id)) / 100.0,
                        0.0,
                    ),
                    1.0,
                ),
                mean_speed=max(
                    float(self._traci.lane.getLastStepMeanSpeed(lane_id)), 0.0
                ),
                free_slots=max(capacity - vehicle_count, 0.0),
            )
        return TrafficState(lanes)
