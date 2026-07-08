from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx
import sumolib

from .area_model import AreaModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmergencyRouteOption:
    edge_ids: tuple[str, ...]
    tls_sequence: tuple[str, ...]
    length: float
    base_travel_time: float
    queue_penalty: float
    blocked_edge_penalty: float
    corridor_activation_cost: float
    civil_traffic_impact: float
    predicted_eta: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_ids": list(self.edge_ids),
            "tls_sequence": list(self.tls_sequence),
            "length": round(self.length, 2),
            "base_travel_time": round(self.base_travel_time, 2),
            "queue_penalty": round(self.queue_penalty, 2),
            "blocked_edge_penalty": round(self.blocked_edge_penalty, 2),
            "corridor_activation_cost": round(self.corridor_activation_cost, 2),
            "civil_traffic_impact": round(self.civil_traffic_impact, 2),
            "predicted_eta": round(self.predicted_eta, 2),
        }


class EmergencyRouter:
    """Calculates and selects alternative routes for an emergency vehicle."""

    def __init__(
        self,
        traci_connection: Any,
        area: AreaModel,
        net_path: str | Path | None = None,
    ) -> None:
        self._traci = traci_connection
        self._area = area
        self._network = (
            sumolib.net.readNet(
                str(net_path), withPrograms=True, withConnections=True
            )
            if net_path is not None
            else None
        )
        self._graph: nx.DiGraph | None = (
            self._build_graph(self._network) if self._network is not None else None
        )
        self._edge_to_tls = (
            self._build_edge_to_tls(self._network)
            if self._network is not None
            else self._build_area_edge_to_tls(area)
        )

    @staticmethod
    def _edge_base_time(edge: object) -> float:
        return float(edge.getLength()) / max(float(edge.getSpeed()), 0.1)

    def _build_graph(self, network: object) -> nx.DiGraph:
        graph = nx.DiGraph()
        for edge in network.getEdges():
            if edge.isSpecial() or not edge.allows("passenger"):
                continue
            edge_id = edge.getID()
            graph.add_node(edge_id, base_time=self._edge_base_time(edge))
            for outgoing in edge.getOutgoing():
                if outgoing.isSpecial() or not outgoing.allows("passenger"):
                    continue
                graph.add_edge(
                    edge_id,
                    outgoing.getID(),
                    weight=self._edge_base_time(outgoing),
                )
        return graph

    @staticmethod
    def _build_area_edge_to_tls(area: AreaModel) -> dict[tuple[str, str], str]:
        edge_to_tls = {}
        for intersection in area.intersections:
            for link in intersection.links:
                in_edge = link.incoming_lane.rsplit("_", 1)[0]
                out_edge = link.outgoing_lane.rsplit("_", 1)[0]
                edge_to_tls[(in_edge, out_edge)] = intersection.tls_id
        return edge_to_tls

    @staticmethod
    def _build_edge_to_tls(network: object) -> dict[tuple[str, str], str]:
        edge_to_tls = {}
        for tls in network.getTrafficLights():
            for connection in tls.getConnections():
                in_edge = connection[0].getID().rsplit("_", 1)[0]
                out_edge = connection[1].getID().rsplit("_", 1)[0]
                edge_to_tls[(in_edge, out_edge)] = str(tls.getID())
        return edge_to_tls

    def _get_tls_sequence(self, edges: tuple[str, ...]) -> tuple[str, ...]:
        """Extract the sequence of traffic lights along the route."""
        sequence = []
        for i in range(len(edges) - 1):
            pair = (edges[i], edges[i + 1])
            if pair in self._edge_to_tls:
                tls_id = self._edge_to_tls[pair]
                if not sequence or sequence[-1] != tls_id:
                    sequence.append(tls_id)
        return tuple(sequence)

    def _estimate_queue_penalty(self, edges: tuple[str, ...]) -> float:
        """Estimate time penalty due to current vehicle queues on the edges."""
        penalty = 0.0
        for edge_id in edges:
            try:
                halting = self._traci.edge.getLastStepHaltingNumber(edge_id)
                penalty += halting * 2.0
            except Exception:
                pass
        return penalty

    def _estimate_blocked_edge_penalty(self, edges: tuple[str, ...]) -> float:
        penalty = 0.0
        for edge_id in edges:
            try:
                halting = float(self._traci.edge.getLastStepHaltingNumber(edge_id))
                vehicles = float(self._traci.edge.getLastStepVehicleNumber(edge_id))
                speed = float(self._traci.edge.getLastStepMeanSpeed(edge_id))
            except Exception:
                continue
            if vehicles <= 0:
                continue
            halted_share = halting / max(vehicles, 1.0)
            if halted_share >= 0.75 and speed <= 1.5:
                penalty += 45.0
            elif halted_share >= 0.5 and speed <= 3.0:
                penalty += 20.0
        return penalty

    @staticmethod
    def _estimate_corridor_activation_cost(tls_sequence: tuple[str, ...]) -> float:
        return float(len(tls_sequence)) * 3.0

    @staticmethod
    def _estimate_civil_traffic_impact(
        queue_penalty: float,
        blocked_edge_penalty: float,
        tls_sequence: tuple[str, ...],
    ) -> float:
        return (
            queue_penalty * 0.2
            + blocked_edge_penalty * 0.1
            + float(len(tls_sequence)) * 1.5
        )

    def _route_option_from_edges(
        self, edges: tuple[str, ...]
    ) -> EmergencyRouteOption | None:
        if len(edges) < 2:
            return None
        length = 0.0
        base_time = 0.0
        if self._network is not None:
            for edge_id in edges:
                try:
                    edge = self._network.getEdge(edge_id)
                except KeyError:
                    return None
                length += float(edge.getLength())
                base_time += self._edge_base_time(edge)
        else:
            try:
                stage = self._traci.simulation.findRoute(edges[0], edges[-1])
            except Exception:
                return None
            length = float(stage.length)
            base_time = float(stage.travelTime)

        tls_sequence = self._get_tls_sequence(edges)
        queue_penalty = self._estimate_queue_penalty(edges)
        blocked_edge_penalty = self._estimate_blocked_edge_penalty(edges)
        corridor_activation_cost = self._estimate_corridor_activation_cost(
            tls_sequence
        )
        civil_traffic_impact = self._estimate_civil_traffic_impact(
            queue_penalty,
            blocked_edge_penalty,
            tls_sequence,
        )
        return EmergencyRouteOption(
            edge_ids=edges,
            tls_sequence=tls_sequence,
            length=length,
            base_travel_time=base_time,
            queue_penalty=queue_penalty,
            blocked_edge_penalty=blocked_edge_penalty,
            corridor_activation_cost=corridor_activation_cost,
            civil_traffic_impact=civil_traffic_impact,
            predicted_eta=(
                base_time
                + queue_penalty
                + blocked_edge_penalty
                + corridor_activation_cost
                + civil_traffic_impact
            ),
        )

    def _network_alternatives(
        self,
        start_edge: str,
        end_edge: str,
        num_alternatives: int,
    ) -> list[EmergencyRouteOption]:
        if self._graph is None:
            return []
        if start_edge not in self._graph or end_edge not in self._graph:
            return []

        alternatives: list[EmergencyRouteOption] = []
        seen: set[tuple[str, ...]] = set()
        try:
            paths = nx.shortest_simple_paths(
                self._graph, start_edge, end_edge, weight="weight"
            )
            for path in paths:
                edges = tuple(str(edge_id) for edge_id in path)
                if edges in seen:
                    continue
                seen.add(edges)
                option = self._route_option_from_edges(edges)
                if option is None:
                    continue
                alternatives.append(option)
                if len(alternatives) >= num_alternatives:
                    break
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
        return alternatives

    def find_alternatives(
        self,
        start_edge: str,
        end_edge: str,
        vtype: str,
        depart_time: float,
        num_alternatives: int = 3,
    ) -> tuple[EmergencyRouteOption | None, list[dict[str, Any]]]:
        """Find multiple valid alternative routes and select the best one.

        Returns a tuple of (best_route, all_alternatives_logs).
        """
        alternatives: list[EmergencyRouteOption] = self._network_alternatives(
            start_edge, end_edge, num_alternatives
        )
        penalized_edges: dict[str, float] = {}
        effort_end = depart_time + 3_600

        try:
            for attempt in range(max(num_alternatives * 5, 1)):
                if len(alternatives) >= num_alternatives:
                    break
                stage = self._traci.simulation.findRoute(
                    start_edge,
                    end_edge,
                    vtype,
                    depart_time,
                    routingMode=1 if penalized_edges else 0,
                )
                edges = tuple(stage.edges)
                if len(edges) < 2:
                    break  # No valid route found

                if not any(alt.edge_ids == edges for alt in alternatives):
                    option = self._route_option_from_edges(edges)
                    if option is not None:
                        alternatives.append(option)
                    if len(alternatives) >= num_alternatives:
                        break

                # Penalize the current route to force SUMO's router to search
                # around it on the next attempt. Start/end edges stay usable
                # because every alternative must still leave and arrive there.
                for edge in edges[1:-1]:
                    if edge.startswith(":"):
                        continue
                    try:
                        current_effort = self._traci.edge.getEffort(edge, depart_time)
                        if edge not in penalized_edges:
                            penalized_edges[edge] = current_effort
                        baseline = (
                            current_effort
                            if current_effort >= 0
                            else self._traci.edge.getTraveltime(edge)
                        )
                        new_effort = max(float(baseline), 1.0) * (
                            10.0 + attempt
                        )
                        self._traci.edge.setEffort(
                            edge, new_effort, depart_time, effort_end
                        )
                    except Exception:
                        pass
        finally:
            # Restore original efforts
            for edge, effort in penalized_edges.items():
                try:
                    self._traci.edge.setEffort(edge, effort, depart_time, effort_end)
                except Exception:
                    pass

        if not alternatives:
            return None, []

        # Choose the route with the lowest predicted ETA
        best_route = min(alternatives, key=lambda r: r.predicted_eta)
        
        logs = []
        for alt in alternatives:
            reason = (
                "Selected (Lowest ETA)"
                if alt == best_route
                else "Rejected (Higher ETA)"
            )
            log_entry = alt.as_dict()
            log_entry["reason"] = reason
            logs.append(log_entry)

        return best_route, logs
