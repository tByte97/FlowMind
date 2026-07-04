from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from flowmind.experiment import configure_projection_data


class ExperimentEnvironmentTest(unittest.TestCase):
    def test_packaged_projection_database_is_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            projection_dir = configure_projection_data()

            self.assertIsNotNone(projection_dir)
            self.assertTrue((projection_dir / "proj.db").is_file())
            self.assertEqual(os.environ["PROJ_DATA"], str(projection_dir))
            self.assertEqual(os.environ["PROJ_LIB"], str(projection_dir))


if __name__ == "__main__":
    unittest.main()
