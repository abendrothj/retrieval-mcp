import argparse
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


class BucketReportTests(unittest.TestCase):
    """A run is read bucket by bucket: an arm can win overall and lose a category.

    The whole point of mixing question shapes is that the aggregate hides them, so the split is
    pinned against hand-computed values, and the arm-wide numbers are pinned again to prove the
    addition changed nothing above it.
    """

    QUESTIONS = [
        {"id": "q-vague-1", "category": "vague_conceptual",
         "expected_json": {"answer": "pkg/a.py::alpha"}},
        {"id": "q-vague-2", "category": "vague_conceptual",
         "expected_json": {"answer": "pkg/b.py::beta"}},
        {"id": "q-exact-1", "category": "exact_control",
         "expected_json": {"answer": "pkg/a.py::alpha"}},
        {"id": "q-exact-2", "category": "exact_control",
         "expected_json": {"answer": "pkg/a.py::alpha"}},
        {"id": "q-exact-3", "category": "exact_control",
         "expected_json": {"answer": "pkg/a.py::alpha"}},
    ]
    # task_id, input tokens, resolved, tool payload. q-vague-2 answers while its gold identity
    # was never on screen, and is also graded wrong: one bucket carries both safety counts.
    TRIALS = [
        ("q-vague-1", 1000, True, "pkg/a.py: def alpha()"),
        ("q-vague-2", 3000, False, "pkg/z.py: def zeta()"),
        ("q-exact-1", 100, True, "pkg/a.py: def alpha()"),
        ("q-exact-2", 200, True, "pkg/a.py: def alpha()"),
        # Skewed on purpose: this bucket's median (200) and mean (400) differ, so a bucket that
        # quietly reports one statistic under the other name is caught.
        ("q-exact-3", 900, True, "pkg/a.py: def alpha()"),
    ]

    def build(self, directory):
        root = Path(directory)
        questions = root / "questions.json"
        questions.write_text(json.dumps(self.QUESTIONS), encoding="utf-8")
        run = root / "run"
        run.mkdir()
        for position, (task_id, tokens, resolved, body) in enumerate(self.TRIALS):
            trial = run / f"trial-{position:04d}"
            trial.mkdir()
            (trial / "run.json").write_text(json.dumps({
                "system": "arm", "task_id": task_id, "repetition": 1, "status": "completed",
                "correct": resolved, "resolved_correct": resolved,
                "resolved_credit": 1.0 if resolved else 0.0, "budget_exhausted": False,
                "wall_time_ms": 1000.0, "repository_unchanged": True,
            }), encoding="utf-8")
            stream = [
                {"type": "system", "subtype": "init"},
                {"type": "assistant", "message": {
                    "id": "m1",
                    "content": [{"type": "tool_use", "id": "u1",
                                 "name": "mcp__retrieval__read_source"}],
                    "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 5}}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "u1", "content": body}]}},
                {"type": "result", "subtype": "success", "total_cost_usd": 0.0},
            ]
            (trial / "transcript.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in stream), encoding="utf-8")
        return argparse.Namespace(run=run, questions=questions)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.summary = end_to_end.report(self.build(self.directory.name))["systems"]["arm"]

    def test_the_arm_wide_numbers_are_what_they_were_before_buckets_existed(self):
        self.assertEqual(self.summary["trials"], 5)
        self.assertEqual(self.summary["resolved_correct"], 4)
        self.assertEqual(self.summary["resolved_credit_mean"], 0.8)
        # median(100, 200, 900, 1000, 3000) = 900, mean = 1040: the aggregate buckets must not move.
        self.assertEqual(self.summary["input_tokens"], {"median": 900, "mean": 1040.0, "n": 5})
        self.assertEqual(self.summary["answered_without_evidence"], 1)
        self.assertEqual(self.summary["wrong_without_evidence"], 1)
        self.assertEqual(set(self.summary) - set(end_to_end.measures([])), {"by_category"})

    def test_each_bucket_reports_its_own_questions_correctness_and_tokens(self):
        buckets = self.summary["by_category"]
        self.assertEqual(sorted(buckets), ["exact_control", "vague_conceptual"])
        vague, exact = buckets["vague_conceptual"], buckets["exact_control"]
        self.assertEqual((vague["questions"], vague["trials"]), (2, 2))
        self.assertEqual((exact["questions"], exact["trials"]), (3, 3))
        self.assertEqual(vague["resolved_correct"], 1)
        self.assertEqual(exact["resolved_correct"], 3)
        self.assertEqual(vague["resolved_credit_mean"], 0.5)
        self.assertEqual(exact["resolved_credit_mean"], 1.0)
        # Hand-computed: (1000, 3000) against (100, 200, 900). Pooling the arm gives 900/1040, and
        # reporting the bucket mean as its median gives 400 where the median is 200.
        self.assertEqual(vague["input_tokens"], {"median": 2000.0, "mean": 2000.0, "n": 2})
        self.assertEqual(exact["input_tokens"], {"median": 200, "mean": 400.0, "n": 3})

    def test_the_safety_columns_are_charged_to_the_bucket_that_earned_them(self):
        """An efficiency change that answers early in one shape must be visible in that shape."""
        buckets = self.summary["by_category"]
        self.assertEqual((buckets["vague_conceptual"]["answered_without_evidence"],
                          buckets["vague_conceptual"]["wrong_without_evidence"]), (1, 1))
        self.assertEqual((buckets["exact_control"]["answered_without_evidence"],
                          buckets["exact_control"]["wrong_without_evidence"]), (0, 0))

    def test_a_bucket_reports_every_measure_the_arm_reports(self):
        """A bucket with fewer columns than the arm silently drops the one being argued about."""
        for bucket in self.summary["by_category"].values():
            self.assertEqual(set(bucket) - {"questions"}, set(end_to_end.measures([])))



class EvidenceIncludesTheRequestTests(unittest.TestCase):
    """A shell command names the file it pages; ripgrep given one file does not repeat the path."""

    def setUp(self):
        self.task = {"id": "t", "category": "symbol_resolution",
                     "expected_json": {"answer": "kernel/time/timer.c::add_timer_local"}}

    def test_a_path_named_only_in_the_command_counts_as_evidence(self):
        records = end_to_end.calls_from([
            {"type": "item.completed", "item": {
                "type": "command_execution",
                "command": "/bin/zsh -lc \"sed -n '1200,1260p' kernel/time/timer.c\"",
                "aggregated_output": "void add_timer_local(struct timer_list *timer)\n"}},
        ], "codex")
        self.assertEqual(records[0]["request"].count("kernel/time/timer.c"), 1)
        joined = records[0]["request"] + "\n" + records[0]["body"]
        self.assertIn("kernel/time/timer.c", joined)
        self.assertIn("add_timer_local", joined)

    def test_an_mcp_call_carries_its_arguments(self):
        records = end_to_end.calls_from([
            {"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "retrieval", "tool": "read_source",
                "arguments": {"path": "kernel/time/timer.c"}, "result": "void add_timer_local("}},
        ], "codex")
        self.assertIn("kernel/time/timer.c", records[0]["request"])


if __name__ == "__main__":
    unittest.main()
