import unittest

from end_to_end import context_token_turns


class ContextPersistenceTests(unittest.TestCase):
    def record(self, body):
        return {"name": "read_source", "body": body, "read": True, "latency_ms": None}

    def test_an_early_payload_is_paid_for_by_every_later_turn(self):
        """Equal bytes are not equal cost: the earlier one is re-sent more often."""
        early = [self.record("x" * 4000), self.record(""), self.record("")]
        late = [self.record(""), self.record(""), self.record("x" * 4000)]

        self.assertEqual(context_token_turns(early), 3000)
        self.assertEqual(context_token_turns(late), 1000)

    def test_one_call_is_charged_once_and_no_calls_cost_nothing(self):
        self.assertEqual(context_token_turns([self.record("x" * 4000)]), 1000)
        self.assertEqual(context_token_turns([]), 0)

    def test_a_shorter_conversation_carries_the_same_payload_less_far(self):
        """The reason turns dominate: cutting a trailing call also discounts every earlier row."""
        payload = self.record("x" * 4000)
        self.assertEqual(context_token_turns([payload, self.record("y" * 400)]), 2100)
        self.assertEqual(context_token_turns([payload]), 1000)


if __name__ == "__main__":
    unittest.main()
