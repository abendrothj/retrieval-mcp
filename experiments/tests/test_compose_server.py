"""The plan interface: reference resolution, one-variable surface, and loud failure.

The properties whose failure would be silent: a reference must come from rows the server actually
returned (never from rendered text), a plan must fail rather than run a step with an unresolved
argument, and the tool surface the client sees must be exactly one tool - if the four leak through
beside it, the arm differs from its baseline in two ways and the comparison is dead.
"""
import io
import json
import unittest

import compose_server


def payload(rows, text="rendered"):
    return {"result": {"content": [{"type": "text", "text": text}],
                       "structuredContent": {"results": rows}}}


class Resolve(unittest.TestCase):
    def test_a_reference_reads_the_first_row_of_the_named_step(self):
        rows = [[{"symbol": "a/b.go::Renew", "path": "a/b.go"}]]
        self.assertEqual(compose_server.resolve("$1.symbol", rows), "a/b.go::Renew")

    def test_references_resolve_inside_nested_arguments(self):
        rows = [[{"path": "a/b.go", "start_line": 12}]]
        arguments = {"path": "$1.path", "lines": ["$1.start_line", 3], "keep": "literal"}
        self.assertEqual(compose_server.resolve(arguments, rows),
                         {"path": "a/b.go", "lines": [12, 3], "keep": "literal"})

    def test_a_plain_string_is_left_alone(self):
        self.assertEqual(compose_server.resolve("lease renewal", []), "lease renewal")
        self.assertEqual(compose_server.resolve("$notareference", []), "$notareference")

    def test_a_reference_to_a_step_that_has_not_run_is_refused(self):
        with self.assertRaisesRegex(ValueError, "names step 2"):
            compose_server.resolve("$2.symbol", [[{"symbol": "x"}]])

    def test_a_reference_into_an_empty_page_is_refused(self):
        with self.assertRaisesRegex(ValueError, "returned no rows"):
            compose_server.resolve("$1.path", [[]])

    def test_a_reference_to_a_missing_field_names_what_was_there(self):
        with self.assertRaisesRegex(ValueError, "start_line"):
            compose_server.resolve("$1.nope", [[{"path": "a", "start_line": 1}]])


    def test_a_name_that_lands_on_an_object_repeating_it_unwraps_once(self):
        """The server's `symbol` cell is an object carrying a `symbol` string. Found by a probe:
        without this the next step is called with a map and the server says
        `invalid type: map, expected a string`."""
        rows = [[{"symbol": {"kind": "method_elem", "direct_callees": 0,
                             "symbol": "server/lease/lessor.go::Checkpoint"}}]]
        self.assertEqual(compose_server.resolve("$1.symbol", rows),
                         "server/lease/lessor.go::Checkpoint")

    def test_a_dotted_path_walks_the_same_object_explicitly(self):
        rows = [[{"symbol": {"kind": "method_elem", "symbol": "a/b.go::F"}}]]
        self.assertEqual(compose_server.resolve("$1.symbol.kind", rows), "method_elem")

    def test_walking_into_a_scalar_names_where_it_stopped(self):
        with self.assertRaisesRegex(ValueError, "walks into str"):
            compose_server.resolve("$1.path.nope", [[{"path": "a/b.go"}]])

class Rows(unittest.TestCase):
    def test_rows_come_from_structured_content_only(self):
        self.assertEqual(compose_server.rows_of(payload([{"path": "a"}])), [{"path": "a"}])

    def test_a_result_without_structured_rows_yields_none(self):
        self.assertEqual(compose_server.rows_of({"result": {"content": []}}), [])
        self.assertEqual(compose_server.rows_of({"error": {"code": -1}}), [])


