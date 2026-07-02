from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
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

    def test_command_text_uses_platform_quoting(self) -> None:
        command = ["python", "folder with spaces/script.py", "--duration", "900"]
        self.assertEqual(
            project_manager.command_text(command),
            subprocess.list2cmdline(command),
        )


if __name__ == "__main__":
    unittest.main()
