"""Offline checks for the frozen suite, grading, category reports, and routing sequences."""
from collections import Counter
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import analyze
import benchmark
import prepare_stratified
from test_experiments import event

ROOT = Path(__file__).resolve().parents[1]
SUITE = Path(__file__).with_name("stratified.json")


class GradingTests(unittest.TestCase):
    def test_content_and_format_are_separate(self):
        task = {"expected_json": {"answer": "symbol"}}
        self.assertTrue(benchmark.grade_answer(task, '{"answer":"symbol"}')["correct"])
        fenced = benchmark.grade_answer(task, '```json\n{"answer":"symbol"}\n```')
        self.assertTrue(fenced["correct"])
        self.assertFalse(fenced["format_correct"])
        with_prose = benchmark.grade_answer(task, 'Explanation.\n```json\n{"answer":"symbol"}\n```')
        self.assertTrue(with_prose["correct"])
        self.assertFalse(with_prose["format_correct"])
        self.assertEqual(with_prose["grading"], "json-answer-v2")
        self.assertFalse(benchmark.grade_answer(task, '```json\n{"answer":"symbol"}\n```\n```json\n{"answer":"wrong"}\n```')["correct"])
        for answer in ['The answer is symbol.', '{"answer":"wrong"}',
                       '{"answer":"wrong","answer":"symbol"}',
                       '{"answer":"symbol"}\n{"answer":"wrong"}',
                       '{"answer":"symbol","extra":true}']:
            self.assertFalse(benchmark.grade_answer(task, answer)["correct"])
        self.assertFalse(benchmark.grade_answer({"expected_json":{"answer":1}}, '{"answer":true}')["correct"])
        self.assertFalse(benchmark.grade_answer({"expected_json":{"answer":1}}, '{"answer":NaN}')["correct"])
        self.assertFalse(benchmark.grade_answer({"expected":"symbol"}, '`symbol`')["correct"])

    def test_sets_do_not_allow_duplicates_or_reorder_chains(self):
        task = {"expected_json":{"answer":["a","b"]}, "answer_set":True}
        self.assertTrue(benchmark.grade_answer(task, '{"answer":["b","a"]}')["correct"])
        self.assertFalse(benchmark.grade_answer(task, '{"answer":["a","a","b"]}')["correct"])
        task.pop("answer_set")
        self.assertFalse(benchmark.grade_answer(task, '{"answer":["b","a"]}')["correct"])


