from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.config import ControlConfig
from flowmind.ml_approval import (
    ML_APPROVAL_SCHEMA_VERSION,
    validate_queue_control_approval,
)
from flowmind.provenance import canonical_sha256, file_sha256


class MLApprovalTest(unittest.TestCase):
    def test_approval_is_bound_to_models_zone_network_and_control(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.joblib"
            network = root / "network.xml"
            zone = root / "zone.json"
            approval = root / "approval.json"
            model.write_bytes(b"model")
            network.write_bytes(b"network")
            zone.write_text("{}", encoding="utf-8")
            control = ControlConfig()
            approval.write_text(
                json.dumps(
                    {
                        "schema_version": ML_APPROVAL_SCHEMA_VERSION,
                        "status": "approved",
                        "network_sha256": file_sha256(network),
                        "zone_sha256": file_sha256(zone),
                        "model_artifact_sha256": [file_sha256(model)],
                        "control_config_sha256": canonical_sha256(control),
                    }
                ),
                encoding="utf-8",
            )

            validate_queue_control_approval(
                approval,
                (model,),
                network,
                zone,
                control,
            )
            with self.assertRaisesRegex(ValueError, "control_config_sha256"):
                validate_queue_control_approval(
                    approval,
                    (model,),
                    network,
                    zone,
                    ControlConfig(zone_coordination_weight=3.0),
                )


if __name__ == "__main__":
    unittest.main()
