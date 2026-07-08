from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from tools.build_sumo_map import (
    BuildConfig,
    BuildError,
    SumoTools,
    ensure_outputs_available,
    is_osm_file,
    netconvert_command,
    random_trips_command,
    validate_config,
    write_sumo_config,
)


def fake_tools(root: Path) -> SumoTools:
    return SumoTools(
        home=root,
        netconvert=root / "bin" / "netconvert",
        polyconvert=root / "bin" / "polyconvert",
        sumo=root / "bin" / "sumo",
        random_trips=root / "tools" / "randomTrips.py",
        net_typemap=root / "data" / "typemap" / "osmNetconvert.typ.xml",
        urban_typemap=root
        / "data"
        / "typemap"
        / "osmNetconvertUrbanDe.typ.xml",
        polygon_typemap=root
        / "data"
        / "typemap"
        / "osmPolyconvert.typ.xml",
    )


class BuildSumoMapTests(unittest.TestCase):
    def test_recognizes_supported_osm_names(self) -> None:
        for name in ("map.osm", "map.osm.xml", "map.osm.gz", "map.osm.xml.gz"):
            with self.subTest(name=name):
                self.assertTrue(is_osm_file(Path(name)))
        self.assertFalse(is_osm_file(Path("map.xml")))

    def test_config_validation_rejects_missing_input_and_unsafe_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = BuildConfig(root / "map.osm", root / "output")
            with self.assertRaisesRegex(BuildError, "не знайдено"):
                validate_config(missing)

            source = root / "map.osm"
            source.touch()
            unsafe = BuildConfig(source, root / "output", prefix="../map")
            with self.assertRaisesRegex(BuildError, "Prefix"):
                validate_config(unsafe)

    def test_existing_output_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.osm"
            source.touch()
            config = BuildConfig(source, root)
            config.network_path.touch()

            with self.assertRaisesRegex(BuildError, "--force"):
                ensure_outputs_available(config)
            ensure_outputs_available(
                BuildConfig(source, root, force=True)
            )

    def test_commands_use_requested_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BuildConfig(
                source_osm=root / "source.osm",
                output_dir=root / "result",
                duration=900,
                insertion_rate=2_400,
                seed=77,
            )
            tools = fake_tools(root / "sumo")

            net_command = netconvert_command(config, tools)
            self.assertIn(str(config.source_osm), net_command)
            self.assertIn(str(config.network_path), net_command)
            self.assertIn("--tls.join", net_command)

            traffic_command = random_trips_command(config, tools)
            self.assertEqual(
                traffic_command[traffic_command.index("--end") + 1],
                "900",
            )
            self.assertEqual(
                traffic_command[traffic_command.index("--insertion-rate") + 1],
                "2400",
            )
            self.assertEqual(
                traffic_command[traffic_command.index("--seed") + 1],
                "77",
            )

    def test_sumo_config_matches_enabled_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.osm"
            source.touch()

            full = BuildConfig(source, root, duration=600)
            write_sumo_config(full)
            config_root = ET.parse(full.sumo_config_path).getroot()
            self.assertEqual(
                config_root.find("./input/net-file").attrib["value"],
                "osm.net.xml.gz",
            )
            self.assertEqual(
                config_root.find("./input/route-files").attrib["value"],
                "osm.passenger.rou.xml",
            )
            self.assertEqual(
                config_root.find("./input/additional-files").attrib["value"],
                "osm.poly.xml.gz",
            )

            minimal = BuildConfig(
                source,
                root,
                prefix="minimal",
                polygons=False,
                traffic=False,
            )
            write_sumo_config(minimal)
            config_root = ET.parse(minimal.sumo_config_path).getroot()
            self.assertIsNone(config_root.find("./input/route-files"))
            self.assertIsNone(config_root.find("./input/additional-files"))


if __name__ == "__main__":
    unittest.main()
