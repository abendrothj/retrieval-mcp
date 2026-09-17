import json
from pathlib import Path
import tempfile
import unittest

from query_pathology import labels, report


def write_run(root, run_id, task, trial, profile, calls):
    directory = root/run_id
    directory.mkdir(parents=True)
    (directory/"run.json").write_text(json.dumps({"run_id":run_id, "task_id":task, "trial":trial,
        "profile":profile, "status":"completed", "correct":True, "format_correct":True}))
    lines = []
    for sequence, (tool, arguments, count) in enumerate(calls, 1):
        common = {"schema_version":1, "run_id":run_id, "profile":profile, "tool":tool,
                  "arguments":arguments, "sequence":sequence, "request_id":sequence}
        lines.append({**common, "event":"tool_start"})
        lines.append({**common, "event":"tool_end", "result_count":count, "error":None,
                      "retrieval_bytes":10*count})
    (directory/"server.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))


class LabelTests(unittest.TestCase):
    def test_escaped_pipe_depends_on_the_regex_flag(self):
        self.assertIn("escaped_pipe_regex", labels("search_exact", {"query":"a\\|b", "regex":True}, 0, None))
        self.assertIn("escaped_pipe_literal", labels("search_exact", {"query":"a\\|b"}, 0, None))
        self.assertIn("alternation_shaped", labels("search_exact", {"query":"a|b", "regex":True}, 3, None))

    def test_form_and_outcome_labels_are_independent(self):
        found = labels("search_exact", {"query":"size and digest"}, 0, None)
        self.assertEqual(sorted(found), ["empty", "literal_multiword"])
        # A multi-word literal that does match is still a multi-word literal, and is not empty.
        self.assertEqual(labels("search_exact", {"query":"size and digest"}, 2, None), ["literal_multiword"])
        # Errors are not counted as empty results.
        self.assertEqual(labels("read_source", {"path":"x"}, None, "boom"), ["error"])


class ReportTests(unittest.TestCase):
    def test_pairs_and_bookkeeping_residual(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_run(root, "t-1-A", "t", 1, "A", [("search_exact", {"query":"a\\|b", "regex":True, "limit":20}, 0),
                                                   ("search_exact", {"query":"a", "limit":20}, 20),
                                                   ("read_source", {"path":"a.rs"}, 40)])
            write_run(root, "t-1-D", "t", 1, "D", [("find_symbol", {"name":"a"}, 1)])
            result = report(root)
            self.assertEqual(result["runs_analyzed"], 2)
            self.assertEqual(result["profiles"]["A"]["empty_calls"], 1)
            self.assertEqual(result["profiles"]["A"]["labels"]["escaped_pipe_regex"], 1)
            self.assertEqual(result["profiles"]["A"]["labels"]["page_truncated_not_followed"], 1)
            self.assertNotIn("escaped_pipe_regex", result["profiles"]["D"]["labels"])
            pair = result["a_vs_d_pairs"][0]
            self.assertEqual((pair["excess"], pair["excess_excluding_empty"]), (2, 1))

    def test_followed_pagination_is_distinguished(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_run(root, "t-1-A", "t", 1, "A", [("search_exact", {"query":"a", "limit":2}, 2),
                                                   ("search_exact", {"query":"a", "limit":2, "offset":2}, 1)])
            profile = report(root)["profiles"]["A"]
            self.assertEqual(profile["labels"]["page_followed"], 1)
            self.assertNotIn("page_truncated_not_followed", profile["labels"])


if __name__ == "__main__":
    unittest.main()
