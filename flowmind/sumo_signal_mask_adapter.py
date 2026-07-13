from __future__ import annotations

from copy import deepcopy


class SumoSignalMaskAdapter:
    """Apply fail-closed movement masks without changing phase order.

    The adapter clones the active, startup-validated SUMO logic and can only
    replace an existing green signal with red. It never creates a new green,
    changes phase timing, or changes yellow/all-red successor phases.
    """

    def __init__(self, traci_connection: object) -> None:
        self._trafficlight = traci_connection.trafficlight
        self._base_logic_by_tls: dict[str, object] = {}
        self._last_masks_by_tls: dict[
            str,
            tuple[tuple[int, tuple[int, ...]], ...],
        ] = {}

    @property
    def supported(self) -> bool:
        return all(
            callable(getattr(self._trafficlight, name, None))
            for name in (
                "getProgram",
                "getAllProgramLogics",
                "setProgramLogic",
                "setPhase",
            )
        )

    def has_active_mask(self, tls_id: str) -> bool:
        return bool(self._last_masks_by_tls.get(tls_id, ()))

    def synchronize(
        self,
        tls_id: str,
        current_phase: int,
        masks_by_phase: dict[int, tuple[int, ...]],
    ) -> bool:
        normalized = tuple(
            sorted(
                (int(phase), tuple(sorted(set(indices))))
                for phase, indices in masks_by_phase.items()
                if indices
            )
        )
        if normalized == self._last_masks_by_tls.get(tls_id, ()):
            return True
        if not self.supported:
            return not normalized
        base_logic = self._base_logic_by_tls.get(tls_id)
        if base_logic is None:
            program_id = str(self._trafficlight.getProgram(tls_id))
            base_logic = next(
                (
                    logic
                    for logic in self._trafficlight.getAllProgramLogics(tls_id)
                    if str(logic.programID) == program_id
                ),
                None,
            )
            if base_logic is None:
                return False
            self._base_logic_by_tls[tls_id] = deepcopy(base_logic)

        logic = deepcopy(base_logic)
        logic.currentPhaseIndex = int(current_phase)
        for phase_index, signal_indices in normalized:
            if not 0 <= phase_index < len(logic.phases):
                return False
            phase = logic.phases[phase_index]
            state = list(str(phase.state))
            for signal_index in signal_indices:
                if not 0 <= signal_index < len(state):
                    return False
                # Removing right of way is conflict-monotone and therefore
                # preserves the startup-validated conflict matrix.
                if state[signal_index] not in "Gg":
                    return False
                state[signal_index] = "r"
            phase.state = "".join(state)

        self._trafficlight.setProgramLogic(tls_id, logic)
        self._trafficlight.setPhase(tls_id, int(current_phase))
        self._last_masks_by_tls[tls_id] = normalized
        return True

    def restore(self, tls_id: str, current_phase: int) -> bool:
        """Restore the unmasked startup program before handing TLS control off."""

        return self.synchronize(tls_id, current_phase, {})


__all__ = ["SumoSignalMaskAdapter"]
