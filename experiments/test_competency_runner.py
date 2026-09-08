import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from competency_runner import run

SYNTAX = "literal-pipe,regex-alternation,regex-literal-pipe,paginate-entries,bounded-read"


def arguments(**overrides):
    base = SimpleNamespace(output=None, root=Path(__file__).with_name("competency_repo"),
        questions=Path(__file__).with_name("competency_questions.json"),
        server=Path(__file__).resolve().parent.parent/"target/debug/retrieval-mcp",
        profile="B", help_levels="baseline,primer", ids=SYNTAX, client="scripted",
        allow_model_usage=False, model=None, semantic_command=None, timeout=60, tool_timeout=10,
        max_calls=20, max_bytes=200000, max_budget_usd=1, seed=42)
    return SimpleNamespace(**{**vars(base), **overrides})


class CompetencyRunnerTests(unittest.TestCase):
    def test_no_model_process_without_explicit_gate(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("subprocess.Popen", side_effect=AssertionError("must not launch")):
            output = Path(temporary)/"probes"
            with self.assertRaises(ValueError):
                run(arguments(output=output, client="claude", model="test"))
            with self.assertRaises(ValueError):
                run(arguments(output=output, client="claude", model=None, allow_model_usage=True))
            # Structural probes need find_symbol/find_callers; profile A must not run them silently.
            with self.assertRaises(ValueError) as raised:
                run(arguments(output=output, profile="A", ids="duplicate-definition"))
            self.assertIn("unavailable in profile A", str(raised.exception))
            # Probes without a deterministic fixture require an authorized model trial.
            with self.assertRaises(ValueError) as raised:
                run(arguments(output=output, ids="transitive-chain"))
            self.assertIn("fixture agent", str(raised.exception))
            self.assertFalse(output.exists())

    def test_scripted_probes_score_against_real_mcp_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/"probes"
            summary = run(arguments(output=output))
            self.assertEqual(summary, {"version":"competency-summary-v1", "test_only":True,
                "planned":10, "completed":10, "failed":0, "probe_passes":10})
            records = [json.loads(p.read_text()) for p in sorted(output.glob("probe-*/run.json"))]
            self.assertEqual(sorted({r["task_id"] for r in records}), sorted(SYNTAX.split(",")))
            self.assertEqual({r["syntax_help"] for r in records}, {"baseline", "primer"})
            for record in records:
                self.assertEqual(record["status"], "completed")
                self.assertTrue(record["test_only"])
                self.assertTrue(record["scored"]["correct"], record)
                self.assertTrue(record["scored"]["behavior_constraint_satisfied"])
                self.assertGreater(record["attempted_calls"], 0)
                self.assertTrue(all(call["forwarded"] for call in record["calls"]))
                self.assertFalse(record["budget_exhausted"])
                self.assertTrue(record["repository_unchanged"])
            pages = [r for r in records if r["task_id"] == "paginate-entries"][0]
            self.assertEqual(pages["attempted_calls"], 3)
            # Delivered responses, not just call names, are what the behaviour constraint reads.
            captured = [json.loads(line) for path in sorted(output.glob("probe-*/responses.jsonl"))
                        for line in path.read_text().splitlines()]
            self.assertEqual(len(captured), sum(r["attempted_calls"] for r in records))
            self.assertTrue(all("result" in record for record in captured))
            # Primer text is the only prompt difference; the fixture agent ignores both prompts.
            self.assertEqual(len({r["prompt_sha256"] for r in records}), 10)
            self.assertTrue(all(r["fixture_ignores_prompt"] for r in records))

    def test_refuses_reused_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/"probes"
            run(arguments(output=output, ids="bounded-read", help_levels="baseline"))
            with self.assertRaises(FileExistsError):
                run(arguments(output=output, ids="bounded-read", help_levels="baseline"))

    def test_command_line_entry_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/"probes"
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("competency_runner.py")),
                "--output", str(output), "--ids", "regex-alternation", "--help-levels", "baseline"],
                text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["probe_passes"], 1)
            manifest = json.loads((output/"manifest.json").read_text())
            self.assertEqual(manifest["model"], "scripted-no-inference")
            self.assertEqual(manifest["grading"], "competency-strict-v1")


if __name__ == "__main__":
    unittest.main()
