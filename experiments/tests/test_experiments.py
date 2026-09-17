"""Offline analysis tests and an end-to-end harness check using a deterministic fake agent."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import analyze
import benchmark


def event(seq, tool, kind, arguments=None, locations=None, count=1, error=None):
    value = {"schema_version": 1, "session_id": "s", "sequence": seq, "tool": tool,
             "event": kind, "timestamp_ms": 100, "arguments": arguments or {}, "result_count": count,
             "error": error, "locations": locations or [], "latency_ms": 1, "response_bytes": 20,
             "retrieval_bytes": 10}
    if tool in {"find_symbol", "find_callers"}:
        value["coverage"] = {"complete": False}
    return value


class AnalysisTests(unittest.TestCase):
    def test_fallback_redundancy_and_verification_are_distinct(self):
        data = []
        specs = [("search_concept", {"query":"retry"}, [], 0),
                 ("search_exact", {"query":"delay"}, [{"path":"a.py","line":5}], 1),
                 ("find_symbol", {"name":"delay"}, [{"path":"a.py","line":5,"end_line":8}], 1),
                 ("read_source", {"path":"a.py","start_line":5,"end_line":8}, [{"path":"a.py","line":5,"end_line":8}], 4),
                 ("read_source", {"path":"./a.py","start_line":5,"end_line":8}, [{"path":"a.py","line":5,"end_line":8}], 4)]
        for seq,(tool,args,locations,count) in enumerate(specs,1):
            data.extend([event(seq,tool,"tool_start",args), event(seq,tool,"tool_end",args,locations,count)])
        result = analyze.analyze_events(data)["sessions"][0]
        self.assertEqual(result["first_tool"], "search_concept")
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(result["redundant_read_count"], 1)
        self.assertEqual(result["duplicate_request_count"], 1)
        self.assertEqual(result["advanced_followups"][0]["overlapping_source_verifications"], [4,5])
        self.assertEqual(result["advanced_followups"][0]["subsequent_read_source"], 2)

    def test_concurrent_reads_are_not_followups_even_with_equal_timestamps(self):
        data = [event(1,"find_symbol","tool_start"), event(2,"read_source","tool_start"),
                event(1,"find_symbol","tool_end", locations=[{"path":"a","line":1}]),
                event(2,"read_source","tool_end", locations=[{"path":"a","line":1}]),
                event(3,"search_exact","tool_start")]
        result = analyze.analyze_events(data)["sessions"][0]
        self.assertEqual(result["advanced_followups"][0]["subsequent_read_source"], 0)
        self.assertEqual(result["advanced_followups"][0]["overlapping_source_verifications"], [])
        self.assertEqual(result["incomplete_calls"], 1)

    def test_completed_order_is_not_first_selection_order(self):
        data = [event(1,"search_exact","tool_start"),event(2,"find_symbol","tool_start"),
                event(2,"find_symbol","tool_end"),event(1,"search_exact","tool_end",count=0)]
        result = analyze.analyze_events(data)["sessions"][0]
        self.assertEqual(result["first_tool"], "search_exact")
        self.assertEqual(result["fallback_count"], 0)

    def test_covered_ranges_need_full_union_not_any_overlap(self):
        self.assertTrue(analyze.covered(("a",1,10),[("a",1,5),("a",6,10)]))
        self.assertFalse(analyze.covered(("a",1,10),[("a",1,4),("a",6,10)]))
        self.assertFalse(analyze.covered(("b",1,10),[("a",1,10)]))

    def test_missing_end_duplicates_and_default_arguments(self):
        data = [event(1,"read_source","tool_start"),event(1,"read_source","tool_start")]
        result = analyze.analyze_events(data)
        self.assertTrue(result["warnings"])
        self.assertEqual(result["sessions"][0]["incomplete_calls"],1)
        self.assertEqual(analyze.normalized_arguments("read_source",{"path":"a"}),
                         analyze.normalized_arguments("read_source",{"path":"./a","start_line":1,"end_line":100}))


class HarnessTests(unittest.TestCase):
    def test_real_mcp_all_profiles_transcripts_and_analysis(self):
        server = Path(__file__).resolve().parents[2] / "target/debug/retrieval-mcp"
        self.assertTrue(server.exists(), "run cargo build --bin retrieval-mcp before these tests")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "repo"
            root.mkdir()
            (root / "policy.py").write_text("def delay(attempt):\n    return min(2 ** attempt, 32)\n")
            questions = directory / "questions.json"
            questions.write_text(json.dumps([{"id":"delay","question":"What is the delay cap? Answer an integer.","expected":"32"}]))
            args = SimpleNamespace(root=root, output=directory/"runs", questions=questions, server=server,
                model="test-only", client="command", agent_command=[sys.executable,str(Path(__file__).resolve().parents[1] / "fixture_backends.py"),"--fake-agent","{mcp_config}"],
                semantic_command=[sys.executable,str(Path(__file__).resolve().parents[1] / "fixture_backends.py"),"--fake-semantic"],
                profiles=list("ABCD"),repetitions=1,seed=42,timeout=15,tool_timeout=10,max_budget_usd=1,
                semantic_cache="warm",dry_run=False)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(benchmark.run(args),0)
            result = analyze.analyze_directory(args.output)
            self.assertEqual(len(result["runs"]),4)
            self.assertEqual(len(result["matched_comparisons"]),5)
            self.assertTrue(all(r["eligible_for_comparison"] for r in result["runs"]))
            for run in result["runs"]:
                self.assertTrue(run["metadata"]["correct"])
                self.assertEqual(sum(run["tool_counts"].values()),2)
                folder = args.output / run["metadata"]["run_id"]
                self.assertTrue((folder/"transcript.jsonl").exists())
                self.assertTrue((folder/"server.jsonl").exists())
            ab = next(c for c in result["matched_comparisons"] if c["baseline"] == "A" and c["variant"] == "B")
            self.assertEqual(ab["search_exact_saved"],1)
            # Failed or contaminated trials must not yield apparent retrieval savings.
            run_path = args.output / "delay-1-B/run.json"
            meta = json.loads(run_path.read_text()); meta["unexpected_tools"] = ["Bash"]
            benchmark.write_json(run_path,meta)
            ab = next(c for c in analyze.analyze_directory(args.output)["matched_comparisons"] if c["baseline"] == "A" and c["variant"] == "B")
            self.assertFalse(ab["eligible"]); self.assertIsNone(ab["search_exact_saved"])
            with self.assertRaises(FileExistsError):
                benchmark.run(args)
            args.output = directory/"dry"; args.dry_run = True
            self.assertEqual(benchmark.run(args),0)
            self.assertFalse(list(args.output.glob("*/transcript.jsonl")))

    def test_transcript_client_errors_and_unexpected_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"transcript.jsonl"
            path.write_text(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"}]}})+"\n"+
                            json.dumps({"type":"result","is_error":True,"subtype":"budget_exceeded","result":"partial"})+"\n")
            result = benchmark.transcript_outcome(path)
            self.assertEqual(result["unexpected_tools"],["Bash"])
            self.assertEqual(result["client_error"],"budget_exceeded")


if __name__ == "__main__":
    unittest.main()
