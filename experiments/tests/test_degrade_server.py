"""The positive control: a server damaged on purpose, so a suite can prove it sees retrieval.

A control that silently stopped degrading would score like HEAD and read as "this suite cannot
tell a good page from a bad one" - the one conclusion the arm exists to make trustworthy. So the
properties asserted here are the ones that keep it a control: the surface is untouched, ranking
moves without the byte count moving, and an undegradable payload raises rather than passing.
"""
import copy
import json
import unittest

from degrade_server import degrade, drop_column, orderer, pin_excerpt, rewrite_request


def page(rows):
    """One tool result in both representations a client may read."""
    header = "results[%d] path\tsymbol" % len(rows)
    lines = "\n".join(f"src/{name}.rs\t{name}" for name in rows)
    return {"jsonrpc": "2.0", "id": 3, "result": {
        "content": [{"type": "text", "text": f"backend: bm25\n{header}\n{lines}\nhas_more: false"}],
        "structuredContent": {"backend": "bm25",
                              "results": [{"path": f"src/{name}.rs", "symbol": name}
                                          for name in rows]}}}


def rendered(payload):
    text = payload["result"]["content"][0]["text"].split("\n")
    return [line.split("\t")[-1] for line in text[2:-1]]


def structured(payload):
    return [row["symbol"] for row in payload["result"]["structuredContent"]["results"]]


class ShuffleTests(unittest.TestCase):
    def setUp(self):
        self.rows = [f"sym{index}" for index in range(10)]

    def test_ranking_is_destroyed_and_the_bytes_are_not(self):
        """Only quality can move under this arm. A token difference against HEAD would be a
        defect in the proxy rather than a finding."""
        before = page(self.rows)
        after = degrade(copy.deepcopy(before), orderer(True, None, 7))
        self.assertNotEqual(structured(after), self.rows)
        self.assertEqual(sorted(structured(after)), sorted(self.rows))
        self.assertEqual(len(json.dumps(after)), len(json.dumps(before)))

    def test_both_representations_move_together(self):
        """Claude Code reads the rendered text and never sees the schema; Codex forwards the whole
        result. Degrading one and not the other gives the two clients different arms."""
        after = degrade(page(self.rows), orderer(True, None, 7))
        self.assertEqual(rendered(after), structured(after))

    def test_the_permutation_is_seeded(self):
        left = degrade(page(self.rows), orderer(True, None, 11))
        right = degrade(page(self.rows), orderer(True, None, 11))
        self.assertEqual(structured(left), structured(right))


class PageFractionTests(unittest.TestCase):
    def test_the_block_count_is_rewritten_with_the_rows(self):
        after = degrade(page([f"sym{index}" for index in range(10)]), orderer(False, 0.5, 7))
        self.assertEqual(len(structured(after)), 5)
        self.assertEqual(rendered(after), structured(after))
        self.assertIn("results[5] ", after["result"]["content"][0]["text"])

    def test_a_short_page_keeps_one_row_rather_than_none(self):
        after = degrade(page(["only"]), orderer(False, 0.25, 7))
        self.assertEqual(structured(after), ["only"])


def caller_page(rows):
    """A `find_callers` page: the attribution sorts first, and an excerpt sits beside it."""
    header = "results[%d] caller\texcerpt\tpath" % len(rows)
    lines = "\n".join(f"{caller}\t{excerpt}\tsrc/a.rs" for caller, excerpt in rows)
    return {"jsonrpc": "2.0", "id": 5, "result": {
        "content": [{"type": "text", "text": f"{header}\n{lines}\nhas_more: false"}],
        "structuredContent": {"results": [{"caller": caller, "excerpt": excerpt, "path": "src/a.rs"}
                                          for caller, excerpt in rows]}}}


