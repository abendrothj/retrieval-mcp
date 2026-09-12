import unittest

import schema_ablation


class SchemaAblationTests(unittest.TestCase):
    def test_schema_tokens_use_minified_bytes(self):
        document = {"tools": [{"name": "search", "description": "x", "inputSchema": {}}]}
        expected = len('{"name":"search","description":"x","inputSchema":{}}'.encode()) / 4
        self.assertEqual(schema_ablation.tool_schema_tokens(document), {"search": expected})

    def test_leave_one_out_reports_quality_and_turn_utility(self):
        def row(system, task, score, calls, tools):
            schema = {tool: 10.0 for tool in tools}
            return {
                "task_id": task,
                "repetition": 1,
                "system": system,
                "score": score,
                "input_tokens": 100 + calls,
                "calls": calls,
                "turns": calls + 1,
                "context_token_turns": calls * 20,
                "tools": {"trace": 1} if system == "full" else {},
                "visible_tools": frozenset(tools),
                "schema_tokens": schema,
            }

        rows = [
            row("full", "q1", 1.0, 2, {"search", "trace"}),
            row("full", "q2", 1.0, 3, {"search", "trace"}),
            row("no-trace", "q1", 0.0, 5, {"search"}),
            row("no-trace", "q2", 1.0, 4, {"search"}),
        ]
        report = schema_ablation.analyze(rows, "full")
        utility = report["per_tool"]["trace"]["leave_one_out_utility"]
        self.assertEqual(utility["unique_solves_enabled"], 1)
        self.assertEqual(utility["questions"], ["q1"])
        self.assertEqual(utility["turns_avoided"], 4)
        self.assertEqual(report["systems"]["no-trace"]["regressions_vs_full"], ["q1"])


if __name__ == "__main__":
    unittest.main()
