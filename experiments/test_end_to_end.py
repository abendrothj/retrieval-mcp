import json
import tempfile
import unittest
from pathlib import Path

import end_to_end
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


CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "mcp_servers": [{"name": "retrieval", "status": "connected"}]},
    {"type": "assistant", "message": {"id": "m1", "content": [{"type": "thinking"}],
                                      "usage": {"input_tokens": 3, "cache_creation_input_tokens": 100,
                                                "cache_read_input_tokens": 0, "output_tokens": 7}}},
    {"type": "assistant", "message": {
        "id": "m1",
        "content": [{"type": "tool_use", "id": "t1", "name": "mcp__retrieval__read_source"}],
        "usage": {"input_tokens": 3, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 0, "output_tokens": 7}}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                              "content": "x" * 4000}]}},
    {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text"}],
                                      "usage": {"input_tokens": 1, "cache_creation_input_tokens": 40,
                                                "cache_read_input_tokens": 103, "output_tokens": 9}}},
    {"type": "result", "subtype": "success", "total_cost_usd": 0.06,
     "usage": {"output_tokens_details": {"thinking_tokens": 38}}},
]


class ClaudeStreamTests(unittest.TestCase):
    """A Claude trial has no side-channel event file; without this the payload metrics read zero."""

    def trial(self, directory, events):
        path = Path(directory) / "transcript.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return Path(directory)

    def test_tool_results_are_attributed_to_the_tool_that_requested_them(self):
        with tempfile.TemporaryDirectory() as directory:
            stream, client = end_to_end.events(self.trial(directory, CLAUDE_STREAM))
        self.assertEqual(client, "claude")
        records = end_to_end.calls_from(stream, client)
        self.assertEqual([(record["name"], record["read"]) for record in records],
                         [("read_source", True)])
        self.assertEqual(context_token_turns(records), 1000)

    def test_repeated_message_usage_is_counted_once_and_includes_replayed_prefix(self):
        steps = end_to_end.steps_from(CLAUDE_STREAM, "claude")
        self.assertEqual([step["input"] for step in steps], [103, 144])
        self.assertEqual(sum(step["output"] for step in steps), 16)
        self.assertEqual(steps[-1]["reasoning"], 38)

    def test_a_codex_transcript_is_not_mistaken_for_a_claude_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = self.trial(directory, [{"type": "item.completed",
                                            "item": {"type": "agent_message", "text": "hi"}}])
            self.assertEqual(end_to_end.events(trial), ([], None))


class EvidenceSafetyTests(unittest.TestCase):
    """Closure is only a win when the evidence was present; this is the line that catches the rest."""

    TASK = {"expected_json": {"answer": ["pkg/a.py::alpha", "pkg/b.py::beta"]}}

    def test_an_identity_absent_from_every_result_is_flagged(self):
        seen = json.dumps({"results": [{"path": "pkg/a.py", "symbol": "alpha"}]})
        pairs = end_to_end.gold_identities(self.TASK)
        self.assertEqual(end_to_end.unretrieved(seen, pairs), [("pkg/b.py", "beta")])

    def test_a_fully_evidenced_answer_is_not_flagged(self):
        seen = json.dumps({"results": [{"path": "pkg/a.py", "symbol": "alpha"},
                                       {"path": "pkg/b.py", "symbol": "beta"}]})
        pairs = end_to_end.gold_identities(self.TASK)
        self.assertEqual(end_to_end.unretrieved(seen, pairs), [])

if __name__ == "__main__":
    unittest.main()
