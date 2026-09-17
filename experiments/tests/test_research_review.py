import unittest

from research_review import summarize


class ResearchReviewTests(unittest.TestCase):
    def test_pairs_repetitions_outliers_and_missing_metrics_stay_distinct(self):
        rows = [{"task_id":task, "total_calls_saved":saving, "both_payload_match":correct}
                for task,saving,correct in [("hard", 20, True), ("hard", 10, False), ("easy", -1, True), ("easy", 0, True)]]
        result = summarize(rows)
        self.assertEqual(result["all_eligible"]["pairs"], 4)
        self.assertEqual(result["all_eligible"]["questions"], 2)
        self.assertEqual(result["all_eligible"]["saved"]["total_calls_saved"]["median"], 5)
        self.assertEqual(result["both_payload_match"]["pairs"], 3)
        self.assertEqual(result["both_payload_match"]["saved"]["total_calls_saved"]["median"], 0)
        self.assertEqual(result["all_eligible"]["saved"]["input_tokens_saved"]["n"], 0)
        self.assertEqual(result["leave_one_question_out_calls_saved"]["hard"]["mean"], -0.5)
        self.assertEqual(result["all_eligible"]["fewer_calls"], 2)
        self.assertEqual(result["all_eligible"]["more_calls"], 1)

    def test_empty_subset_is_not_zero_effect(self):
        result = summarize([])
        self.assertIsNone(result["both_payload_match"]["saved"]["total_calls_saved"]["mean"])
        self.assertEqual(result["leave_one_question_out_calls_saved"], {})
