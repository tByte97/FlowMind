from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dashboard import app


class DashboardAppTests(unittest.TestCase):
    def test_discover_result_sets_finds_live_status_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live_dir = root / "rivne_area"
            live_dir.mkdir(parents=True)
            (live_dir / "live_status.json").write_text("{}", encoding="utf-8")

            result_sets = app.discover_result_sets(root)

        self.assertEqual(result_sets, [live_dir.resolve()])


if __name__ == "__main__":
    unittest.main()
