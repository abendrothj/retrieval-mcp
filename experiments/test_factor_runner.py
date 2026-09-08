import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factor_runner import run
from analyze_factors import report


class FactorRunnerTests(unittest.TestCase):
    def test_no_model_process_without_explicit_gate(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaises(ValueError):
                run(SimpleNamespace(client="claude", allow_model_usage=False, model="test"))

    def test_real_matrix_resume_and_interrupted_attempt_preservation(self):
        script = Path(__file__).with_name("factor_runner.py")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/"matrix"
            command = [sys.executable, str(script), "--output", str(output)]
            def execute(extra=(), expected=0):
                result = subprocess.run([*command, *extra], text=True, capture_output=True, timeout=90)
                self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
                return result
            execute()
            summary = report(output)
            self.assertEqual(len(summary["conditions"]), 12)
            self.assertTrue(all(c["primary_passes"] == 1 for c in summary["conditions"]))
            paths = sorted(output.glob("trial-*/attempt-*/run.json"))
            self.assertEqual(len(paths), 12)
            for path in paths:
                record = json.loads(path.read_text())
                self.assertEqual(record["status"], "completed")
                self.assertTrue(record["correct"])
                self.assertTrue(record["policy_adherent"])
                self.assertEqual(record["attempted_calls"], 2)
                events = [json.loads(line) for line in (path.parent/"policy.jsonl").read_text().splitlines()]
                self.assertTrue(all(e["forwarded"] for e in events))
                self.assertTrue((path.parent/"server.jsonl").exists())
            frozen = {p:p.read_bytes() for p in paths}
            execute(["--resume"])
            self.assertEqual(frozen, {p:p.read_bytes() for p in paths})
            execute(["--resume", "--max-calls", "19"], expected=1)
            # Simulate process interruption using only this temporary test artifact.
            victim = paths[0]
            state = json.loads(victim.read_text())
            state["status"] = "running"
            victim.write_text(json.dumps(state))
            interrupted = victim.read_bytes()
            execute(["--resume"], expected=1)
            execute(["--resume", "--retry-failed"])
            self.assertEqual(victim.read_bytes(), interrupted)
            self.assertEqual(len(list(output.glob("trial-*/attempt-*/run.json"))), 13)
            summary = report(output)
            retried = [r for r in summary["trials"] if r["attempt_count"] == 2]
            self.assertEqual(len(retried), 1)
            self.assertFalse(retried[0]["eligible"])
            self.assertEqual(retried[0]["latest_status"], "completed")
            # An interruption before run.json exists is also an attempt, not invisible.
            other = paths[1]
            other.rename(other.with_name("interrupted-record.json"))
            execute(["--resume", "--retry-failed"])
            summary = report(output)
            incomplete = [r for r in summary["trials"] if r["first_attempt"]["status"] == "interrupted_before_record"]
            self.assertEqual(len(incomplete), 1)
            self.assertFalse(incomplete[0]["eligible"])
