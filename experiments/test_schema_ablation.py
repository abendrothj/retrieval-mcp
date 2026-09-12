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

    def test_break_even_prices_schema_against_turns_and_answers(self):
        full = [{"turns": 4, "input_tokens": 400}, {"turns": 6, "input_tokens": 600}]
        stub = {"turns_avoided": 4, "unique_solves_enabled": 0}
        # 10 turns x 50 tokens = 500 of schema, against 4 avoided turns x 100 tokens = 400.
        verdict = schema_ablation.break_even("t", 50.0, full, stub)
        self.assertEqual((verdict["schema_cost_tokens"], verdict["turn_saving_tokens"]), (500, 400))
        self.assertEqual(verdict["net_tokens"], -100)
        self.assertEqual(verdict["verdict"], "does not pay for its schema on this suite")

        paying = schema_ablation.break_even("t", 10.0, full, stub)
        self.assertEqual(paying["verdict"], "earns its schema on turns alone")

        enabling = schema_ablation.break_even(
            "t", 50.0, full, {"turns_avoided": 0, "unique_solves_enabled": 1})
        self.assertEqual(enabling["verdict"], "earns its schema by enabling answers")


if __name__ == "__main__":
    unittest.main()