class FakeChild:
    """Records the calls a plan makes and answers them from a script."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def call(self, name, arguments):
        self.calls.append((name, json.loads(json.dumps(arguments))))
        return self.answers.pop(0)


class Plan(unittest.TestCase):
    def test_a_dependent_chain_runs_in_one_call_and_counts_its_operations(self):
        child = FakeChild([payload([{"symbol": "a/b.go::Renew"}], "concept page"),
                           payload([{"path": "c/d.go", "start_line": 40}], "caller page"),
                           payload([], "source")])
        steps = [{"tool": "search_concept", "arguments": {"query": "lease renewal"}},
                 {"tool": "find_callers", "arguments": {"name": "$1.symbol"}},
                 {"tool": "read_source", "arguments": {"path": "$2.path",
                                                       "start_line": "$2.start_line"}}]
        operations, result = compose_server.run_plan(child, steps, io.StringIO())
        self.assertEqual(operations, 3)
        self.assertEqual([name for name, _ in child.calls],
                         ["search_concept", "find_callers", "read_source"])
        self.assertEqual(child.calls[1][1], {"name": "a/b.go::Renew"})
        self.assertEqual(child.calls[2][1], {"path": "c/d.go", "start_line": 40})
        self.assertEqual(result["structuredContent"]["operations"], 3)
        text = result["content"][0]["text"]
        for fragment in ("step 1: search_concept", "concept page", "step 3: read_source"):
            self.assertIn(fragment, text)

    def test_a_one_step_plan_is_todays_behaviour(self):
        child = FakeChild([payload([{"path": "a/b.go"}], "one page")])
        operations, result = compose_server.run_plan(
            child, [{"tool": "search_exact", "arguments": {"query": "Renew"}}], io.StringIO())
        self.assertEqual(operations, 1)
        self.assertIn("one page", result["content"][0]["text"])

    def test_a_failed_step_stops_the_plan_and_returns_what_ran(self):
        """Throwing the call away would waste the round trip this interface exists to save, so a
        dead step ends the plan and the model sees how far the chain got."""
        child = FakeChild([{"error": {"code": -32602, "message": "bad path"}},
                           payload([{"path": "never"}])])
        operations, result = compose_server.run_plan(
            child, [{"tool": "read_source", "arguments": {"path": "x"}},
                    {"tool": "search_exact", "arguments": {"query": "y"}}], io.StringIO())
        self.assertEqual(len(child.calls), 1)
        self.assertEqual(operations, 1)
        self.assertIn("plan stopped", result["content"][0]["text"])
        self.assertIn("step 1", result["structuredContent"]["stopped"])

    def test_an_unresolvable_reference_stops_the_plan_without_calling_the_step(self):
        child = FakeChild([payload([], "empty page")])
        operations, result = compose_server.run_plan(
            child, [{"tool": "search_concept", "arguments": {"query": "q"}},
                    {"tool": "find_callers", "arguments": {"name": "$1.symbol_name"}}],
            io.StringIO())
        self.assertEqual(operations, 1)
        self.assertEqual(len(child.calls), 1)
        self.assertIn("did not run", result["structuredContent"]["stopped"])

    def test_the_bare_name_is_derived_for_find_callers(self):
        """`find_callers` answers `unknown_symbol` for a path::Name identity - measured against the
        real server, which is how this field came to exist."""
        rows = [[{"symbol": {"symbol": "server/lease/lessor.go::Checkpoint"}}]]
        self.assertEqual(compose_server.resolve("$1.symbol_name", rows), "Checkpoint")
        self.assertEqual(compose_server.resolve("$1.symbol", rows),
                         "server/lease/lessor.go::Checkpoint")

    def test_a_malformed_step_is_refused_before_any_call(self):
        child = FakeChild([])
        with self.assertRaisesRegex(ValueError, "step 1"):
            compose_server.run_plan(child, [{"tool": "search_exact"}], io.StringIO())
        self.assertEqual(child.calls, [])


class Instructions(unittest.TestCase):
    """Codex prefixes a server's initialize instructions to every tool description, so an arm whose
    instructions name four uncallable tools sends the model to the shell - measured at 0 MCP calls
    across 7 trials before this existed."""

    SOURCE = ("Route repository retrieval by the question's intent:\n"
              "- Known literal: use search_exact.\n"
              "- Behaviour whose spelling is unknown: start with search_concept.\n"
              "- Direct callers: use find_callers.\n"
              "- Verify evidence with read_source.")

    def test_every_tool_name_is_restated_as_a_step(self):
        text = compose_server.rewrite_instructions(self.SOURCE)
        for name in compose_server.STEP_TOOLS:
            self.assertIn(f"a `{name}` step", text)

    def test_the_servers_own_routing_survives(self):
        text = compose_server.rewrite_instructions(self.SOURCE)
        self.assertIn("Route repository retrieval by the question's intent", text)
        self.assertIn("Direct callers", text)

    def test_the_call_shape_and_reference_syntax_are_stated(self):
        text = compose_server.rewrite_instructions(self.SOURCE)
        self.assertIn("one tool, `retrieve`", text)
        self.assertIn("$1.symbol_name", text)

    def test_no_claim_is_made_about_the_interface_being_better(self):
        text = compose_server.rewrite_instructions(self.SOURCE).lower()
        for word in ("cheaper", "faster", "more precise", "better", "prefer"):
            self.assertNotIn(word, text)


class Surface(unittest.TestCase):
    def test_the_description_is_generated_from_the_real_tools(self):
        text = compose_server.description(
            [{"name": "search_concept", "description": "Rank definitions.\nMore prose."},
             {"name": "find_callers", "description": "Direct callers."}])
        self.assertIn("- search_concept: Rank definitions.", text)
        self.assertIn("- find_callers: Direct callers.", text)
        self.assertNotIn("More prose.", text)

    def test_the_description_documents_composing_the_way_a_shipped_tool_would(self):
        """Usage guidance is what a tool description is for, and withholding it would measure a
        product nobody would ship. The arm is therefore the upper bound on the interface."""
        text = compose_server.description([{"name": "search_exact", "description": "Find."}]).lower()
        self.assertIn("one plan rather than issuing them one at a time", text)

    def test_no_claim_about_outcomes_reaches_the_description(self):
        """`skills/retrieval-mcp/SKILL.md` said these tools are "cheaper and more precise than
        reading files at random" and that sentence contaminated 2,109 archived trials. Telling the
        model how an interface works is documentation; telling it the interface wins is a claim
        about the measurement."""
        text = compose_server.description([{"name": "search_exact", "description": "Find."}]).lower()
        for word in ("cheaper", "more precise", "faster", "better than", "fewer tokens",
                     "more efficient"):
            self.assertNotIn(word, text)

    def test_the_tool_declares_the_annotations_that_let_it_run(self):
        """Codex refuses an unannotated MCP tool under a `never` approval policy - "MCP tool call
        requires approval" - which killed two attempts of this study while looking like the model
        declining to compose."""
        real = [{"name": "search_exact", "description": "d",
                 "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                 "openWorldHint": False}}] * 4
        self.assertEqual(compose_server.annotations(real),
                         {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})

    def test_a_plan_is_read_only_only_if_every_step_is(self):
        mixed = [{"annotations": {"readOnlyHint": True}}, {"annotations": {"readOnlyHint": False}}]
        self.assertFalse(compose_server.annotations(mixed)["readOnlyHint"])

    def test_the_schema_admits_only_the_four_real_tools_as_steps(self):
        enum = SCHEMA_STEP = compose_server.SCHEMA["properties"]["steps"]["items"]["properties"]["tool"]["enum"]
        self.assertEqual(sorted(enum),
                         ["find_callers", "read_source", "search_concept", "search_exact"])