class SuiteTests(unittest.TestCase):
    def test_pinned_balanced_suite_and_full_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manifest = prepare_stratified.prepare(ROOT, base/"suite", SUITE)
            self.assertEqual(manifest["categories"], dict.fromkeys(prepare_stratified.CATEGORIES, 4))
            self.assertGreater(manifest["source_lines"], 2000)
            corpus = base/"suite/corpus"
            self.assertEqual(set(manifest["corpus_files"]),
                             {str(p.relative_to(corpus)) for p in corpus.rglob("*") if p.is_file()})
            tasks = json.loads((base/"suite/questions.json").read_text())
            self.assertEqual(len(tasks), 24)
            args = SimpleNamespace(root=corpus, output=base/"plan", questions=base/"suite/questions.json",
                server=ROOT/"target/debug/retrieval-mcp", model="test-only", client="command", agent_command=["false"],
                semantic_command=["false"], profiles=list("ABCD"), repetitions=3, seed=42, timeout=10,
                tool_timeout=10, max_budget_usd=1, semantic_cache="warm", dry_run=True)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(benchmark.run(args), 0)
            schedule = json.loads((base/"plan/schedule.json").read_text())
            self.assertEqual(len(schedule), 288)
            self.assertEqual(Counter(t["category"] for t in schedule), dict.fromkeys(prepare_stratified.CATEGORIES, 48))
            self.assertFalse(list((base/"plan").glob("*/transcript.jsonl")))
            for task in tasks:
                prompts = [(base/"plan"/f"{task['id']}-1-{p}"/"prompt.txt").read_text() for p in "ABCD"]
                self.assertTrue(all(p == prompts[0] for p in prompts))
                self.assertNotIn("expected_json", prompts[0])
                self.assertNotIn(task["category"], prompts[0])
            with self.assertRaises(FileExistsError):
                prepare_stratified.prepare(ROOT, base/"suite", SUITE)
            # A changed corpus invalidates the gold key before any model is launched.
            (corpus/"src/lib.rs").write_text("// changed\n")
            args.output = base/"changed-plan"
            with self.assertRaisesRegex(ValueError, "different corpus"):
                benchmark.run(args)
            self.assertFalse(args.output.exists())
            with self.assertRaisesRegex(ValueError, "corpus changed"):
                prepare_stratified.prepare(corpus, base/"new-suite", SUITE)
            self.assertFalse((base/"new-suite").exists())

    def test_subscription_keeps_isolated_profile_tool_list(self):
        args = SimpleNamespace(client="claude", claude_auth="subscription", model="test", max_budget_usd=1)
        command = benchmark.agent_command(args, Path("/trial"), "B")
        self.assertNotIn("--bare", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("search_concept", command[command.index("--allowedTools") + 1])
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertTrue(settings["disableAllHooks"])
        self.assertFalse(settings["autoMemoryEnabled"])
        args.claude_auth = "api"
        self.assertIn("--bare", benchmark.agent_command(args, Path("/trial"), "B"))


class StratifiedAnalysisTests(unittest.TestCase):
    def test_semantic_before_structural_requires_response_before_request(self):
        location = [{"path":"a.rs","line":4,"end_line":8}]
        events = [event(1,"search_concept","tool_start"), event(2,"find_symbol","tool_start"),
                  event(1,"search_concept","tool_end",locations=location),
                  event(2,"find_symbol","tool_end",locations=location),
                  event(3,"find_callers","tool_start"), event(3,"find_callers","tool_end",locations=location)]
        metric = analyze.analyze_events(events)["sessions"][0]
        self.assertEqual(metric["semantic_to_structural"], [{"semantic_sequence":1, "structural_sequence":3,
                         "structural_tool":"find_callers", "overlapping_evidence":True}])
        events[2]["result_count"] = 0
        self.assertEqual(analyze.analyze_events(events)["sessions"][0]["semantic_to_structural"], [])

    def test_categories_correctness_and_failed_trials_stay_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for category in ("exact_lookup", "conceptual_lookup"):
                for profile in "AB":
                    directory = root/f"{category}-{profile}"
                    directory.mkdir()
                    metadata = {"task_id":category,"category":category,"trial":1,"profile":profile,
                        "model":"test","client":"test","repository":{"sha256":"same"},"prompt_sha256":category,
                        "status":"completed","repository_unchanged":True,"correct":profile == "A",
                        "format_correct":True,"wall_time_ms":10 if profile == "A" else 6}
                    if category == "conceptual_lookup" and profile == "B":
                        metadata["status"] = "failed"
                    benchmark.write_json(directory/"run.json",metadata)
                    events = [event(1,"search_exact","tool_start"),event(1,"search_exact","tool_end")]
                    (directory/"server.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            result = analyze.analyze_directory(root)
            self.assertEqual(len(result["category_profiles"]),4)
            exact = next(c for c in result["category_comparisons"] if c["category"] == "exact_lookup")
            self.assertEqual(exact["eligible_pairs"],1)
            self.assertEqual(exact["both_correct_pairs"],0)
            self.assertEqual(exact["mean_saved"]["wall_time_ms_saved"],4)
            self.assertIsNone(exact["both_correct_mean_saved"]["wall_time_ms_saved"])
            failed = next(c for c in result["category_profiles"] if c["category"] == "conceptual_lookup" and c["profile"] == "B")
            self.assertEqual(failed["excluded_trials"],1)
            self.assertIsNone(failed["accuracy"])


if __name__ == "__main__":
    unittest.main()
