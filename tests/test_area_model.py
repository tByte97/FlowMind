from __future__ import annotations

import unittest

from flowmind.area_model import Intersection


class IntersectionTimingMetadataTest(unittest.TestCase):
    def test_phase_timing_accessors_keep_missing_bounds_explicit(self) -> None:
        intersection = Intersection(
            tls_id="I-01",
            position=(0.0, 0.0),
            phases=("G", "y"),
            links=(),
            phase_durations=(6.0, 3.0),
            program_id="0",
            program_type="actuated",
            phase_min_durations=(13.0, None),
            phase_max_durations=(50.0, None),
        )

        self.assertEqual(intersection.phase_min_duration(0), 13.0)
        self.assertEqual(intersection.phase_max_duration(0), 50.0)
        self.assertIsNone(intersection.phase_min_duration(1))
        self.assertIsNone(intersection.phase_max_duration(1))
        self.assertIsNone(intersection.phase_min_duration(99))

    def test_phase_metadata_length_must_match_phase_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "phase_min_durations has 1 values"):
            Intersection(
                tls_id="I-01",
                position=(0.0, 0.0),
                phases=("G", "y"),
                links=(),
                phase_min_durations=(10.0,),
            )


if __name__ == "__main__":
    unittest.main()
