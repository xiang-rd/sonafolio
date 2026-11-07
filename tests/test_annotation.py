"""Prevent confidently misattributing text that spans a speaker change."""

import unittest

from sonafolio.annotation import annotate_segments, interval_union_duration


class AnnotationTests(unittest.TestCase):
    def test_assigns_only_single_speaker_with_sufficient_coverage(self):
        segment = {"start": 1, "end": 3, "text": "回答", "speaker_id": None}
        result = annotate_segments([segment], [{"start": 0, "end": 4, "speaker_id": "SPEAKER_00"}])
        self.assertEqual(result[0]["speaker_id"], "SPEAKER_00")
        self.assertEqual(result[0]["role"], "unknown")

    def test_does_not_assign_majority_speaker_to_mixed_text(self):
        segment = {"start": 0, "end": 10, "text": "问题和回答"}
        turns = [{"start": 0, "end": 9.5, "speaker_id": "A"},
                 {"start": 9.5, "end": 10, "speaker_id": "B"}]
        result = annotate_segments([segment], turns)[0]
        self.assertIsNone(result["speaker_id"])
        self.assertEqual(result["speaker_candidates"], ["A", "B"])

    def test_does_not_double_count_overlapping_turns_of_one_speaker(self):
        self.assertEqual(interval_union_duration([(0, 2), (1, 3), (4, 5)]), 4)
        segment = {"start": 0, "end": 10, "text": "测试"}
        turns = [{"start": 0, "end": 4, "speaker_id": "A"},
                 {"start": 0, "end": 4, "speaker_id": "A"}]
        self.assertIsNone(annotate_segments([segment], turns)[0]["speaker_id"])

    def test_rejects_empty_intervals(self):
        with self.assertRaises(ValueError):
            annotate_segments([{"start": 1, "end": 1}], [])


if __name__ == "__main__":
    unittest.main()