class BlindAttributionTests(unittest.TestCase):
    """The transport placebo. The client forwards an MCP result whole and truncates shell output
    to about a tenth of it, so "being forwarded whole" is a live alternative explanation for a
    measured gap; this arm is what separates it from "structure helps"."""

    def test_the_definition_identity_leaves_both_representations(self):
        after = degrade(page(["alpha", "beta"]), orderer(False, None, 7), blind_attribution=True)
        self.assertEqual(after["result"]["structuredContent"]["results"],
                         [{"path": "src/alpha.rs"}, {"path": "src/beta.rs"}])
        self.assertIn("results[2] path", after["result"]["content"][0]["text"])
        self.assertNotIn("alpha\n", after["result"]["content"][0]["text"])

    def test_a_caller_row_loses_its_call_site_definition_and_keeps_the_evidence(self):
        after = degrade(caller_page([("outer", "let x = inner();")]), orderer(False, None, 7),
                        blind_attribution=True)
        row = after["result"]["structuredContent"]["results"][0]
        self.assertEqual(row, {"excerpt": "let x = inner();", "path": "src/a.rs"})
        self.assertIn("results[1] excerpt\tpath", after["result"]["content"][0]["text"])

    def test_an_excerpt_holding_the_delimiter_survives(self):
        """The row is split once from the end the column sits at, so the rest is never parsed."""
        excerpt = "\tint\tx\t= helper(b);"
        after = degrade(caller_page([("caller_fn", excerpt)]), orderer(False, None, 7),
                        blind_attribution=True)
        self.assertEqual(after["result"]["structuredContent"]["results"][0]["excerpt"], excerpt)
        self.assertIn(excerpt, after["result"]["content"][0]["text"])

    def test_a_page_with_no_attribution_is_untouched(self):
        """`search_exact` rows are already path, line and snippet."""
        grep = {"jsonrpc": "2.0", "id": 6, "result": {
            "content": [{"type": "text", "text": "results[1] line\tpath\n152\tsrc/a.rs"}],
            "structuredContent": {"results": [{"line": 152, "path": "src/a.rs"}]}}}
        self.assertEqual(degrade(copy.deepcopy(grep), orderer(False, None, 7),
                                 blind_attribution=True), grep)

    def test_blinding_composes_with_shuffling(self):
        after = degrade(page([f"sym{index}" for index in range(6)]), orderer(True, None, 3),
                        blind_attribution=True)
        self.assertEqual([row["path"] for row in after["result"]["structuredContent"]["results"]],
                         after["result"]["content"][0]["text"].split("\n")[2:-1])

    def test_a_field_in_only_some_rows_raises(self):
        mixed = page(["a", "b"])
        del mixed["result"]["structuredContent"]["results"][1]["symbol"]
        with self.assertRaises(ValueError):
            degrade(mixed, orderer(False, None, 7), blind_attribution=True)

    def test_a_middle_column_is_dropped_by_splitting_the_whole_row(self):
        """`symbol` is last only while the row has no excerpt. Ask for one and the renderer
        appends `excerpt_truncated` after it, so the field lands in the middle. Refusing that
        killed the arm on the first excerpt-bearing page."""
        columns, rows = drop_column(["path", "symbol", "line"], ["src/a.rs\tname\t1"], "symbol")
        self.assertEqual((columns, rows), (["path", "line"], ["src/a.rs\t1"]))

    def test_a_middle_drop_raises_when_a_value_holds_a_raw_delimiter(self):
        """The whole-row split is safe only because the renderer escapes tabs, and nothing tests
        that escaping on this proxy's behalf, so it is verified per row instead of trusted."""
        with self.assertRaises(ValueError):
            drop_column(["path", "symbol", "excerpt", "flag"],
                        ["src/a.rs\tname\tint\tx = f();\tfalse"], "symbol")

    def test_an_excerpt_bearing_concept_row_blinds_end_to_end(self):
        """The real shape: alphabetical columns with `excerpt_truncated` appended last."""
        columns = "end_line\texcerpt\tpath\tscore\tstart_line\tsymbol\texcerpt_truncated"
        row = "37\tfunc a() {\\n\\treturn\\n}\tdoc/util.go\t10.2\t26\t{\"symbol\":\"doc/util.go::a\"}\t"
        payload = {"jsonrpc": "2.0", "id": 8, "result": {
            "content": [{"type": "text", "text": f"results[1] {columns}\n{row}\nhas_more: false"}],
            "structuredContent": {"results": [{
                "end_line": 37, "excerpt": "func a() {\n\treturn\n}", "path": "doc/util.go",
                "score": 10.2, "start_line": 26, "excerpt_truncated": False,
                "symbol": {"symbol": "doc/util.go::a"}}]}}}
        after = degrade(payload, orderer(False, None, 7), blind_attribution=True)
        text = after["result"]["content"][0]["text"]
        self.assertNotIn("doc/util.go::a", text)
        self.assertIn("func a()", text)
        self.assertNotIn("symbol", after["result"]["structuredContent"]["results"][0])
        self.assertEqual(after["result"]["structuredContent"]["results"][0]["excerpt"],
                         "func a() {\n\treturn\n}")


def concept_call(arguments):
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "search_concept", "arguments": arguments}}


