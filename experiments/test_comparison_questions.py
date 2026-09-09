"""Verify the comparison question set against the pinned corpus without the tools under test.

Gold is checked by reading source and by independent ripgrep counts, never by asking the
indexes the comparison measures: an index cannot validate its own answer key. The merged set
must be exactly the frozen v2 draft plus the authored expansion.
"""
import json
from pathlib import Path
import subprocess
import unittest

import benchmark

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent.parent / "runs/projects-v2-suite/coreutils"
CORPUS = SUITE / "corpus"
QUESTIONS = json.loads((HERE / "comparison_questions.json").read_text())
DRAFT = json.loads((HERE / "v2_questions_draft.json").read_text())
NEW = json.loads((HERE / "comparison_questions_new.json").read_text())


def symbol_path(qualified):
    return qualified.split("::", 1)[0]


def answer_paths(task):
    value = task["expected_json"]["answer"]
    if isinstance(value, list):
        return {symbol_path(item) for item in value if isinstance(item, str)}
    if isinstance(value, dict):
        if "path" in value:
            return {value["path"]}
        return {symbol_path(value["symbol"])}
    if isinstance(value, str):
        return {symbol_path(value)}
    return set()


class ComparisonQuestionTests(unittest.TestCase):
    def test_corpus_matches_its_snapshot(self):
        manifest = json.loads((SUITE / "snapshot.json").read_text())
        self.assertEqual(manifest["revision"], "c29429c13b79b3ab7928c7723eeaf95905a2e35e")
        self.assertEqual(benchmark.fingerprint(CORPUS), manifest["corpus"])

    def test_merged_set_is_draft_plus_authored_expansion(self):
        self.assertEqual(len(DRAFT), 12)
        self.assertEqual(len(NEW), 10)
        self.assertEqual([task["id"] for task in QUESTIONS], [task["id"] for task in DRAFT] + [task["id"] for task in NEW])
        self.assertEqual(len(QUESTIONS), 22)
        self.assertEqual(len({task["id"] for task in QUESTIONS}), 22)
        self.assertTrue(all(task["id"].startswith("comp-") for task in NEW), "expansion ids use the comp- prefix")

    def test_gold_grades_itself_under_the_comparison_grader(self):
        for task in QUESTIONS:
            graded = benchmark.grade_answer(task, json.dumps(task["expected_json"]), "json-answer-v3")
            self.assertTrue(graded["correct"], task["id"])
            self.assertEqual(graded["grading"], "json-answer-v3")

    def test_evidence_anchors_exist_in_the_pinned_corpus(self):
        for task in QUESTIONS:
            self.assertTrue(task["evidence"], task["id"])
            for evidence in task["evidence"]:
                text = (CORPUS / evidence["path"]).read_text(encoding="utf-8")
                for anchor in evidence["contains"]:
                    self.assertIn(anchor, text, (task["id"], evidence["path"], anchor))

    def test_answer_paths_are_real_files_in_the_expected_stratum(self):
        post = set((SUITE / "post_cutoff_files.txt").read_text().split())
        for task in QUESTIONS:
            paths = answer_paths(task)
            if not paths:
                # A null answer has no symbol to place; its stratum follows the evidence file.
                for evidence in task["evidence"]:
                    self.assertEqual(evidence["path"] in post, task["stratum"] == "post_cutoff",
                                     (task["id"], evidence["path"]))
                continue
            for path in paths:
                self.assertTrue((CORPUS / path).is_file(), (task["id"], path))
                self.assertEqual(path in post, task["stratum"] == "post_cutoff", (task["id"], path))

    def test_questions_do_not_leak_their_own_answers(self):
        for task in QUESTIONS:
            value = task["expected_json"]["answer"]
            values = value if isinstance(value, list) else [value]
            values = values + [item for item in values if isinstance(item, dict) for item in item.values()]
            for item in values:
                if isinstance(item, str) and "::" in item:
                    self.assertNotIn(item, task["question"], task["id"])
                    self.assertNotIn(symbol_path(item), task["question"],
                                     f"{task['id']} names the answer's file")

    def test_null_answers_have_no_candidate_definition(self):
        cases = (("comp-tac-no-dedup", r"fn\s+\w*(dedup|duplicate|unique|uniq)"),
                 ("comp-fold-no-sort", r"fn\s+\w*sort"))
        for task_id, pattern in cases:
            question = next(task for task in QUESTIONS if task["id"] == task_id)
            self.assertIsNone(question["expected_json"]["answer"], task_id)
            path = question["evidence"][0]["path"]
            found = subprocess.run(["rg", "--no-config", "-c", pattern, path],
                                   cwd=CORPUS, capture_output=True, text=True, timeout=60)
            self.assertEqual(found.returncode, 1,
                             f"{task_id}: a matching function would invalidate the null answer")

    def test_env_chain_symbols_and_cross_file_edge(self):
        question = next(task for task in QUESTIONS if task["id"] == "comp-env-variable-chain")
        chain = question["expected_json"]["answer"]
        for symbol in chain:
            path, name = symbol_path(symbol), symbol.split("::")[-1]
            found = subprocess.run(["rg", "--no-config", "-n", rf"fn\s+{name}\b", path],
                                   cwd=CORPUS, capture_output=True, text=True, timeout=60)
            self.assertEqual(found.returncode, 0, symbol)
        # The cross-file edge: VariableParser::skip_one drives StringParser::consume_chunk.
        edge = (CORPUS / "src/uu/env/src/variable_parser.rs").read_text(encoding="utf-8")
        self.assertIn("fn skip_one", edge)
        self.assertIn("consume_chunk()?", edge)

    def test_chroot_error_condition_is_grounded(self):
        text = (CORPUS / "src/uu/chroot/src/chroot.rs").read_text(encoding="utf-8")
        self.assertIn("fn name_to_uid(name: &str)", text)
        self.assertIn("ChrootError::NoSuchUser", text)


if __name__ == "__main__":
    unittest.main()
