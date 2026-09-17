import json
from pathlib import Path
import tempfile
import unittest

from audit_failures import true_callers
from validate_suite import validate


class ValidateSuiteTests(unittest.TestCase):
    def test_python_callers_are_reported_by_enclosing_function(self):
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary)
            (corpus / "definition.py").write_text("def helper():\n    pass\n")
            (corpus / "callers.py").write_text(
                "def direct():\n"
                "    helper()\n\n"
                "class Owner:\n"
                "    def method(self):\n"
                "        helper()\n"
            )

            self.assertEqual(
                true_callers(corpus, "helper", "definition.py"),
                ["callers.py::direct", "callers.py::method"],
            )

    def test_schema_source_anchors_and_target_leaks_are_build_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "module.py").write_text("def target():\n    pass\n")
            questions = root / "questions.json"
            questions.write_text(json.dumps([{
                "id": "q",
                "category": "conceptual_lookup",
                "set": "dev",
                "question": "Which function is target?",
                "expected_json": {"answer": "module.py::target"},
                "helper": "missing.py::target",
                "rejected_alternates": ["module.py::other"],
                "evidence": [{"path": "module.py", "contains": "missing snippet"}],
                "author_notes": "Deliberately invalid fixture.",
            }]))

            result = validate(questions, corpus)

            self.assertEqual(result["questions_with_problems"], 1)
            details = {problem["detail"] for problem in result["findings"]["q"]}
            self.assertIn("evidence[0] snippet is absent from module.py", details)
            self.assertIn("question leaks target identifier 'target'", details)
            self.assertIn("helper places target in missing.py, but the corpus does not", details)

    def test_a_caller_the_gold_cannot_name_uniquely_is_a_build_failure(self):
        """Two methods of one name in one file collapse into one identity the grader compares."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "helper.go").write_text("package p\n\nfunc Guard() bool {\n\treturn true\n}\n")
            (corpus / "callers.go").write_text(
                "package p\n\n"
                "type left struct{}\n"
                "type right struct{}\n\n"
                "func (l left) Check() bool {\n\treturn Guard()\n}\n\n"
                "func (r right) Check() bool {\n\treturn Guard()\n}\n"
            )
            questions = root / "questions.json"
            questions.write_text(json.dumps([{
                "id": "q",
                "category": "direct_caller_lookup",
                "set": "dev",
                "question": "Name every method outside its own file that calls the boolean guard.",
                "expected_json": {"answer": ["callers.go::Check"]},
                "helper": "helper.go::Guard",
                "exhaustive": True,
                "rejected_alternates": [],
                "evidence": [{"path": "callers.go", "contains": "return Guard()"}],
                "author_notes": "Two receivers, one method name: the gold can name it once.",
            }]))

            result = validate(questions, corpus)

            self.assertEqual(result["questions_with_problems"], 1)
            details = " ".join(problem["detail"] for problem in result["findings"]["q"])
            self.assertIn("callers.go defines Check 2 times", details)
            self.assertIn("penalised as extras", details)

    def test_a_caller_question_silent_about_test_callers_is_a_build_failure(self):
        """Whether a test function counts is a convention only the question can settle."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "helper.go").write_text("package p\n\nfunc Guard() bool {\n\treturn true\n}\n")
            (corpus / "use.go").write_text("package p\n\nfunc Use() bool {\n\treturn Guard()\n}\n")
            (corpus / "guard_test.go").write_text(
                "package p\n\nfunc TestGuard(t *T) {\n\t_ = Guard()\n}\n")
            question = {
                "id": "q",
                "category": "direct_caller_lookup",
                "set": "dev",
                "question": "Name every function outside its own file that calls the boolean guard.",
                "expected_json": {"answer": ["use.go::Use", "guard_test.go::TestGuard"]},
                "helper": "helper.go::Guard",
                "exhaustive": True,
                "rejected_alternates": ["helper.go::Guard"],
                "evidence": [{"path": "use.go", "contains": "return Guard()"}],
                "author_notes": "The gold counts the test caller; the prose never says so.",
            }
            questions = root / "questions.json"
            questions.write_text(json.dumps([question]))

            silent = validate(questions, corpus)

            self.assertEqual(silent["questions_with_problems"], 1)
            self.assertIn("include test files and the question does not say whether they count",
                          " ".join(problem["detail"] for problem in silent["findings"]["q"]))

            question["question"] += " Test functions count as callers; name them too."
            questions.write_text(json.dumps([question]))

            self.assertEqual(validate(questions, corpus)["problems"], 0)

    def test_a_module_scoped_exclusion_over_a_sibling_caller_is_a_build_failure(self):
        """"Module" names the defining module and its directory equally well; a gold cannot pick."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            (corpus / "parser").mkdir(parents=True)
            (corpus / "far").mkdir()
            (corpus / "parser" / "size.py").write_text("def with_allow_list(parser):\n    return parser\n")
            (corpus / "parser" / "signed.py").write_text(
                "from .size import with_allow_list\n\n\ndef parse_count(text):\n"
                "    return with_allow_list(text)\n")
            (corpus / "far" / "sort.py").write_text(
                "from ..parser.size import with_allow_list\n\n\ndef parse_byte_count(text):\n"
                "    return with_allow_list(text)\n")
            question = {
                "id": "q",
                "category": "direct_caller_lookup",
                "set": "dev",
                "question": ("Name every function outside the parser's own module that configures a parser "
                             "through the whitelist method, each as a qualified symbol of the form "
                             "path::function. Exclude code under test."),
                "expected_json": {"answer": ["far/sort.py::parse_byte_count",
                                             "parser/signed.py::parse_count"]},
                "helper": "parser/size.py::with_allow_list",
                "exhaustive": True,
                "rejected_alternates": ["parser/size.py::with_allow_list"],
                "evidence": [{"path": "far/sort.py", "contains": "return with_allow_list(text)"}],
                "author_notes": "The gold counts the sibling in the helper's own directory.",
            }
            questions = root / "questions.json"
            questions.write_text(json.dumps([question]))

            ambiguous = validate(questions, corpus)

            self.assertEqual(ambiguous["questions_with_problems"], 1)
            self.assertIn("scopes its exclusion by module while parser/signed.py calls",
                          " ".join(problem["detail"] for problem in ambiguous["findings"]["q"]))

            question["question"] = ("Name every function that configures a parser through the whitelist "
                                    "method, each as a qualified symbol of the form path::function. "
                                    "Exclude the file that defines that method, and exclude code under "
                                    "test; a caller in a neighbouring file of the same directory counts.")
            questions.write_text(json.dumps([question]))

            self.assertEqual(validate(questions, corpus)["problems"], 0)


class TwoHopTests(unittest.TestCase):
    """Two-hop expansion is by name, so it inherits every namesake and every set ambiguity."""

    def _suite(self, root, question, answer):
        path = root / "questions.json"
        path.write_text(json.dumps([{
            "id": "q", "category": "transitive_blast_radius", "set": "dev", "question": question,
            "expected_json": {"answer": answer}, "helper": "leaf.go::target", "hops": 2,
            "exhaustive": True, "include_defining_file": True,
            "rejected_alternates": ["leaf.go::other"],
            "evidence": [{"path": "leaf.go", "contains": "func target"}],
            "author_notes": "fixture",
        }]))
        return path

    def test_a_namesake_on_the_first_hop_makes_the_gold_unauthorable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "leaf.go").write_text(
                "package p\n\nfunc target() {}\n\nfunc other() {}\n", encoding="utf-8")
            (corpus / "mid.go").write_text(
                "package p\n\nfunc acquire() { target() }\n", encoding="utf-8")
            (corpus / "elsewhere.go").write_text(
                "package p\n\nfunc acquire(n int) {}\n\nfunc user() { acquire(1) }\n",
                encoding="utf-8")
            questions = self._suite(root, "Every function reaching the leaf in two hops. Name "
                                          "every function or method in the corpus that calls it.",
                                    ["elsewhere.go::user"])

            problems = validate(questions, corpus)["findings"]["q"]

            self.assertIn("defined 2 times where that expansion can see it",
                          " ".join(problem["detail"] for problem in problems))


if __name__ == "__main__":
    unittest.main()
