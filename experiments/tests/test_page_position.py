import json
from pathlib import Path
import tempfile
import unittest

import page_position

CODEX_PAGE = {"type": "item.completed", "item": {
    "type": "mcp_tool_call", "tool": "search_concept", "status": "completed",
    "arguments": {"query": "shared key release"},
    "result": {"content": [{"type": "text", "text": json.dumps({"results": [
        {"path": "a/other.c", "start_line": 1, "symbol": {"symbol": "a/other.c::noise"}},
        {"path": "a/other.c", "start_line": 9, "symbol": {"symbol": "a/other.c::more_noise"}},
        {"path": "net/auth.c", "start_line": 4, "excerpt": "call sctp_auth_shkey_release(k);",
         "symbol": {"symbol": "net/auth.c::sctp_auth_destroy_keys"}},
        {"path": "z/z.c", "start_line": 2, "symbol": {"symbol": "z/z.c::tail"}},
    ]})}]}}}
CODEX_READ = {"type": "item.completed", "item": {
    "type": "mcp_tool_call", "tool": "read_source", "status": "completed", "arguments": {},
    "result": {"content": [{"type": "text", "text": json.dumps(
        {"path": "net/auth.c", "lines": "void sctp_auth_set_key(void) {}", "total_lines": 30})}]}}}
TASK = {"id": "q1", "expected_json": {"answer": {
    "callers": ["net/auth.c::sctp_auth_destroy_keys", "net/auth.c::sctp_auth_set_key"]}}}


def trial(directory, events, system="retrieval-mcp"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "codex-events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8")
    (directory / "run.json").write_text(json.dumps(
        {"task_id": "q1", "system": system, "status": "completed"}), encoding="utf-8")
    return directory


class KeptRows(unittest.TestCase):
    def test_matches_the_proxy_it_is_a_ceiling_for(self):
        """The whole reading is void if this prefix is not the prefix degrade_server keeps."""
        try:
            import degrade_server
        except ImportError:  # pragma: no cover - a checkout without the proxy still tests the rest
            self.skipTest("degrade_server.py is not in this checkout")
        for fraction in page_position.FRACTIONS:
            order = degrade_server.orderer(False, fraction, seed=1)
            for count in range(1, 31):
                self.assertEqual(len(order(count)), page_position.kept_rows(count, fraction),
                                 f"page of {count} at fraction {fraction}")

    def test_a_cut_never_empties_a_page(self):
        self.assertEqual(page_position.kept_rows(3, 0.1), 1)

    def test_attribution_columns_match_the_proxy(self):
        try:
            import degrade_server
        except ImportError:  # pragma: no cover
            self.skipTest("degrade_server.py is not in this checkout")
        self.assertEqual(page_position.ATTRIBUTION, degrade_server.ATTRIBUTION)


class RankedRows(unittest.TestCase):
    def test_reads_a_codex_text_block(self):
        body = json.dumps(CODEX_PAGE["item"]["result"])
        self.assertEqual(len(page_position.ranked_rows(body)), 4)

    def test_reads_a_claude_structured_block(self):
        body = json.dumps({"content": "{}", "structuredContent": {"results": [{"path": "a"}]}})
        self.assertEqual(page_position.ranked_rows(body), [{"path": "a"}])

    def test_a_read_source_payload_has_no_ranked_rows(self):
        body = json.dumps(CODEX_READ["item"]["result"])
        self.assertIsNone(page_position.ranked_rows(body))


class Sightings(unittest.TestCase):
    def setUp(self):
        self.pages = [json.loads(CODEX_PAGE["item"]["result"]["content"][0]["text"])["results"]]

    def test_position_is_the_served_rank(self):
        seen = page_position.sightings(self.pages, ("net/auth.c", "sctp_auth_destroy_keys"))
        self.assertEqual(seen, [(3, 4)])

    def test_a_cut_above_the_gold_keeps_it_and_below_it_does_not(self):
        seen = [(3, 4)]
        self.assertTrue(page_position.survives(seen, 0.75))
        self.assertFalse(page_position.survives(seen, 0.5))

    def test_blinding_hides_a_gold_that_only_the_symbol_column_named(self):
        pair = ("net/auth.c", "sctp_auth_destroy_keys")
        self.assertTrue(page_position.sightings(self.pages, pair))
        self.assertFalse(page_position.sightings(self.pages, pair, blind=True))

    def test_blinding_keeps_a_gold_the_excerpt_also_names(self):
        pair = ("net/auth.c", "sctp_auth_shkey_release")
        self.assertTrue(page_position.sightings(self.pages, pair, blind=True))


class Report(unittest.TestCase):
    def test_reads_a_run_and_separates_off_page_gold_from_lost_gold(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            questions = root / "suite.json"
            questions.write_text(json.dumps([TASK]), encoding="utf-8")
            run = root / "report"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "questions_sha256": __import__("hashlib").sha256(
                    questions.read_bytes()).hexdigest()}), encoding="utf-8")
            trial(run / "trial-0000", [CODEX_PAGE, CODEX_READ])
            trial(run / "trial-0001", [CODEX_PAGE, CODEX_READ], system="native-control")
            result = page_position.report(run, questions, "retrieval-mcp")
        self.assertEqual(result["trials_read"], 1)
        identities = result["per_trial"]["q1"][0]["identities"]
        served = identities["net/auth.c::sctp_auth_destroy_keys"]
        self.assertEqual(served["sightings"], [{"position": 3, "page_rows": 4}])
        self.assertTrue(served["survives"]["0.75"])
        self.assertFalse(served["survives"]["0.5"])
        # The second gold was only ever read, never ranked: a page cut cannot remove it.
        offpage = identities["net/auth.c::sctp_auth_set_key"]
        self.assertFalse(offpage["on_ranked_page"])
        self.assertTrue(offpage["seen_anywhere"])
        self.assertEqual(result["ceiling"]["0.5"]["questions_at_risk_any_repetition"], 1)
        self.assertEqual(result["ceiling"]["0.75"]["questions_at_risk_any_repetition"], 0)
        self.assertEqual(result["bands"], {"top 0.1": 0, "top 0.25": 0, "top 0.5": 0,
                                           "top 0.75": 1, "top 1": 0,
                                           "never on a ranked page": 0})
        self.assertIn("render", dir(page_position))
        self.assertIn("q1", page_position.render(result))

    def test_a_vendored_index_manifest_is_not_a_run_manifest(self):
        """A corpus copy carries .zvec-grep/manifest.json; it pins no question set."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            questions = root / "suite.json"
            questions.write_text(json.dumps([TASK]), encoding="utf-8")
            run = root / "report"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "questions_sha256": __import__("hashlib").sha256(
                    questions.read_bytes()).hexdigest()}), encoding="utf-8")
            vendored = run / "corpus" / ".zvec-grep"
            vendored.mkdir(parents=True)
            (vendored / "manifest.json").write_text(json.dumps({"chunks": 12}), encoding="utf-8")
            trial(run / "trial-0000", [CODEX_PAGE, CODEX_READ])
            self.assertEqual(page_position.report(run, questions, "retrieval-mcp")["trials_read"],
                             1)

    def test_a_suite_the_run_did_not_use_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            questions = root / "suite.json"
            questions.write_text(json.dumps([TASK]), encoding="utf-8")
            run = root / "report"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"questions_sha256": "0" * 64}),
                                               encoding="utf-8")
            with self.assertRaises(ValueError):
                page_position.report(run, questions, "retrieval-mcp")


if __name__ == "__main__":
    unittest.main()
