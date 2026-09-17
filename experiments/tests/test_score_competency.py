import unittest

from score_competency import score


class ProbeScoringTests(unittest.TestCase):
    def test_correct_value_does_not_hide_wrong_method(self):
        task = {"id":"regex-alternation", "expected_json":{"answer":[1,2,3]}}
        call = {"name":"search_exact", "arguments":{"regex":False}, "result":{"structuredContent":{
            "results":[{"line":i,"path":"tokens.py"} for i in [1,2,3]]}}}
        self.assertFalse(score(task, '{"answer":[1,2,3]}', [call])["correct"])
        call["arguments"]["regex"] = True
        self.assertTrue(score(task, '{"answer":[1,2,3]}', [call])["correct"])
        self.assertFalse(score(task, '```json\n{"answer":[1,2,3]}\n```', [call])["correct"])

    def test_no_calls_cannot_pass_a_range_constraint(self):
        task = {"id":"bounded-read", "expected_json":{"answer":"x"}}
        self.assertFalse(score(task, '{"answer":"x"}', [])["correct"])
        task = {"id":"paginate-entries", "expected_json":{"answer":[1]}}
        call = {"name":"search_exact", "arguments":{"limit":20}, "result":{"structuredContent":{
            "results":[{"line":1,"path":"tokens.py"}]}}}
        self.assertFalse(score(task, '{"answer":[1]}', [call])["correct"])
