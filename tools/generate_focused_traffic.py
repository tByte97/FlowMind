from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sumolib

from flowmind.area_model import AreaModel, discover_area, load_zone_tls_ids
from flowmind.config import PROJECT_ROOT


@dataclass(frozen=True)
class RouteCandidate:
    edges: tuple[str, ...]
    tls_ids: tuple[str, ...]
    cost: float


def _edge_position(edge: object) -> tuple[float, float]:
    start = edge.getFromNode().getCoord()
    end = edge.getToNode().getCoord()
    return ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)


def _traffic_light_transitions(
    network: object, area: AreaModel
) -> dict[tuple[str, str], str]:
    transitions: dict[tuple[str, str], str] = {}
    for intersection in area.intersections:
        for link in intersection.links:
            incoming = network.getLane(link.incoming_lane).getEdge().getID()
            outgoing = network.getLane(link.outgoing_lane).getEdge().getID()
            transitions[(incoming, outgoing)] = intersection.tls_id
    return transitions


def _route_tls_ids(
    edge_ids: tuple[str, ...], transitions: dict[tuple[str, str], str]
) -> tuple[str, ...]:
    result: list[str] = []
    for edge_pair in zip(edge_ids, edge_ids[1:], strict=False):
        tls_id = transitions.get(edge_pair)
        if tls_id is not None and (not result or result[-1] != tls_id):
            result.append(tls_id)
    return tuple(result)


def _gateway_edges(
    network: object,
    centre: tuple[float, float],
    inner_radius: float,
    outer_radius: float,
    sectors: int = 8,
    per_sector: int = 12,
) -> dict[int, tuple[object, ...]]:
    candidates: dict[int, list[tuple[float, str, object]]] = {
        index: [] for index in range(sectors)
    }
    for edge in network.getEdges():
        if edge.isSpecial() or not edge.allows("passenger") or edge.getLength() < 60:
            continue
        x, y = _edge_position(edge)
        radius = math.hypot(x - centre[0], y - centre[1])
        if not inner_radius < radius < outer_radius:
            continue
        angle = math.atan2(y - centre[1], x - centre[0])
        sector = int(((angle + math.pi) / (2 * math.pi)) * sectors) % sectors
        road_score = edge.getSpeed() * edge.getLaneNumber() * edge.getLength()
        candidates[sector].append((-road_score, edge.getID(), edge))

    return {
        sector: tuple(
            item[2]
            for item in sorted(items, key=lambda item: (item[0], item[1]))[
                :per_sector
            ]
        )
        for sector, items in candidates.items()
    }


def discover_focused_routes(
    network: object,
    area: AreaModel,
    route_count: int = 8,
    inner_radius: float = 1_200.0,
    outer_radius: float = 2_200.0,
) -> tuple[RouteCandidate, ...]:
    centre = (
        sum(item.position[0] for item in area.intersections)
        / len(area.intersections),
        sum(item.position[1] for item in area.intersections)
        / len(area.intersections),
    )
    gateways = _gateway_edges(network, centre, inner_radius, outer_radius)
    transitions = _traffic_light_transitions(network, area)
    candidates_by_pattern: dict[tuple[str, ...], RouteCandidate] = {}

    for start_sector, start_edges in gateways.items():
        for end_sector, end_edges in gateways.items():
            sector_distance = min(
                (start_sector - end_sector) % len(gateways),
                (end_sector - start_sector) % len(gateways),
            )
            if sector_distance < 3:
                continue
            for start_edge in start_edges:
                for end_edge in end_edges:
                    path, cost = network.getShortestPath(
                        start_edge,
                        end_edge,
                        maxCost=8_000,
                        vClass="passenger",
                    )
                    if not path:
                        continue
                    edge_ids = tuple(edge.getID() for edge in path)
                    tls_ids = _route_tls_ids(edge_ids, transitions)
                    if len(set(tls_ids)) < 2:
                        continue
                    candidate = RouteCandidate(edge_ids, tls_ids, float(cost))
                    current = candidates_by_pattern.get(tls_ids)
                    if current is None or candidate.cost < current.cost:
                        candidates_by_pattern[tls_ids] = candidate

    candidates = list(candidates_by_pattern.values())
    if not candidates:
        raise RuntimeError("Could not find routes crossing multiple zone intersections")

    selected: list[RouteCandidate] = []
    coverage = {tls_id: 0 for tls_id in area.tls_ids}
    while candidates and len(selected) < route_count:
        best = max(
            candidates,
            key=lambda item: (
                sum(max(2 - coverage[tls_id], 0) for tls_id in set(item.tls_ids)),
                len(set(item.tls_ids)),
                -item.cost,
            ),
        )
        selected.append(best)
        candidates.remove(best)
        for tls_id in set(best.tls_ids):
            coverage[tls_id] += 1

    uncovered = [tls_id for tls_id, count in coverage.items() if count == 0]
    if uncovered:
        raise RuntimeError(
            "Focused routes do not cover all zone traffic lights: "
            + ", ".join(uncovered)
        )

    connected: dict[str, set[str]] = {tls_id: set() for tls_id in area.tls_ids}
    for route in selected:
        for left, right in zip(route.tls_ids, route.tls_ids[1:], strict=False):
            connected[left].add(right)
            connected[right].add(left)
    visited = {area.tls_ids[0]}
    frontier = [area.tls_ids[0]]
    while frontier:
        current = frontier.pop()
        for neighbour in connected[current] - visited:
            visited.add(neighbour)
            frontier.append(neighbour)
    if visited != set(area.tls_ids):
        raise RuntimeError("Selected traffic-light zone is not route-connected")

    return tuple(selected)


