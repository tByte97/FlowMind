from __future__ import annotations

import unittest

from flowmind.counterfactual_dataset import (
    COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
    counterfactual_dataset_schema_sha256,
)
from flowmind.ml_dataset import ml_dataset_schema_sha256


class CounterfactualDatasetContractTest(unittest.TestCase):
    def test_contract_is_distinct_from_observational_dataset(self) -> None:
        self.assertEqual(COUNTERFACTUAL_DATASET_SCHEMA_VERSION, 5)
        self.assertEqual(len(counterfactual_dataset_schema_sha256()), 64)
        self.assertNotEqual(
            counterfactual_dataset_schema_sha256(),
            ml_dataset_schema_sha256(),
        )


if __name__ == "__main__":
    unittest.main()
