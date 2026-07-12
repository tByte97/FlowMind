from __future__ import annotations

from enum import Enum, auto

from .decision_feed import DecisionEvent


class CorridorState(Enum):
    NORMAL = auto()
    PREPARE = auto()
    GREEN_WINDOW = auto()
    CLEARANCE = auto()
    RECOVERY = auto()


NextTlsInfo = tuple[str, int, float]


class CorridorManager:
    """Source-neutral emergency-corridor state machine.

    Upcoming and passed TLS observations are supplied by an adapter.  The
    manager never infers a successful passage merely because a TLS disappears
    from the upcoming list.
    """

    def __init__(
        self,
        vehicle_id: str,
        prepare_distance: float = 800.0,
        green_window_distance: float = 300.0,
        timeout_seconds: float = 10.0,
        clearance_seconds: float = 5.0,
        recovery_seconds: float = 30.0,
        prepare_tls_count: int = 3,
    ) -> None:
        if prepare_distance <= 0 or green_window_distance <= 0:
            raise ValueError("Corridor distances must be positive")
        if green_window_distance > prepare_distance:
            raise ValueError("green_window_distance cannot exceed prepare_distance")
        if min(timeout_seconds, clearance_seconds, recovery_seconds) <= 0:
            raise ValueError("Corridor timeouts must be positive")
        if prepare_tls_count <= 0:
            raise ValueError("prepare_tls_count must be positive")

        self.vehicle_id = vehicle_id
        self.state = CorridorState.NORMAL
        self.prepare_distance = float(prepare_distance)
        self.green_window_distance = float(green_window_distance)
        self.timeout_seconds = float(timeout_seconds)
        self.clearance_seconds = float(clearance_seconds)
        self.recovery_seconds = float(recovery_seconds)
        self.prepare_tls_count = int(prepare_tls_count)

        self.active_tls: str | None = None
        self.active_link_index: int | None = None
        self.prepared_tls: tuple[NextTlsInfo, ...] = ()
        self.completed_tls: list[str] = []
        self.unconfirmed_tls: list[str] = []
        self.affected_tls: list[str] = []
        self.recovery_tls: list[str] = []
        self.restored_tls: list[str] = []
        self.downstream_block_count = 0
        self.downstream_block_reasons: dict[str, int] = {}
        self.decision_events: list[DecisionEvent] = []

        self._last_vehicle_seen_time = -1.0
        self._last_upcoming_seen_time = -1.0
        self._clearance_start_time = -1.0
        self._recovery_start_time = -1.0

    def step(
        self,
        simulation_time: float,
        vehicle_in_network: bool,
        next_tls_info: NextTlsInfo | list[NextTlsInfo] | None = None,
        passed_tls_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        previous_state = self.state
        previous_tls = self.active_tls
        passed = tuple(dict.fromkeys(str(tls_id) for tls_id in passed_tls_ids))
        self._confirm_passages(simulation_time, passed)
        self._step(
            float(simulation_time),
            vehicle_in_network,
            self._normalize_upcoming(next_tls_info),
            passed,
        )
        self._record_transition(simulation_time, previous_state, previous_tls)

    def _step(
        self,
        simulation_time: float,
        vehicle_in_network: bool,
        upcoming: tuple[NextTlsInfo, ...],
        passed_tls_ids: tuple[str, ...],
    ) -> None:
        if not vehicle_in_network:
            self._handle_missing_vehicle(simulation_time)
            return

        self._last_vehicle_seen_time = simulation_time
        if self.state == CorridorState.RECOVERY:
            self._advance_recovery(simulation_time)
            return
        if self.state == CorridorState.CLEARANCE:
            if simulation_time - self._clearance_start_time >= self.clearance_seconds:
                self._enter_recovery(simulation_time)
            return

        if upcoming:
            self._last_upcoming_seen_time = simulation_time
            nearest = upcoming[0]
            previous_active = self.active_tls
            if (
                previous_active
                and previous_active != nearest[0]
                and previous_active not in passed_tls_ids
                and previous_active not in self.completed_tls
                and previous_active not in self.unconfirmed_tls
            ):
                self.unconfirmed_tls.append(previous_active)

            if nearest[2] > self.prepare_distance and self.state == CorridorState.NORMAL:
                self.active_tls = None
                self.active_link_index = None
                self.prepared_tls = ()
                return

            self.prepared_tls = upcoming[: self.prepare_tls_count]
            self.active_tls, self.active_link_index, distance = nearest
            for tls_id, _link_index, _distance in self.prepared_tls:
                if tls_id not in self.affected_tls:
                    self.affected_tls.append(tls_id)
            self.state = (
                CorridorState.GREEN_WINDOW
                if distance <= self.green_window_distance
                else CorridorState.PREPARE
            )
            return

        self.prepared_tls = ()
        active_was_confirmed = (
            self.active_tls is None
            or self.active_tls in passed_tls_ids
            or self.active_tls in self.completed_tls
        )
        if self.state in {CorridorState.PREPARE, CorridorState.GREEN_WINDOW}:
            if active_was_confirmed:
                self.active_tls = None
                self.active_link_index = None
                self._enter_clearance(simulation_time)
            elif (
                self._last_upcoming_seen_time >= 0
                and simulation_time - self._last_upcoming_seen_time
                > self.timeout_seconds
            ):
                if self.active_tls and self.active_tls not in self.unconfirmed_tls:
                    self.unconfirmed_tls.append(self.active_tls)
                self.active_tls = None
                self.active_link_index = None
                self._enter_recovery(simulation_time)

    def get_priority_overrides(self) -> dict[str, int]:
        """Hard priority exists only inside the active green window."""

        if (
            self.state == CorridorState.GREEN_WINDOW
            and self.active_tls is not None
            and self.active_link_index is not None
        ):
            return {self.active_tls: self.active_link_index}
        return {}

    def get_preparation_overrides(self) -> dict[str, int]:
        """Return soft look-ahead targets without granting hard priority."""

        if self.state not in {CorridorState.PREPARE, CorridorState.GREEN_WINDOW}:
            return {}
        return {
            tls_id: link_index
            for tls_id, link_index, _distance in self.prepared_tls
            if not (
                self.state == CorridorState.GREEN_WINDOW
                and tls_id == self.active_tls
            )
        }

    def confirm_recovery(self, tls_id: str, simulation_time: float) -> None:
        if tls_id in self.recovery_tls and tls_id not in self.restored_tls:
            self.restored_tls.append(tls_id)
            self.decision_events.append(
                DecisionEvent(
                    time=round(simulation_time, 3),
                    category="corridor",
                    title="Відновлено фазовий offset",
                    detail=f"Перехрестя {tls_id} синхронізовано з базовим планом.",
                    level="success",
                    tls_id=tls_id,
                )
            )
        if self.state == CorridorState.RECOVERY and set(self.recovery_tls).issubset(
            self.restored_tls
        ):
            previous_state = self.state
            self.state = CorridorState.NORMAL
            self._record_transition(simulation_time, previous_state, None)

    def record_downstream_block(
        self,
        tls_id: str,
        reason: str,
        simulation_time: float,
    ) -> None:
        self.downstream_block_count += 1
        self.downstream_block_reasons[reason] = (
            self.downstream_block_reasons.get(reason, 0) + 1
        )
        self.decision_events.append(
            DecisionEvent(
                time=round(simulation_time, 3),
                category="corridor",
                title="Пріоритет швидкої заблоковано safety gate",
                detail=f"Перехрестя {tls_id}: {reason}.",
                level="warning",
                tls_id=tls_id,
            )
        )
        self.decision_events = self.decision_events[-120:]

    def as_summary(self) -> dict[str, object]:
        return {
            "corridor_state": self.state.name,
            "corridor_active_tls": self.active_tls,
            "corridor_prepared_tls": tuple(item[0] for item in self.prepared_tls),
            "corridor_completed_tls": tuple(self.completed_tls),
            "corridor_unconfirmed_tls": tuple(self.unconfirmed_tls),
            "corridor_affected_tls": tuple(self.affected_tls),
            "corridor_recovery_tls": tuple(self.recovery_tls),
            "corridor_restored_tls": tuple(self.restored_tls),
            "corridor_downstream_blocks": self.downstream_block_count,
            "corridor_downstream_block_reasons": self.downstream_block_reasons,
        }

    def _confirm_passages(
        self,
        simulation_time: float,
        passed_tls_ids: tuple[str, ...],
    ) -> None:
        for tls_id in passed_tls_ids:
            if tls_id not in self.completed_tls:
                self.completed_tls.append(tls_id)
                self.decision_events.append(
                    DecisionEvent(
                        time=round(simulation_time, 3),
                        category="corridor",
                        title="Підтверджено проїзд перехрестя",
                        detail=f"Швидка фізично перетнула stop line {tls_id}.",
                        level="success",
                        tls_id=tls_id,
                    )
                )
            if tls_id in self.unconfirmed_tls:
                self.unconfirmed_tls.remove(tls_id)
            if tls_id == self.active_tls:
                self.active_tls = None
                self.active_link_index = None
        self.decision_events = self.decision_events[-120:]

    def _handle_missing_vehicle(self, simulation_time: float) -> None:
        if self.state == CorridorState.NORMAL:
            return
        if self.state == CorridorState.RECOVERY:
            self._advance_recovery(simulation_time)
            return
        if (
            self._last_vehicle_seen_time >= 0
            and simulation_time - self._last_vehicle_seen_time <= self.timeout_seconds
        ):
            return
        if self.active_tls and self.active_tls not in self.completed_tls:
            if self.active_tls not in self.unconfirmed_tls:
                self.unconfirmed_tls.append(self.active_tls)
        self.active_tls = None
        self.active_link_index = None
        self.prepared_tls = ()
        self._enter_recovery(simulation_time)

    def _enter_clearance(self, simulation_time: float) -> None:
        self.state = CorridorState.CLEARANCE
        self._clearance_start_time = simulation_time

    def _enter_recovery(self, simulation_time: float) -> None:
        self.state = CorridorState.RECOVERY
        self._recovery_start_time = simulation_time
        self.recovery_tls = list(self.affected_tls)
        self.restored_tls = [
            tls_id for tls_id in self.restored_tls if tls_id in self.recovery_tls
        ]
        if not self.recovery_tls:
            self.state = CorridorState.NORMAL

    def _advance_recovery(self, simulation_time: float) -> None:
        if set(self.recovery_tls).issubset(self.restored_tls):
            self.state = CorridorState.NORMAL
        elif simulation_time - self._recovery_start_time >= self.recovery_seconds:
            self.state = CorridorState.NORMAL

    def _record_transition(
        self,
        simulation_time: float,
        previous_state: CorridorState,
        previous_tls: str | None,
    ) -> None:
        if self.state == previous_state and self.active_tls == previous_tls:
            return
        tls_id = self.active_tls or previous_tls
        descriptions = {
            CorridorState.NORMAL: (
                "Рух повернувся до нормального режиму",
                "Світлофори знову керуються загальним транспортним попитом.",
                "success",
            ),
            CorridorState.PREPARE: (
                "Підготовка зеленого коридору",
                (
                    f"FlowMind м'яко готує {len(self.prepared_tls)} "
                    "наступних перехресть без hard priority."
                ),
                "warning",
            ),
            CorridorState.GREEN_WINDOW: (
                "Зелений коридор активовано",
                f"Швидка отримала пріоритет на перехресті {tls_id or '—'}.",
                "success",
            ),
            CorridorState.CLEARANCE: (
                "Швидка пройшла контрольовану ділянку",
                "Коридор витримує clearance перед відновленням offset-плану.",
                "info",
            ),
            CorridorState.RECOVERY: (
                "Відновлення фазових offset",
                f"FlowMind синхронізує {len(self.recovery_tls)} перехресть.",
                "info",
            ),
        }
        title, detail, level = descriptions[self.state]
        self.decision_events.append(
            DecisionEvent(
                time=round(simulation_time, 3),
                category="corridor",
                title=title,
                detail=detail,
                level=level,
                tls_id=tls_id,
            )
        )
        self.decision_events = self.decision_events[-120:]

    @staticmethod
    def _normalize_upcoming(
        next_tls_info: NextTlsInfo | list[NextTlsInfo] | None,
    ) -> tuple[NextTlsInfo, ...]:
        if next_tls_info is None:
            return ()
        raw = [next_tls_info] if isinstance(next_tls_info, tuple) else next_tls_info
        nearest_by_tls: dict[str, NextTlsInfo] = {}
        for tls_id, link_index, distance in raw:
            item = (str(tls_id), int(link_index), max(float(distance), 0.0))
            previous = nearest_by_tls.get(item[0])
            if previous is None or item[2] < previous[2]:
                nearest_by_tls[item[0]] = item
        return tuple(sorted(nearest_by_tls.values(), key=lambda item: item[2]))
