"""Compiling LOC-BENCH into this project's schema.

The properties asserted here are the ones that silently produce an unusable suite: LOC-BENCH
spells a method `Class.method` while the grader indexes the leaf, the leak rule has to know the
difference between a Java class name and a generic Python module stem, and a function the patch
adds does not exist at the commit the agent searches.
"""
import json
from pathlib import Path
import tempfile
import unittest

import commit_suite
import locbench_suite

SOURCE = '''\
class Generator:
    def ordered_sql(self, expression):
        return "ORDER BY"

    def other_sql(self, expression):
        return ""


def unsupported_args(value):
    return value
'''


def corpus(directory):
    root = Path(directory)
    (root / "sqlglot").mkdir(parents=True)
    (root / "sqlglot" / "generator.py").write_text(SOURCE, encoding="utf-8")
    return root


def row(**over):
    base = {"instance_id": "tobymao__sqlglot-1", "repo": "tobymao/sqlglot",
            "base_commit": "a" * 40, "category": "Bug Report",
            "problem_statement": "Wrong result when transpiling a window clause between "
                                 "dialects; the emitted SQL drops the sort direction entirely.",
            "edit_functions": ["sqlglot/generator.py:Generator.ordered_sql"],
            "added_functions": []}
    base.update(over)
    return base


class Gold(unittest.TestCase):
    def test_a_qualified_method_resolves_by_its_leaf(self):
        """LOC-BENCH writes `Class.method`; quality_pass indexes `method`."""
        with tempfile.TemporaryDirectory() as raw:
            gold, why = locbench_suite.gold_for(row(), corpus(raw))
        self.assertIsNone(why)
        self.assertEqual(gold, ["sqlglot/generator.py::Generator::ordered_sql"])

    def test_an_added_function_disqualifies_the_instance(self):
        """It does not exist at base_commit, so no arm can find it."""
        with tempfile.TemporaryDirectory() as raw:
            gold, why = locbench_suite.gold_for(
                row(added_functions=["sqlglot/generator.py:Generator.brand_new"]), corpus(raw))
        self.assertIsNone(gold)
        self.assertIn("adds a function", why)

    def test_a_gold_absent_from_the_snapshot_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            gold, why = locbench_suite.gold_for(
                row(edit_functions=["sqlglot/generator.py:Generator.not_here"]), corpus(raw))
        self.assertIsNone(gold)
        self.assertIn("not indexed", why)


class LeakRule(unittest.TestCase):
    def test_a_generic_module_stem_is_not_a_leak(self):
        """Treating `parser.py` as a leak refused every sqlglot issue using the word parser."""
        gold = ["sqlglot/parser.py::Parser::_parse_join"]
        self.assertFalse(commit_suite.leaks(
            "the parser mishandles a join clause", gold, defined_names={"_parse_join": {"x"}}))

    def test_a_stem_the_corpus_defines_is_still_a_leak(self):
        """In Java the stem is the class, and naming it hands over the location."""
        gold = ["extras/RuntimeTypeAdapterFactory.java::create"]
        index = {"create": {"x"}, "RuntimeTypeAdapterFactory": {"x"}}
        self.assertTrue(commit_suite.leaks(
            "Fix RuntimeTypeAdapterFactory depending on Streams", gold, defined_names=index))

    def test_the_gold_symbol_is_always_a_leak(self):
        self.assertFalse(commit_suite.leaks(
            "something unrelated", ["a/b.py::ordered_sql"], defined_names={"ordered_sql": {"x"}}))
        self.assertTrue(commit_suite.leaks(
            "ordered_sql is wrong", ["a/b.py::ordered_sql"], defined_names={"ordered_sql": {"x"}}))


class Question(unittest.TestCase):
    def test_it_builds_a_compilable_entry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = corpus(raw)
            gold, _ = locbench_suite.gold_for(row(), root)
            entry, why = locbench_suite.question_for(row(), gold, root, root)
        self.assertIsNone(why)
        self.assertTrue(entry["question"].endswith(locbench_suite.INSTRUCTION))
        self.assertEqual(entry["evidence"][0]["path"], "sqlglot/generator.py")
        self.assertIn("sqlglot/generator.py::other_sql", entry["rejected_alternates"])
        self.assertEqual(entry["covariates"]["gold_cardinality"], 1)
        self.assertEqual(entry["corpus"], str(root))

    def test_hints_and_patches_never_reach_the_question(self):
        with tempfile.TemporaryDirectory() as raw:
            root = corpus(raw)
            data = row(hints_text="the fix is in ordered_sql", patch="--- a/x\n+++ b/x")
            gold, _ = locbench_suite.gold_for(data, root)
            entry, _ = locbench_suite.question_for(data, gold, root, root)
        self.assertNotIn("the fix is in", entry["question"])
        self.assertNotIn("+++", entry["question"])


class Deduplicate(unittest.TestCase):
    def test_identical_files_across_snapshots_become_one_inode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("snap-a", "snap-b"):
                (root / name).mkdir()
                (root / name / "same.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            (root / "snap-b" / "different.py").write_text("x = 2\n", encoding="utf-8")
            linked, saved = locbench_suite.deduplicate(root)
            self.assertEqual(linked, 1)
            self.assertGreater(saved, 0)
            self.assertEqual((root / "snap-a" / "same.py").stat().st_ino,
                             (root / "snap-b" / "same.py").stat().st_ino)
            # Content is unchanged, which is the only thing a corpus owes its reader.
            self.assertEqual((root / "snap-b" / "same.py").read_text(), "def f():\n    return 1\n")


if __name__ == "__main__":
    unittest.main()
