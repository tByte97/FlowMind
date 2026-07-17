from __future__ import annotations

import unittest

from experiments.run_ablation import ablation_variants


class RunAblationTests(unittest.TestCase):
    def test_builds_five_distinct_mask_policies(self) -> None:
        variants = ablation_variants()
        self.assertEqual(len(variants), 5)
        self.assertFalse(variants["graph_penalty_only"].physical_hard_mask_enabled)
        self.assertFalse(variants["physical_confirmed_only"].graph_hard_mask_enabled)
        self.assertEqual(
            variants["graph_hysteresis_3_samples"].graph_hard_mask_confirmation_samples,
            3,
        )
        self.assertTrue(variants["per_tls_local_fallback"].throughput_fallback_enabled)
        self.assertEqual(
            variants["legacy_aggressive_control"].max_graph_masked_movement_share,
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
