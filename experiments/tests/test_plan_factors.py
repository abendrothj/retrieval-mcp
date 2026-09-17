from collections import Counter
import unittest

from plan_factors import plan


class FactorPlanTests(unittest.TestCase):
    def test_balanced_and_one_factor_contrasts(self):
        result = plan([{"id":"q", "question":"Where is the implementation?", "expected_json":{"answer":"SECRET_GOLD"}}], 2)
        cells = {c["id"]:c for c in result["conditions"]}
        self.assertEqual(len(cells), 12)
        self.assertEqual(len(result["trials"]), 24)
        self.assertEqual(Counter(t["condition"] for t in result["trials"]), dict.fromkeys(cells, 2))
        for contrast in result["contrasts"]:
            a, b = cells[contrast["baseline"]], cells[contrast["variant"]]
            self.assertEqual([f for f in ("availability", "routing", "syntax_help") if a[f] != b[f]], [contrast["factor"]])
        for trial in result["trials"]:
            self.assertNotIn("SECRET_GOLD", trial["prompt"])
            c = cells[trial["condition"]]
            if c["routing"] != "free":
                self.assertEqual(c["availability"], "D")
                self.assertNotIn("Choose the retrieval methods yourself.", trial["prompt"])

    def test_deterministic_and_invalid_inputs(self):
        tasks = [{"id":"q", "question":"Q"}]
        self.assertEqual(plan(tasks), plan(tasks))
        for invalid, reps in (([], 1), (tasks * 2, 1), (tasks, 0)):
            with self.assertRaises(ValueError):
                plan(invalid, reps)