class ForceExcerptTests(unittest.TestCase):
    """The pin that makes the blinded arm and its baseline differ in one property.

    Without it the amount of *content* a blinded page loses is set by how often the model asked
    for source text - 11% of concept pages on the Claude Django study against 100% on etcd -
    which is client behaviour, not the variable under test. `runs/page-position-20260922`.
    """

    def fields(self, arguments):
        return pin_excerpt(concept_call(arguments))["params"]["arguments"]["fields"]

    def test_a_call_that_asked_for_nothing_is_pinned(self):
        self.assertEqual(self.fields({"query": "release the shared key"}), ["excerpt"])

    def test_an_empty_field_list_is_pinned(self):
        """`fields: []` is a real thing an agent sends."""
        self.assertEqual(self.fields({"query": "q", "fields": []}), ["excerpt"])

    def test_a_call_that_already_asked_is_left_exactly_as_it_was(self):
        self.assertEqual(self.fields({"query": "q", "fields": ["excerpt"]}), ["excerpt"])

    def test_other_requested_fields_are_kept(self):
        self.assertEqual(self.fields({"query": "q", "fields": ["other"]}), ["other", "excerpt"])

    def test_the_rest_of_the_call_is_untouched(self):
        pinned = pin_excerpt(concept_call({"query": "q", "limit": 25, "path": "net/"}))
        arguments = pinned["params"]["arguments"]
        self.assertEqual(arguments["query"], "q")
        self.assertEqual((arguments["limit"], arguments["path"]), (25, "net/"))

    def test_another_tool_is_never_pinned(self):
        """`fields` belongs to ConceptArgs alone and every args struct is deny_unknown_fields,
        so pinning it onto find_callers would have the server reject the call."""
        for tool in ("find_callers", "search_exact", "read_source"):
            call = concept_call({"name": "helper"})
            call["params"]["name"] = tool
            self.assertEqual(pin_excerpt(copy.deepcopy(call)), call)

    def test_a_notification_or_handshake_passes_through(self):
        for payload in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}):
            self.assertEqual(pin_excerpt(copy.deepcopy(payload)), payload)

    def test_a_line_round_trips_as_one_json_line(self):
        line = rewrite_request(json.dumps(concept_call({"query": "q"})) + "\n")
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(json.loads(line)["params"]["arguments"]["fields"], ["excerpt"])

    def test_a_non_json_line_passes_through_unchanged(self):
        self.assertEqual(rewrite_request("not json\n"), "not json\n")

    def test_an_uninterpretable_call_raises_rather_than_passing_unpinned(self):
        """`main` turns this into a dead arm: an arm that quietly stopped pinning differs from
        its baseline in a second, unrecorded variable."""
        for arguments in ("a string", ["a", "list"]):
            with self.assertRaises(ValueError):
                pin_excerpt(concept_call(arguments))
        with self.assertRaises(ValueError):
            pin_excerpt(concept_call({"query": "q", "fields": "excerpt"}))


class PassThroughTests(unittest.TestCase):
    def test_the_tool_surface_is_untouched(self):
        """`tools/list` is the prompt prefix. If this arm changed it, a token comparison against
        HEAD would be measuring the surface rather than the page."""
        listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search_concept"}]}}
        self.assertEqual(degrade(copy.deepcopy(listing), orderer(True, None, 7)), listing)

    def test_read_source_is_left_alone(self):
        """It is how an agent verifies evidence; damaging it would confound retrieval with
        verification."""
        source = {"jsonrpc": "2.0", "id": 4, "result": {
            "content": [{"type": "text", "text": "lines[2] line\ttext\n1\tfn a()\n2\t}"}],
            "structuredContent": {"lines": [{"line": 1}, {"line": 2}], "path": "a.rs"}}}
        self.assertEqual(degrade(copy.deepcopy(source), orderer(True, None, 7)), source)

    def test_an_empty_page_is_not_an_error(self):
        empty = page([])
        self.assertEqual(degrade(copy.deepcopy(empty), orderer(True, None, 7)), empty)


class LoudFailureTests(unittest.TestCase):
    def test_a_payload_that_cannot_be_degraded_raises(self):
        """`pump` turns this into a dead arm. Forwarding it intact would leave a control that is
        HEAD for some calls."""
        broken = page(["a", "b"])
        broken["result"]["content"][0]["text"] = "backend: bm25\nhas_more: false"
        with self.assertRaises(ValueError):
            degrade(broken, orderer(True, None, 7))

    def test_a_row_block_shorter_than_it_claims_raises(self):
        broken = page(["a", "b", "c"])
        text = broken["result"]["content"][0]["text"].split("\n")
        broken["result"]["content"][0]["text"] = "\n".join(text[:3])
        with self.assertRaises(ValueError):
            degrade(broken, orderer(True, None, 7))


if __name__ == "__main__":
    unittest.main()