def write_routes(
    output_path: Path,
    routes: tuple[RouteCandidate, ...],
    duration: int,
    vehicles_per_hour: int,
) -> None:
    root = ET.Element(
        "routes",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/routes_file.xsd",
        },
    )
    ET.SubElement(
        root,
        "vType",
        {
            "id": "zone_passenger",
            "vClass": "passenger",
            "color": "0.18,0.55,0.95",
            "sigma": "0.3",
        },
    )
    flow_rate = max(round(vehicles_per_hour / len(routes)), 1)
    for index, route in enumerate(routes):
        route_id = f"zone_route_{index:02d}"
        ET.SubElement(
            root,
            "route",
            {"id": route_id, "edges": " ".join(route.edges)},
        )
        ET.SubElement(
            root,
            "flow",
            {
                "id": f"zone_flow_{index:02d}",
                "type": "zone_passenger",
                "route": route_id,
                "begin": "0",
                "end": str(duration),
                "vehsPerHour": str(flow_rate),
                "departLane": "best",
                "departSpeed": "max",
            },
        )

    ET.indent(root, space="    ")
    tree = ET.ElementTree(root)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def write_sumo_config(output_path: Path, duration: int) -> None:
    root = ET.Element(
        "sumoConfiguration",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/sumoConfiguration.xsd",
        },
    )
    inputs = ET.SubElement(root, "input")
    ET.SubElement(inputs, "net-file", {"value": "osm.net.xml.gz"})
    ET.SubElement(inputs, "route-files", {"value": "focused.rou.xml"})
    ET.SubElement(inputs, "additional-files", {"value": "osm.poly.xml.gz"})
    time = ET.SubElement(root, "time")
    ET.SubElement(time, "begin", {"value": "0"})
    ET.SubElement(time, "end", {"value": str(duration)})
    processing = ET.SubElement(root, "processing")
    ET.SubElement(processing, "ignore-route-errors", {"value": "false"})
    ET.SubElement(processing, "time-to-teleport", {"value": "300"})
    report = ET.SubElement(root, "report")
    ET.SubElement(report, "no-step-log", {"value": "true"})
    gui = ET.SubElement(root, "gui_only")
    ET.SubElement(gui, "gui-settings-file", {"value": "osm.view.xml"})
    ET.indent(root, space="    ")
    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)


def write_manifest(
    output_path: Path,
    area: AreaModel,
    routes: tuple[RouteCandidate, ...],
    duration: int,
    vehicles_per_hour: int,
) -> None:
    payload = {
        "duration": duration,
        "vehicles_per_hour": vehicles_per_hour,
        "controlled_tls": list(area.tls_ids),
        "routes": [
            {
                "id": f"zone_route_{index:02d}",
                "edge_count": len(route.edges),
                "length": round(route.cost, 2),
                "tls_ids": list(route.tls_ids),
            }
            for index, route in enumerate(routes)
        ],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    scenario_dir = PROJECT_ROOT / "simulation" / "rivne_area"
    parser = argparse.ArgumentParser(
        description="Generate traffic focused on the central FlowMind zone"
    )
    parser.add_argument(
        "--net", type=Path, default=scenario_dir / "osm.net.xml.gz"
    )
    parser.add_argument(
        "--zone", type=Path, default=scenario_dir / "central_zone.json"
    )
    parser.add_argument("--output-dir", type=Path, default=scenario_dir)
    parser.add_argument("--duration", type=int, default=1_800)
    parser.add_argument("--vehicles-per-hour", type=int, default=2_400)
    parser.add_argument("--routes", type=int, default=8)
    args = parser.parse_args()

    if args.duration <= 0 or args.vehicles_per_hour <= 0 or args.routes < 2:
        parser.error("duration, vehicles-per-hour and routes must be positive")

    network = sumolib.net.readNet(
        str(args.net), withPrograms=True, withConnections=True
    )
    tls_ids = load_zone_tls_ids(args.zone)
    area = discover_area(args.net, requested_tls=tls_ids)
    routes = discover_focused_routes(network, area, args.routes)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_routes(
        args.output_dir / "focused.rou.xml",
        routes,
        args.duration,
        args.vehicles_per_hour,
    )
    write_sumo_config(args.output_dir / "focused.sumocfg", args.duration)
    write_manifest(
        args.output_dir / "focused.manifest.json",
        area,
        routes,
        args.duration,
        args.vehicles_per_hour,
    )
    print(
        f"Generated {len(routes)} focused routes covering "
        f"{len(area.tls_ids)} connected traffic lights"
    )


if __name__ == "__main__":
    main()
