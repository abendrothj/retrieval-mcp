import json
from pathlib import Path
import tempfile
import unittest

from audit_answers import audit_answer, report


class AuditTests(unittest.TestCase):
    def test_prose_is_separate_from_payload(self):
        task = {"expected_json":{"answer":32}}
        for text in ['{"answer":32}', 'The cap is 32.\n{"answer":32}', '```json\n{"answer":32}\n```']:
            self.assertEqual(audit_answer(task, text), "payload_matches")
        self.assertEqual(audit_answer(task, '{"answer":true}'), "payload_differs")
        self.assertEqual(audit_answer(task, 'The cap is 32.'), "manual_review")

    def test_ambiguity_and_malformed_json_are_not_recovered(self):
        task = {"expected_json":{"answer":32}}
        for text in ['{"answer":31} {"answer":32}', '{"answer":31,"answer":32}',
                     '{"answer":', '{"other":{"answer":32}}', '```json\n{"answer":32}']:
            self.assertEqual(audit_answer(task, text), "manual_review")

    def test_sets_and_ordered_chains_keep_frozen_rules(self):
        task = {"expected_json":{"answer":["a", "b"]}, "answer_set":True}
        self.assertEqual(audit_answer(task, '{"answer":["b","a"]}'), "payload_matches")
        self.assertEqual(audit_answer(task, '{"answer":["a","b","b"]}'), "payload_differs")
        task["answer_set"] = False
        self.assertEqual(audit_answer(task, '{"answer":["b","a"]}'), "payload_differs")

    def test_report_preserves_inputs_and_excludes_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/"modelshare-questions.json").write_text(json.dumps([{"id":"q", "expected_json":{"answer":32}}]))
            for status in ("completed", "failed"):
                folder = root/"modelshare"/status
                folder.mkdir(parents=True)
                (folder/"run.json").write_text(json.dumps({"task_id":"q", "profile":"A", "status":status,
                    "correct":False, "format_correct":False, "answer":'Found it.\n{"answer":32}'}))
            before = {str(p):p.read_bytes() for p in root.rglob("*.json")}
            result = report(root)
            self.assertEqual(result["counts"]["modelshare/A"]["recovered_payload_matches"], 1)
            self.assertEqual(result["counts"]["modelshare/A"]["excluded_unfinished"], 1)
            self.assertEqual(before, {str(p):p.read_bytes() for p in root.rglob("*.json")})


if __name__ == "__main__":
    unittest.main()
