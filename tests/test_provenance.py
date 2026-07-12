from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.config import RunConfig
from flowmind.provenance import (
    pair_config_sha256,
    run_config_sha256,
    scenario_provenance,
)


class ProvenanceTest(unittest.TestCase):
    def test_scenario_hashes_manifest_config_routes_network_and_zone(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "focused.sumocfg"
            route = root / "focused.rou.xml"
            network = root / "osm.net.xml.gz"
            zone = root / "central_zone.json"
            config.write_text(
                '<configuration><input><route-files value="focused.rou.xml"/>'
                "</input></configuration>",
                encoding="utf-8",
            )
            route.write_text("<routes/>", encoding="utf-8")
            network.write_bytes(b"network")
            zone.write_text("{}", encoding="utf-8")
            (root / "focused.manifest.json").write_text(
                json.dumps({"vehicles_per_hour": 3600, "duration": 1800}),
                encoding="utf-8",
            )

            result = scenario_provenance(config, zone, network)

        self.assertEqual(result.demand_vehicles_per_hour, 3600.0)
        self.assertEqual(result.demand_duration_seconds, 1800.0)
        self.assertTrue(result.scenario_sha256)
        self.assertTrue(result.route_files_sha256)
        self.assertTrue(result.network_sha256)

    def test_pair_hash_ignores_mode_but_run_hash_does_not(self) -> None:
        fixed = RunConfig(mode="static_fixed", seed=77)
        flowmind = RunConfig(mode="flowmind", seed=77)

        self.assertEqual(
            pair_config_sha256(fixed),
            pair_config_sha256(flowmind),
        )
        self.assertNotEqual(
            run_config_sha256(fixed),
            run_config_sha256(flowmind),
        )

    def test_pair_hash_changes_with_seed_or_emergency_route(self) -> None:
        first = RunConfig(mode="flowmind", seed=77)
        second = RunConfig(mode="flowmind", seed=78)

        self.assertNotEqual(pair_config_sha256(first), pair_config_sha256(second))
        self.assertNotEqual(
            pair_config_sha256(first, ("a", "b")),
            pair_config_sha256(first, ("a", "c")),
        )


if __name__ == "__main__":
    unittest.main()
