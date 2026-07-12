from __future__ import annotations

import unittest

from experiments.run_evaluation import emergency_route_edges, validate_replicate_count


class EvaluationRunnerTests(unittest.TestCase):
    def test_full_run_requires_30_to_50_replicates(self) -> None:
        validate_replicate_count(30, False)
        validate_replicate_count(50, False)
        with self.assertRaises(ValueError):
            validate_replicate_count(29, False)
        with self.assertRaises(ValueError):
            validate_replicate_count(51, False)

    def test_smoke_profile_allows_small_positive_run(self) -> None:
        validate_replicate_count(1, True)
        with self.assertRaises(ValueError):
            validate_replicate_count(0, True)

    def test_emergency_route_is_parsed_from_summary(self) -> None:
        self.assertEqual(
            emergency_route_edges({"emergency_route_edges": "edge-a edge-b"}),
            ("edge-a", "edge-b"),
        )
        self.assertEqual(emergency_route_edges({}), ())


if __name__ == "__main__":
    unittest.main()
