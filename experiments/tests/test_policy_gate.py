import unittest

from policy_gate import PolicyGate


class PolicyTests(unittest.TestCase):
    def test_violation_then_recovery_is_retained_and_charged(self):
        gate = PolicyGate("D", "concept_first", max_calls=2)
        sent = []
        def forward(p):
            sent.append(p)
            return {"content":[]}
        response, first = gate.call({"name":"search_exact"}, forward)
        self.assertTrue(response["isError"])
        self.assertFalse(sent)
        _, recovered = gate.call({"name":"search_concept"}, forward)
        self.assertEqual(recovered["first_attempt"], "search_exact")
        self.assertEqual(recovered["first_forwarded"], "search_concept")
        self.assertEqual(recovered["policy_violations"], 1)
        _, exhausted = gate.call({"name":"read_source"}, forward)
        self.assertEqual(exhausted["reason"], "budget_exhausted")
        self.assertEqual(len(sent), 1)

    def test_bad_arguments_do_not_mean_policy_disobedience(self):
        gate = PolicyGate("D", "lexical_first")
        _, e = gate.call({"name":"search_exact"}, lambda p:{"isError":True})
        self.assertTrue(e["tool_error"])
        self.assertEqual(e["policy_violations"], 0)
        _, e = gate.call({"name":"read_source"}, lambda p:{})
        self.assertTrue(e["forwarded"])

    def test_profile_and_response_limits(self):
        with self.assertRaises(ValueError):
            PolicyGate("A", "concept_first")
        gate = PolicyGate("A", "free", max_bytes=1000)
        _, e = gate.call({"name":"find_symbol"}, lambda p:self.fail("must not forward"))
        self.assertEqual(e["reason"], "unavailable_tool")
        response, e = gate.call({"name":"search_exact"}, lambda p:{"data":"x" * 2000})
        self.assertTrue(response["isError"])
        self.assertEqual(e["reason"], "response_budget_exhausted")
        _, e = gate.call({"name":"read_source"}, lambda p:self.fail("must not forward"))
        self.assertEqual(e["reason"], "budget_exhausted")
