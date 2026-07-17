from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from flowmind.config import PROJECT_ROOT, RunConfig
from flowmind.experiment import load_area
from flowmind.runtime_contract import validate_runtime_contract


class RuntimeContractTest(unittest.TestCase):
    def test_rivne_runtime_contract_reports_twenty_tls(self) -> None:
        config = RunConfig(mode="flowmind", queue_model_paths=())
        area = load_area(config)
        network = PROJECT_ROOT / "simulation" / "rivne_area" / "osm.net.xml.gz"
        with patch.dict(os.environ, {"FLOWMIND_STRICT_STARTUP": "0"}, clear=False):
            contract = validate_runtime_contract(config, area, network, None)

        self.assertTrue(contract.valid)
        self.assertEqual(contract.controlled_tls_count, 20)
        self.assertEqual(contract.configured_tls_count, 20)
        self.assertTrue(contract.network_sha256)
        self.assertTrue(contract.zone_sha256)

    def test_strict_contract_rejects_unversioned_image(self) -> None:
        config = RunConfig(mode="flowmind", queue_model_paths=())
        area = load_area(config)
        network = PROJECT_ROOT / "simulation" / "rivne_area" / "osm.net.xml.gz"
        with patch.dict(
            os.environ,
            {
                "FLOWMIND_STRICT_STARTUP": "1",
                "FLOWMIND_IMAGE_COMMIT": "unversioned",
            },
            clear=False,
        ):
            contract = validate_runtime_contract(config, area, network, None)

        self.assertFalse(contract.valid)
        with self.assertRaisesRegex(RuntimeError, "image git commit"):
            contract.raise_for_errors()


if __name__ == "__main__":
    unittest.main()
