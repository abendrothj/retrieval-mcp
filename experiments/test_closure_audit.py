import json
from pathlib import Path
import tempfile
import unittest

from closure_audit import audit


def trial(directory, task_id, system, answer, tool_bodies):
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps({
        "task_id": task_id, "system": system, "status": "completed", "repetition": 1,
        "answer": answer, "attempted_calls": len(tool_bodies),
    }))
    (directory / "codex-events.jsonl").write_text("\n".join(
        json.dumps({"type": "item.completed",
                    "item": {"type": "mcp_tool_call", "tool": "search_concept", "result": body}})
        for body in tool_bodies))


class ClosureAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.corpus = root / "corpus"
        self.corpus.mkdir()
        (self.corpus / "alpha.py").write_text("def wanted():\n    pass\n")
        self.questions = root / "questions.json"
        self.questions.write_text(json.dumps([
            {"id": "q-stop", "expected_json": {"answer": "alpha.py::wanted"}},
            {"id": "q-seen", "expected_json": {"answer": "alpha.py::wanted"}},
        ]))
        self.run = root / "run"
        self.run.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_a_loss_with_the_fact_never_on_screen_is_a_premature_stop(self):
        trial(self.run / "trial-0000", "q-stop", "retrieval-mcp",
              '{"answer": "alpha.py::wanted"}', ["alpha.py::wanted is the one"])
        trial(self.run / "trial-0001", "q-stop", "retrieval-mcp-closure",
              '{"answer": "alpha.py::other"}', ["nothing relevant here"])

        result = audit(self.run, self.questions, self.corpus,
                       "retrieval-mcp", "retrieval-mcp-closure")

        self.assertEqual(result["verdicts"], {"premature_stop": 1, "expansion_skipped": 1})
        self.assertEqual(result["findings"]["q-stop"]["never_retrieved"], ["alpha.py::wanted"])

    def test_a_loss_with_the_fact_on_screen_is_not_blamed_on_stopping(self):
        trial(self.run / "trial-0000", "q-seen", "retrieval-mcp",
              '{"answer": "alpha.py::wanted"}', ["alpha.py::wanted"])
        trial(self.run / "trial-0001", "q-seen", "retrieval-mcp-closure",
              '{"answer": "alpha.py::decoy"}', ["alpha.py::wanted was right there"])

        result = audit(self.run, self.questions, self.corpus,
                       "retrieval-mcp", "retrieval-mcp-closure")

        self.assertEqual(result["verdicts"], {"evidence_seen": 1})
        self.assertEqual(result["treatment_regressions"], 1)

    def test_equal_and_improved_questions_are_not_counted_as_regressions(self):
        for name, system in (("trial-0000", "retrieval-mcp"), ("trial-0001", "retrieval-mcp-closure")):
            trial(self.run / name, "q-seen", system, '{"answer": "alpha.py::wanted"}',
                  ["alpha.py::wanted"])
        trial(self.run / "trial-0002", "q-stop", "retrieval-mcp",
              '{"answer": "alpha.py::miss"}', ["alpha.py::wanted"])
        trial(self.run / "trial-0003", "q-stop", "retrieval-mcp-closure",
              '{"answer": "alpha.py::wanted"}', ["alpha.py::wanted"])

        result = audit(self.run, self.questions, self.corpus,
                       "retrieval-mcp", "retrieval-mcp-closure")

        self.assertEqual(result["treatment_regressions"], 0)
        self.assertEqual(result["treatment_improvements"], 1)
        self.assertEqual(result["questions_paired"], 2)


if __name__ == "__main__":
    unittest.main()
