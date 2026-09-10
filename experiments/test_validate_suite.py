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


if __name__ == "__main__":
    unittest.main()
