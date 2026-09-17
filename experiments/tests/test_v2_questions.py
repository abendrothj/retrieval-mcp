"""Verify the v2 draft against the pinned corpus without using the tools under test.

Gold is checked by reading source and by an independent ripgrep count, never by asking
find_symbol or find_callers: an index cannot validate its own answer key.
"""
import json
from pathlib import Path
import re
import subprocess
import unittest

import benchmark

HERE = Path(__file__).resolve().parents[1]
SUITE = HERE.parent.parent/"runs/projects-v2-suite/coreutils"
CORPUS = SUITE/"corpus"
QUESTIONS = json.loads((HERE/"suites/v2_questions_draft.json").read_text())
CATEGORIES = {"exact_lookup", "conceptual_lookup", "symbol_resolution",
              "direct_caller_lookup", "transitive_blast_radius", "mixed_discovery_structure"}


def symbol_path(qualified):
    return qualified.split("::", 1)[0]


# The pinned corpus lives beside its run artifacts, outside this repository, so these checks are a
# local gate rather than a CI one. Skipping keeps that honest: a machine without the corpus reports
# "skipped", never a pass it did not earn.
@unittest.skipUnless(CORPUS.is_dir(), f"pinned corpus not present at {CORPUS}")
class DraftTests(unittest.TestCase):
    def test_corpus_matches_its_snapshot(self):
        manifest = json.loads((SUITE/"snapshot.json").read_text())
        self.assertEqual(manifest["revision"], "c29429c13b79b3ab7928c7723eeaf95905a2e35e")
        self.assertEqual(benchmark.fingerprint(CORPUS), manifest["corpus"])

    def test_shape_balance_and_typed_gold(self):
        self.assertEqual(len(QUESTIONS), 12)
        self.assertEqual(len({q["id"] for q in QUESTIONS}), 12)
        for category in CATEGORIES:
            self.assertEqual(sum(q["category"] == category for q in QUESTIONS), 2, category)
        for stratum in ("pre_cutoff", "post_cutoff"):
            self.assertEqual(sum(q["stratum"] == stratum for q in QUESTIONS), 6, stratum)
        for task in QUESTIONS:
            self.assertIn(task["category"], CATEGORIES)
            self.assertTrue(task["question"].strip())
            # Gold must grade itself correct under the frozen grader.
            graded = benchmark.grade_answer(task, json.dumps(task["expected_json"]))
            self.assertTrue(graded["correct"], task["id"])
            self.assertEqual(graded["grading"], "json-answer-v2")

    def test_evidence_anchors_exist_in_the_pinned_corpus(self):
        for task in QUESTIONS:
            self.assertTrue(task["evidence"], task["id"])
            for evidence in task["evidence"]:
                text = (CORPUS/evidence["path"]).read_text(encoding="utf-8")
                for anchor in evidence["contains"]:
                    self.assertIn(anchor, text, (task["id"], evidence["path"], anchor))

    def test_answer_paths_are_real_files_in_the_expected_stratum(self):
        post = set((SUITE/"post_cutoff_files.txt").read_text().split())
        self.assertEqual(len(post), 431)
        for task in QUESTIONS:
            value = task["expected_json"]["answer"]
            values = value if isinstance(value, list) else [value]
            paths = {symbol_path(v) for v in values if isinstance(v, str)}
            if isinstance(value, dict):
                paths = {value["path"]} if "path" in value else {symbol_path(value["symbol"])}
            for path in paths:
                self.assertTrue((CORPUS/path).is_file(), (task["id"], path))
                # Evidence for a stratum claim is the file's own history, not the question's label.
                self.assertEqual(path in post, task["stratum"] == "post_cutoff", (task["id"], path))

    def test_questions_do_not_leak_their_own_answers(self):
        for task in QUESTIONS:
            value = task["expected_json"]["answer"]
            values = value if isinstance(value, list) else [value]
            values = values + [v for item in values if isinstance(item, dict) for v in item.values()]
            for item in values:
                if isinstance(item, str) and "::" in item:
                    self.assertNotIn(item, task["question"], task["id"])
                    self.assertNotIn(symbol_path(item), task["question"],
                                     f"{task['id']} names the answer's file")

    def test_line_number_gold_matches_the_pinned_file(self):
        task = next(q for q in QUESTIONS if q["id"] == "diag-list-items-definition")
        answer = task["expected_json"]["answer"]
        lines = (CORPUS/answer["path"]).read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[answer["line"] - 1].startswith("pub fn list_items"))

    def test_caller_sets_match_an_independent_ripgrep_pass(self):
        # Count mentions per file; the gold caller sets must be a subset of files that mention
        # the symbol at all, and the absence answer must have no candidate definition.
        for task, symbol in (("diag-char-span-callers", "char_span"),
                             ("fmt-break-simple-callers", "break_simple"),
                             ("hardware-detect-avx2-reference", "detect_avx2")):
            question = next(q for q in QUESTIONS if q["id"] == task)
            found = subprocess.run(["rg", "--no-config", "-l", rf"\b{symbol}\b", "-g", "*.rs", "."],
                                   cwd=CORPUS, capture_output=True, text=True, timeout=60)
            mentioned = set(found.stdout.split())
            value = question["expected_json"]["answer"]
            items = value if isinstance(value, list) else [value]
            items = [i["symbol"] if isinstance(i, dict) else i for i in items]
            for item in items:
                self.assertIn("./" + symbol_path(item), mentioned, (task, item))
            # A referenced-but-not-called symbol must still be mentioned in exactly its own file.
            if task == "hardware-detect-avx2-reference":
                self.assertEqual(mentioned, {"./src/uucore/src/lib/features/hardware.rs"})
        absent = subprocess.run(["rg", "--no-config", "-c", r"fn .*(format|display|to_string)",
                                 "src/uu/sort/src/numeric_str_cmp.rs"],
                                cwd=CORPUS, capture_output=True, text=True, timeout=60)
        self.assertEqual(absent.returncode, 1, "a formatting function would invalidate the null answer")


if __name__ == "__main__":
    unittest.main()
