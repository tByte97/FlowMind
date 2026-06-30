from __future__ import annotations

import json
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from flowmind.area_model import load_zone_tls_ids
from flowmind.config import PROJECT_ROOT, RunConfig


SCENARIO_DIR = PROJECT_ROOT / "simulation" / "rivne_area"


class FocusedScenarioTest(unittest.TestCase):
    def test_default_run_uses_focused_scenario(self) -> None:
        config = RunConfig(mode="fixed")
        self.assertEqual(config.config_path.name, "focused.sumocfg")
        self.assertEqual(config.zone_path.name, "central_zone.json")

    def test_manifest_covers_a_connected_six_light_zone(self) -> None:
        zone_tls = load_zone_tls_ids(SCENARIO_DIR / "central_zone.json")
        manifest = json.loads(
            (SCENARIO_DIR / "focused.manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(tuple(manifest["controlled_tls"]), zone_tls)

        routes = manifest["routes"]
        covered = {
            tls_id for route in routes for tls_id in route.get("tls_ids", [])
        }
        self.assertEqual(covered, set(zone_tls))
        self.assertTrue(all(len(route["tls_ids"]) >= 2 for route in routes))

        graph = {tls_id: set() for tls_id in zone_tls}
        for route in routes:
            for left, right in zip(
                route["tls_ids"], route["tls_ids"][1:], strict=False
            ):
                graph[left].add(right)
                graph[right].add(left)
        visited = {zone_tls[0]}
        frontier = [zone_tls[0]]
        while frontier:
            current = frontier.pop()
            for neighbour in graph[current] - visited:
                visited.add(neighbour)
                frontier.append(neighbour)
        self.assertEqual(visited, set(zone_tls))

    def test_route_file_contains_matching_routes_and_flows(self) -> None:
        root = ET.parse(SCENARIO_DIR / "focused.rou.xml").getroot()
        routes = root.findall("route")
        flows = root.findall("flow")
        self.assertEqual(len(routes), 8)
        self.assertEqual(len(flows), 8)
        self.assertTrue(all(len(route.attrib["edges"].split()) >= 2 for route in routes))
        self.assertEqual(
            {flow.attrib["route"] for flow in flows},
            {route.attrib["id"] for route in routes},
        )


if __name__ == "__main__":
    unittest.main()
