from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .area_model import AreaModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmergencyRouteOption:
    edge_ids: tuple[str, ...]
    tls_sequence: tuple[str, ...]
    length: float
    base_travel_time: float
    queue_penalty: float
    predicted_eta: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_ids": self.edge_ids,
            "tls_sequence": self.tls_sequence,
            "length": round(self.length, 2),
            "base_travel_time": round(self.base_travel_time, 2),
            "queue_penalty": round(self.queue_penalty, 2),
            "predicted_eta": round(self.predicted_eta, 2),
        }


class EmergencyRouter:
    """Calculates and selects alternative routes for an emergency vehicle."""

    def __init__(self, traci_connection: Any, area: AreaModel) -> None:
        self._traci = traci_connection
        self._area = area

    def _get_tls_sequence(self, edges: tuple[str, ...]) -> tuple[str, ...]:
        """Extract the sequence of traffic lights along the route."""
        sequence = []
        # Create a mapping from (incoming_edge, outgoing_edge) to tls_id
        edge_to_tls = {}
        for intersection in self._area.intersections:
            for link in intersection.links:
                in_edge = link.incoming_lane.rsplit("_", 1)[0]
                out_edge = link.outgoing_lane.rsplit("_", 1)[0]
                edge_to_tls[(in_edge, out_edge)] = intersection.tls_id

        for i in range(len(edges) - 1):
            pair = (edges[i], edges[i + 1])
            if pair in edge_to_tls:
                tls_id = edge_to_tls[pair]
                if not sequence or sequence[-1] != tls_id:
                    sequence.append(tls_id)
        return tuple(sequence)

    def _estimate_queue_penalty(self, edges: tuple[str, ...]) -> float:
        """Estimate time penalty due to current vehicle queues on the edges."""
        penalty = 0.0
        for edge_id in edges:
            try:
                # Approximate 2 seconds penalty per halting vehicle on the edge
                halting = self._traci.edge.getLastStepHaltingNumber(edge_id)
                penalty += halting * 2.0
            except Exception:
                pass
        return penalty

    def find_alternatives(
        self, start_edge: str, end_edge: str, vtype: str, depart_time: float, num_alternatives: int = 3
    ) -> tuple[EmergencyRouteOption | None, list[dict[str, Any]]]:
        """Find multiple valid alternative routes and select the best one.
        
        Returns a tuple of (best_route, all_alternatives_logs).
        """
        alternatives: list[EmergencyRouteOption] = []
        penalized_edges: dict[str, float] = {}

        try:
            for i in range(num_alternatives):
                stage = self._traci.simulation.findRoute(
                    start_edge, end_edge, vtype, depart_time, routingMode=0
                )
                edges = tuple(stage.edges)
                if len(edges) < 2:
                    break  # No valid route found

                # Prevent adding duplicate identical routes
                if any(alt.edge_ids == edges for alt in alternatives):
                    # We found a duplicate, try to penalize more or break
                    break

                length = float(stage.length)
                base_time = float(stage.travelTime)
                queue_penalty = self._estimate_queue_penalty(edges)
                eta = base_time + queue_penalty
                tls_seq = self._get_tls_sequence(edges)

                option = EmergencyRouteOption(
                    edge_ids=edges,
                    tls_sequence=tls_seq,
                    length=length,
                    base_travel_time=base_time,
                    queue_penalty=queue_penalty,
                    predicted_eta=eta,
                )
                alternatives.append(option)

                # Penalize edges to force finding an alternative next iteration
                for edge in edges:
                    try:
                        current_effort = self._traci.edge.getEffort(edge, depart_time)
                        if current_effort < 0:
                            current_effort = self._traci.edge.getTraveltime(edge)
                        new_effort = current_effort * 1.5  # 50% penalty
                        self._traci.edge.setEffort(edge, new_effort)
                        penalized_edges[edge] = current_effort
                    except Exception:
                        pass
        finally:
            # Restore original efforts
            for edge, effort in penalized_edges.items():
                try:
                    self._traci.edge.setEffort(edge, effort)
                except Exception:
                    pass

        if not alternatives:
            return None, []

        # Choose the route with the lowest predicted ETA
        best_route = min(alternatives, key=lambda r: r.predicted_eta)
        
        logs = []
        for alt in alternatives:
            reason = "Selected (Lowest ETA)" if alt == best_route else "Rejected (Higher ETA or duplicate)"
            log_entry = alt.as_dict()
            log_entry["reason"] = reason
            logs.append(log_entry)

        return best_route, logs
