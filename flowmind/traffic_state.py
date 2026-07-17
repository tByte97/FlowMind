from __future__ import annotations

from dataclasses import dataclass, replace

from .area_model import AreaModel


@dataclass(frozen=True)
class LaneState:
    queue: int
    vehicle_count: int
    occupancy: float
    mean_speed: float
    free_slots: float
    vehicle_ids: tuple[str, ...] = ()
    valid: bool = True
    sample_time: float = 0.0
    error: str | None = None
    stale: bool = False


@dataclass(frozen=True)
class TrafficState:
    lanes: dict[str, LaneState]
    sample_time: float = 0.0

    def lane(self, lane_id: str) -> LaneState:
        return self.lanes.get(
            lane_id,
            LaneState(
                0,
                0,
                0.0,
                0.0,
                0.0,
                valid=False,
                sample_time=self.sample_time,
                error="lane missing from traffic snapshot",
            ),
        )

    @property
    def stale_lane_ids(self) -> tuple[str, ...]:
        return tuple(sorted(lane_id for lane_id, lane in self.lanes.items() if lane.stale))

    @property
    def invalid_lane_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(lane_id for lane_id, lane in self.lanes.items() if not lane.valid)
        )

    @property
    def sensor_error_lane_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(lane_id for lane_id, lane in self.lanes.items() if lane.error)
        )

    @property
    def usable(self) -> bool:
        return not self.invalid_lane_ids


class TrafficStateReader:
    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        sensor_range_meters: float = 120.0,
        monitored_lane_ids: tuple[str, ...] = (),
        last_known_good_ttl: float = 6.0,
    ) -> None:
        self._traci = traci_connection
        self._incoming = set(area.incoming_lanes)
        self._outgoing = set(area.outgoing_lanes)
        self._full_lane = set(monitored_lane_ids) - self._incoming - self._outgoing
        self._lane_ids = tuple(
            sorted(self._incoming | self._outgoing | self._full_lane)
        )
        self._sensor_range_meters = max(float(sensor_range_meters), 1.0)
        self._last_known_good_ttl = max(float(last_known_good_ttl), 0.0)
        self._last_good: dict[str, LaneState] = {}

    def read(self, simulation_time: float = 0.0) -> TrafficState:
        lanes: dict[str, LaneState] = {}
        for lane_id in self._lane_ids:
            lanes[lane_id] = self._read_lane(lane_id, float(simulation_time))
        return TrafficState(lanes, sample_time=float(simulation_time))

    def _read_lane(self, lane_id: str, simulation_time: float) -> LaneState:
        try:
            length = max(
                float(
                    self._required_call(
                        getattr(self._traci.lane, "getLength", None),
                        lane_id,
                    )
                ),
                1.0,
            )
            vehicle_ids = tuple(
                str(vehicle_id)
                for vehicle_id in self._required_call(
                    getattr(self._traci.lane, "getLastStepVehicleIDs", None),
                    lane_id,
                )
            )
            vehicle_api = getattr(self._traci, "vehicle", None)
            if vehicle_ids and vehicle_api is not None:
                state = self._vehicle_lane_state(
                    lane_id,
                    length,
                    vehicle_ids,
                    vehicle_api,
                    simulation_time,
                )
            else:
                state = self._aggregate_lane_state(
                    lane_id,
                    length,
                    vehicle_ids,
                    simulation_time,
                )
        except Exception as error:
            return self._invalid_or_last_good(lane_id, simulation_time, error)
        self._last_good[lane_id] = state
        return state

    def _vehicle_lane_state(
        self,
        lane_id: str,
        length: float,
        vehicle_ids: tuple[str, ...],
        vehicle_api: object,
        simulation_time: float,
    ) -> LaneState:
        sensor_vehicle_ids = tuple(
            vehicle_id
            for vehicle_id in vehicle_ids
            if self._inside_sensor_window(lane_id, vehicle_id, length, vehicle_api)
        )
        speeds = tuple(
            max(
                float(
                    self._required_call(
                        getattr(vehicle_api, "getSpeed", None),
                        vehicle_id,
                    )
                ),
                0.0,
            )
            for vehicle_id in sensor_vehicle_ids
        )
        vehicle_count = len(sensor_vehicle_ids)
        sensor_length = (
            length if lane_id in self._full_lane else min(self._sensor_range_meters, length)
        )
        capacity = max(sensor_length / 7.5, 1.0)
        return LaneState(
            queue=sum(1 for speed in speeds if speed < 0.1),
            vehicle_count=vehicle_count,
            occupancy=min(max(vehicle_count / capacity, 0.0), 1.0),
            mean_speed=sum(speeds) / len(speeds) if speeds else 0.0,
            free_slots=max(capacity - vehicle_count, 0.0),
            vehicle_ids=sensor_vehicle_ids,
            sample_time=simulation_time,
        )

    def _inside_sensor_window(
        self,
        lane_id: str,
        vehicle_id: str,
        lane_length: float,
        vehicle_api: object,
    ) -> bool:
        position = max(
            float(
                self._required_call(
                    getattr(vehicle_api, "getLanePosition", None),
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
        if lane_id in self._full_lane:
            return True
        return False

    def _aggregate_lane_state(
        self,
        lane_id: str,
        length: float,
        vehicle_ids: tuple[str, ...],
        simulation_time: float,
    ) -> LaneState:
        vehicle_count = int(
            self._required_call(
                getattr(self._traci.lane, "getLastStepVehicleNumber", None),
                lane_id,
            )
        )
        monitored_length = (
            length
            if lane_id in self._full_lane
            else min(length, self._sensor_range_meters)
        )
        capacity = max(monitored_length / 7.5, 1.0)
        return LaneState(
            queue=int(
                self._required_call(
                    getattr(self._traci.lane, "getLastStepHaltingNumber", None),
                    lane_id,
                )
            ),
            vehicle_count=vehicle_count,
            occupancy=min(
                max(
                    float(
                        self._required_call(
                            getattr(self._traci.lane, "getLastStepOccupancy", None),
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
                self._required_call(
                    getattr(self._traci.lane, "getLastStepMeanSpeed", None),
                    lane_id,
                    )
                ),
                0.0,
            ),
            free_slots=max(capacity - vehicle_count, 0.0),
            vehicle_ids=vehicle_ids,
            sample_time=simulation_time,
        )

    @staticmethod
    def _required_call(callable_object: object, *args: object) -> object:
        if callable_object is None:
            raise RuntimeError("sensor API method is unavailable")
        try:
            return callable_object(*args)  # type: ignore[operator]
        except Exception as error:
            raise RuntimeError(str(error) or type(error).__name__) from error

    def _invalid_or_last_good(
        self,
        lane_id: str,
        simulation_time: float,
        error: Exception,
    ) -> LaneState:
        message = str(error) or type(error).__name__
        previous = self._last_good.get(lane_id)
        if (
            previous is not None
            and simulation_time - previous.sample_time
            <= self._last_known_good_ttl
        ):
            return replace(
                previous,
                valid=True,
                stale=True,
                error=message,
            )
        return LaneState(
            queue=0,
            vehicle_count=0,
            occupancy=0.0,
            mean_speed=0.0,
            free_slots=0.0,
            valid=False,
            sample_time=simulation_time,
            error=message,
        )
