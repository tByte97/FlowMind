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
    vehicle_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrafficState:
    lanes: dict[str, LaneState]

    def lane(self, lane_id: str) -> LaneState:
        return self.lanes.get(lane_id, LaneState(0, 0, 0.0, 0.0, 0.0))


class TrafficStateReader:
    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        sensor_range_meters: float = 120.0,
    ) -> None:
        self._traci = traci_connection
        self._incoming = set(area.incoming_lanes)
        self._outgoing = set(area.outgoing_lanes)
        self._lane_ids = tuple(sorted(self._incoming | self._outgoing))
        self._sensor_range_meters = max(float(sensor_range_meters), 1.0)

    def read(self) -> TrafficState:
        lanes: dict[str, LaneState] = {}
        for lane_id in self._lane_ids:
            lanes[lane_id] = self._read_lane(lane_id)
        return TrafficState(lanes)

    def _read_lane(self, lane_id: str) -> LaneState:
        length = max(
            float(
                self._safe_call(
                    getattr(self._traci.lane, "getLength", None),
                    1.0,
                    lane_id,
                )
            ),
            1.0,
        )
        vehicle_ids = tuple(
            str(vehicle_id)
            for vehicle_id in self._safe_call(
                getattr(self._traci.lane, "getLastStepVehicleIDs", None),
                (),
                lane_id,
            )
        )
        vehicle_api = getattr(self._traci, "vehicle", None)
        if vehicle_ids and vehicle_api is not None:
            sensor_vehicle_ids = tuple(
                vehicle_id
                for vehicle_id in vehicle_ids
                if self._inside_sensor_window(lane_id, vehicle_id, length, vehicle_api)
            )
            speeds = tuple(
                max(
                    float(
                        self._safe_call(
                            getattr(vehicle_api, "getSpeed", None),
                            0.0,
                            vehicle_id,
                        )
                    ),
                    0.0,
                )
                for vehicle_id in sensor_vehicle_ids
            )
            vehicle_count = len(sensor_vehicle_ids)
            queue = sum(1 for speed in speeds if speed < 0.1)
            sensor_length = min(self._sensor_range_meters, length)
            capacity = max(sensor_length / 7.5, 1.0)
            return LaneState(
                queue=queue,
                vehicle_count=vehicle_count,
                occupancy=min(max(vehicle_count / capacity, 0.0), 1.0),
                mean_speed=sum(speeds) / len(speeds) if speeds else 0.0,
                free_slots=max(capacity - vehicle_count, 0.0),
                vehicle_ids=sensor_vehicle_ids,
            )
        return self._fallback_lane_state(lane_id, length)

    def _inside_sensor_window(
        self,
        lane_id: str,
        vehicle_id: str,
        lane_length: float,
        vehicle_api: object,
    ) -> bool:
        position = max(
            float(
                self._safe_call(
                    getattr(vehicle_api, "getLanePosition", None),
                    -1.0,
                    vehicle_id,
                )
            ),
            0.0,
        )
        if (
            lane_id in self._incoming
            and lane_length - position <= self._sensor_range_meters
        ):
            return True
        if lane_id in self._outgoing and position <= self._sensor_range_meters:
            return True
        return False

    def _fallback_lane_state(self, lane_id: str, length: float) -> LaneState:
        vehicle_count = int(
            self._safe_call(
                getattr(self._traci.lane, "getLastStepVehicleNumber", None),
                0,
                lane_id,
            )
        )
        capacity = max(min(length, self._sensor_range_meters) / 7.5, 1.0)
        return LaneState(
            queue=int(
                self._safe_call(
                    getattr(self._traci.lane, "getLastStepHaltingNumber", None),
                    0,
                    lane_id,
                )
            ),
            vehicle_count=vehicle_count,
            occupancy=min(
                max(
                    float(
                        self._safe_call(
                            getattr(self._traci.lane, "getLastStepOccupancy", None),
                            0.0,
                            lane_id,
                        )
                    )
                    / 100.0,
                    0.0,
                ),
                1.0,
            ),
            mean_speed=max(
                float(
                    self._safe_call(
                        getattr(self._traci.lane, "getLastStepMeanSpeed", None),
                        0.0,
                        lane_id,
                    )
                ),
                0.0,
            ),
            free_slots=max(capacity - vehicle_count, 0.0),
            vehicle_ids=tuple(
                str(vehicle_id)
                for vehicle_id in self._safe_call(
                    getattr(self._traci.lane, "getLastStepVehicleIDs", None),
                    (),
                    lane_id,
                )
            ),
        )

    @staticmethod
    def _safe_call(callable_object: object, default: object, *args: object) -> object:
        if callable_object is None:
            return default
        try:
            return callable_object(*args)  # type: ignore[operator]
        except Exception:
            return default
