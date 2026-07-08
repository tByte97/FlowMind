from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import socket
from pathlib import Path

import project_manager


class ProjectManagerTests(unittest.TestCase):
    def test_project_python_prefers_posix_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.touch()

            self.assertEqual(project_manager.project_python(root), executable.absolute())

    def test_project_python_prefers_windows_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ".venv" / "Scripts" / "python.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()

            self.assertEqual(project_manager.project_python(root), executable.absolute())

    def test_project_python_prefers_repo_venv_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "venv" / "Scripts" / "python.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()

            self.assertEqual(project_manager.project_python(root), executable.absolute())

    def test_project_python_falls_back_to_current_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                project_manager.project_python(Path(directory)),
                Path(sys.executable).resolve(),
            )

    def test_positive_int(self) -> None:
        self.assertEqual(project_manager.positive_int("0", "seed", 0), 0)
        self.assertEqual(project_manager.positive_int("10", "duration"), 10)
        with self.assertRaisesRegex(ValueError, "duration"):
            project_manager.positive_int("0", "duration")
        with self.assertRaisesRegex(ValueError, "duration"):
            project_manager.positive_int("abc", "duration")

    def test_network_port_validation(self) -> None:
        self.assertEqual(project_manager.network_port("8765", "WebSocket"), 8765)
        with self.assertRaisesRegex(ValueError, "1–65535"):
            project_manager.network_port("70000", "WebSocket")

    def test_command_text_uses_platform_quoting(self) -> None:
        command = ["python", "folder with spaces/script.py", "--duration", "900"]
        self.assertEqual(
            project_manager.command_text(command),
            subprocess.list2cmdline(command),
        )

    def test_queue_model_path_helpers(self) -> None:
        paths = (
            Path("models/queue_lgbm_30s_current.joblib"),
            Path("models/queue_lgbm_60s_current.joblib"),
        )

        text = project_manager.format_queue_model_paths(paths)
        parsed = project_manager.parse_queue_model_paths(text)

        self.assertEqual(parsed, paths)

    def test_default_queue_model_preset_has_three_horizons(self) -> None:
        paths = project_manager.QUEUE_MODEL_PRESETS[
            project_manager.FORECAST_PRESET_ENSEMBLE
        ]

        self.assertEqual(len(paths), 3)
        self.assertIn("30s", paths[0].name)
        self.assertIn("60s", paths[1].name)
        self.assertIn("90s", paths[2].name)

    def test_scenario_profile_prefers_focused_scenario_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "new_area"
            scenario.mkdir()
            for name in (
                "osm.sumocfg",
                "focused.sumocfg",
                "central_zone.json",
                "emergency.json",
                "osm.net.xml.gz",
            ):
                (scenario / name).touch()

            profile = project_manager.scenario_profile_from_directory(scenario)

        self.assertEqual(profile.name, "new_area")
        self.assertEqual(profile.config_path.name, "focused.sumocfg")
        self.assertEqual(profile.zone_path.name, "central_zone.json")
        self.assertEqual(profile.emergency_path.name, "emergency.json")
        self.assertEqual(profile.net_path.name, "osm.net.xml.gz")

    def test_discover_scenarios_orders_default_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            simulation = Path(directory)
            default = simulation / "rivne_area"
            custom = simulation / "new_area"
            for scenario in (custom, default):
                scenario.mkdir()
                (scenario / "focused.sumocfg").touch()
                (scenario / "central_zone.json").touch()
                (scenario / "osm.net.xml.gz").touch()

            profiles = project_manager.discover_scenarios(simulation)

        self.assertEqual([profile.name for profile in profiles], ["rivne_area", "new_area"])

    def test_find_free_port_skips_busy_port(self) -> None:
        try:
            busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError as error:
            self.skipTest(f"sockets are unavailable in this environment: {error}")
        with busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            occupied = busy.getsockname()[1]

            free_port = project_manager.find_free_port(occupied, attempts=5)

        self.assertNotEqual(free_port, occupied)
        self.assertGreater(free_port, occupied)


if __name__ == "__main__":
    unittest.main()
