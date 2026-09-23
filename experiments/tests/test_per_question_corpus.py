"""Per-question corpora and the oracle arm.

A suite has always shared one corpus per arm. LOC-BENCH pins a `base_commit` per instance, so a
question carries its own tree. The properties asserted here are the ones whose failure is silent:
a single-corpus run must be unchanged, a trial must search *its* question's tree, the grader's
definition index must follow the tree rather than whichever one came first, and the oracle seed
must hand over the file without handing over the answer.
"""
import json
from pathlib import Path
import tempfile
import unittest

import comparison_runner


def task(identifier, answer, corpus=None):
    entry = {"id": identifier, "category": "c", "set": "holdout", "question": "q?",
             "expected_json": {"answer": answer}, "rejected_alternates": ["a/b.py::x"],
             "evidence": [{"path": "a/b.py", "contains": "def"}], "author_notes": "n"}
    if corpus:
        entry["corpus"] = corpus
    return entry


SYSTEMS = [{"id": "native", "prompt_policy": "", "mcp_enabled": False},
           {"id": "oracle", "prompt_policy": "", "mcp_enabled": False, "oracle": True}]


class CorpusRoot(unittest.TestCase):
    def test_a_single_corpus_run_is_unchanged(self):
        roots = {"native": "/corpora/one"}
        self.assertEqual(comparison_runner.corpus_root(roots, "native", "anything"),
                         "/corpora/one")

    def test_a_pinned_question_wins_over_the_arm_default(self):
        roots = {"native": "/corpora/one", ("native", "q2"): "/corpora/q2"}
        self.assertEqual(comparison_runner.corpus_root(roots, "native", "q2"), "/corpora/q2")
        self.assertEqual(comparison_runner.corpus_root(roots, "native", "q1"), "/corpora/one")

    def test_the_plan_gives_each_trial_its_own_root(self):
        roots = {"native": "/d", "oracle": "/d",
                 ("native", "q1"): "/c/q1", ("native", "q2"): "/c/q2",
                 ("oracle", "q1"): "/c/q1", ("oracle", "q2"): "/c/q2"}
        plan = comparison_runner.make_plan(
            [task("q1", "a/b.py::f"), task("q2", "a/b.py::g")], SYSTEMS, roots, 1, 1)
        seen = {(t["task_id"], t["system"]): t["prompt"] for t in plan["trials"]}
        self.assertIn("/c/q1", seen[("q1", "native")])
        self.assertIn("/c/q2", seen[("q2", "native")])
        self.assertNotIn("/c/q2", seen[("q1", "native")])


class OracleArm(unittest.TestCase):
    def plan(self, answer):
        roots = {"native": "/d", "oracle": "/d"}
        plan = comparison_runner.make_plan([task("q1", answer)], SYSTEMS, roots, 1, 1)
        return {t["system"]: t["prompt"] for t in plan["trials"]}

    def test_the_oracle_is_told_the_file_and_not_the_symbol(self):
        prompts = self.plan("django/db/models/query.py::fast_delete")
        self.assertIn("django/db/models/query.py", prompts["oracle"])
        # Handing over the symbol would be handing over the answer.
        self.assertNotIn("fast_delete", prompts["oracle"])

    def test_every_other_arm_is_untouched(self):
        prompts = self.plan("django/db/models/query.py::fast_delete")
        self.assertNotIn("you have been told", prompts["native"].lower())
        self.assertNotIn("django/db/models/query.py", prompts["native"])

    def test_a_multi_file_gold_seeds_every_file_once(self):
        prompts = self.plan(["a/one.py::f", "b/two.py::g", "a/one.py::h"])
        seed = prompts["oracle"]
        self.assertIn("a/one.py", seed)
        self.assertIn("b/two.py", seed)
        self.assertEqual(seed.count("a/one.py"), 1)

    def test_a_gold_the_oracle_cannot_seed_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            self.plan(None)


class AgentEnvironment(unittest.TestCase):
    """An arm may need the client configured differently, not just the server.

    `--agent-command` is one string for the whole run, so without a per-arm hook there is no way
    to give one arm a shell and deny it to another - and on Codex that is the difference between
    measuring MCP against shell and measuring shell-plus-MCP against shell.
    """

    def systems(self, extra):
        base = {"id": "arm", "mcp_enabled": False, "upstreams": [], "environment": {},
                "prompt_policy": "", "prepare_commands": [], "check_commands": [],
                "version_command": None}
        base.update(extra)
        other = dict(base, id="other")
        other.pop("agent_environment", None)
        # load_systems requires a comparison, not a single arm.
        return {"version": "comparison-systems-v2", "systems": [base, other]}

    def load(self, document):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "systems.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return comparison_runner.load_systems(path)

    def test_the_field_is_accepted(self):
        loaded = self.load(self.systems({"agent_environment": {"CODEX_DISABLE_SHELL": "1"}}))
        self.assertEqual(loaded["systems"][0]["agent_environment"],
                         {"CODEX_DISABLE_SHELL": "1"})

    def test_an_arm_without_it_still_loads(self):
        self.assertNotIn("agent_environment", self.load(self.systems({}))["systems"][0])

    def test_an_unknown_field_is_still_refused(self):
        with self.assertRaises(ValueError):
            self.load(self.systems({"agent_enviroment": {"X": "1"}}))


class QuestionCorpora(unittest.TestCase):
    def write(self, directory, tasks):
        path = Path(directory) / "suite.json"
        path.write_text(json.dumps(tasks), encoding="utf-8")
        return path

    def test_a_suite_without_pinned_corpora_reports_none(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write(raw, [task("q1", "a/b.py::f")])
            self.assertEqual(comparison_runner.question_corpora_for(path), {})

    def test_pinned_corpora_are_collected_by_question(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write(raw, [task("q1", "a/b.py::f", corpus="/c/q1"),
                                    task("q2", "a/b.py::g", corpus="/c/q2")])
            self.assertEqual(comparison_runner.question_corpora_for(path),
                             {"q1": "/c/q1", "q2": "/c/q2"})

    def test_a_half_pinned_suite_is_refused(self):
        """Arms would search different trees for a reason the record could not state."""
        with tempfile.TemporaryDirectory() as raw:
            path = self.write(raw, [task("q1", "a/b.py::f", corpus="/c/q1"),
                                    task("q2", "a/b.py::g")])
            with self.assertRaises(ValueError):
                comparison_runner.question_corpora_for(path)


class LinkTree(unittest.TestCase):
    def test_the_copy_shares_inodes_and_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "src"
            (source / "pkg").mkdir(parents=True)
            original = source / "pkg" / "a.py"
            original.write_text("def f():\n    return 1\n", encoding="utf-8")
            target = root / "linked"
            comparison_runner.link_tree(source, target, comparison_runner.ignore_root_state(source))
            copied = target / "pkg" / "a.py"
            self.assertEqual(copied.read_text(), "def f():\n    return 1\n")
            self.assertEqual(copied.stat().st_ino, original.stat().st_ino)
            with self.assertRaises(PermissionError):
                copied.write_text("tampered", encoding="utf-8")
            # Leave nothing read-only behind for the temporary directory to trip over.
            for path in sorted(target.rglob("*"), reverse=True):
                path.chmod(0o700)
            target.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
