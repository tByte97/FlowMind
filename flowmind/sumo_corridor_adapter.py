from __future__ import annotations

from dataclasses import dataclass

from .corridor_manager import NextTlsInfo


@dataclass(frozen=True)
class CorridorObservation:
    vehicle_in_network: bool
    upcoming_tls: tuple[NextTlsInfo, ...]
    passed_tls_ids: tuple[str, ...]


class SumoCorridorObservationAdapter:
    """Translate SUMO vehicle telemetry into neutral corridor observations.

    A passage is emitted only after the vehicle was observed approaching the
    stop line inside ``confirmation_distance`` and that TLS then moved behind
    the vehicle.  A mere change in the upcoming TLS list is not sufficient.
    """

    def __init__(
        self,
        traci_connection: object,
        vehicle_id: str,
        confirmation_distance: float = 35.0,
    ) -> None:
        if confirmation_distance <= 0:
            raise ValueError("confirmation_distance must be positive")
        self._traci = traci_connection
        self._vehicle_id = vehicle_id
        self._confirmation_distance = float(confirmation_distance)
        self._last_distance: dict[str, float] = {}
        self._armed_tls: set[str] = set()

    def observe(self) -> CorridorObservation:
        in_network = self._vehicle_id in set(self._traci.vehicle.getIDList())
        if not in_network:
            return CorridorObservation(False, (), ())

        raw = self._traci.vehicle.getNextTLS(self._vehicle_id)
        upcoming = tuple(
            sorted(
                (
                    (str(tls_id), int(link_index), max(float(distance), 0.0))
                    for tls_id, link_index, distance, *_state in raw
                ),
                key=lambda item: item[2],
            )
        )
        current_ids = {tls_id for tls_id, _link_index, _distance in upcoming}
        passed = tuple(sorted(self._armed_tls - current_ids))
        self._armed_tls.difference_update(passed)

        for tls_id, _link_index, distance in upcoming:
            previous = self._last_distance.get(tls_id)
            if (
                previous is not None
                and distance <= self._confirmation_distance
                and distance <= previous + 1.0
            ):
                self._armed_tls.add(tls_id)
            self._last_distance[tls_id] = distance

        for tls_id in tuple(self._last_distance):
            if tls_id not in current_ids and tls_id not in self._armed_tls:
                self._last_distance.pop(tls_id, None)

        return CorridorObservation(True, upcoming, passed)
