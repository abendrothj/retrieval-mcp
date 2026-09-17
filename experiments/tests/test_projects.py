"""Committed-snapshot isolation and project-suite preflight checks (no model calls)."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from analyze_projects import report
import benchmark
from prepare_stratified import CATEGORIES
from snapshot_projects import snapshot


class ProjectTests(unittest.TestCase):
    def test_snapshot_uses_commit_not_worktree_and_excludes_non_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base/"repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo/"a.py").write_text("def original():\n    return 1\n")
            (repo/".env").write_text("NOT_A_REAL_SECRET=fixture\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.py", ".env"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)
            (repo/"a.py").write_text("def modified():\n    return 2\n")
            (repo/"untracked.py").write_text("do_not_export = True\n")
            manifest = snapshot(repo, base/"snapshot")
            self.assertEqual(manifest["source_files"], 1)
            self.assertEqual((base/"snapshot/corpus/a.py").read_text(), "def original():\n    return 1\n")
            self.assertFalse((base/"snapshot/corpus/.env").exists())
            self.assertFalse((base/"snapshot/corpus/untracked.py").exists())
            self.assertIn("modified", (repo/"a.py").read_text())
            with self.assertRaises(FileExistsError):
                snapshot(repo, base/"snapshot")

    def test_project_tasks_are_balanced_and_gold_values_are_well_formed(self):
        suite = json.loads((Path(__file__).resolve().parents[1] / "suites/project_questions.json").read_text())
        ids = set()
        for project, tasks in suite["projects"].items():
            self.assertEqual(len(suite["revisions"][project]), 40)
            self.assertEqual(Counter(t["category"] for t in tasks), dict.fromkeys(CATEGORIES, 2))
            for task in tasks:
                self.assertNotIn(task["id"], ids)
                ids.add(task["id"])
                self.assertTrue(task["evidence"])
                self.assertTrue(benchmark.grade_answer(task, json.dumps(task["expected_json"]))["correct"])
                self.assertEqual(task["repository_id"], project)
        self.assertEqual(len(ids), 36)

    def test_report_excludes_unfinished_trials_and_separate_pilot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = {"task_id":"lookup", "trial":1, "model":"test", "client":"claude",
                        "repository":{"sha256":"snapshot"}, "prompt_sha256":"prompt",
                        "category":"exact_lookup", "profile":"A", "status":"completed",
                        "repository_unchanged":True, "correct":True, "format_correct":False,
                        "wall_time_ms":2000}
            for name, changes in [("done", {}), ("pending", {"trial":2, "status":"running"})]:
                trial = root/"modelshare"/name
                trial.mkdir(parents=True)
                benchmark.write_json(trial/"run.json", {**metadata, **changes})
                (trial/"server.jsonl").write_text("")
            (root/"modelshare/done/transcript.jsonl").write_text(
                json.dumps({"type":"system", "subtype":"init", "model":"test",
                            "tools":["mcp__retrieval__search_exact"], "skills":[], "plugins":[]}) + "\n" +
                json.dumps({"type":"result", "total_cost_usd":0.25}) + "\n")
            pilot = root/"pilot"/"trial"
            pilot.mkdir(parents=True)
            benchmark.write_json(pilot/"run.json", metadata)
            (pilot/"transcript.jsonl").write_text(json.dumps({"type":"result", "total_cost_usd":100}) + "\n")
            result = report(root)
            totals = next(p for p in result["profiles"] if p["repository"] == "all" and p["profile"] == "A")
            self.assertEqual(totals["observed_trials"], 2)
            self.assertEqual(totals["eligible_trials"], 1)
            self.assertEqual(totals["correct"], 1)
            self.assertEqual(totals["format_correct"], 0)
            self.assertEqual(totals["mean_wall_seconds"], 2)
            self.assertEqual(result["model_sessions"], {"test":1})
            self.assertEqual(result["client_reported_cost_usd"], 0.25)
            self.assertEqual(result["initialization_contamination"], [])


if __name__ == "__main__":
    unittest.main()
