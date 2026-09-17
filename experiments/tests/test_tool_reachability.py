import unittest

import tool_reachability


class MarkerPresentTests(unittest.TestCase):
    def test_matches_path_and_nested_symbol(self):
        result = {
            "results": [{
                "path": "pkg/mod.py",
                "symbol": {"name": "target", "symbol": "pkg/mod.py::target"},
            }]
        }
        self.assertTrue(tool_reachability.marker_present(result, "pkg/mod.py::target"))
        self.assertFalse(tool_reachability.marker_present(result, "other/mod.py::target"))

    def test_matches_trace_row_path_and_caller(self):
        result = {"results": [{"path": "pkg/caller.py", "caller": "entry", "depth": 2}]}
        self.assertTrue(tool_reachability.marker_present(result, "pkg/caller.py::entry"))


class PathProblemsTests(unittest.TestCase):
    def test_requires_seed_then_derived_steps(self):
        task = {"tool_path": [
            {
                "tool": "search_concept",
                "arguments": {"query": "seed"},
                "expect": ["pkg/seed.py::seed"],
            },
            {
                "tool": "trace_dependencies",
                "arguments": {"name": "seed", "direction": "callers"},
                "expect": ["pkg/answer.py::answer"],
                "uses_prior": "pkg/seed.py::seed",
            },
        ]}
        self.assertEqual(tool_reachability.path_problems(task), [])

    def test_rejects_unexplained_second_step(self):
        task = {"tool_path": [
            {"tool": "search_concept", "arguments": {"query": "seed"}, "expect": ["seed"]},
            {"tool": "read_source", "arguments": {"path": "pkg.py"}, "expect": ["answer"]},
        ]}
        self.assertIn(
            "tool_path[1] must name evidence consumed from an earlier step",
            tool_reachability.path_problems(task),
        )

    def test_rejects_hard_coded_gold_as_initial_query(self):
        task = {
            "expected_json": {"answer": "pkg/answer.py::answer"},
            "tool_path": [{
                "tool": "find_symbol",
                "arguments": {"name": "answer"},
                "expect": ["pkg/answer.py::answer"],
            }],
        }
        self.assertIn(
            "tool_path[0] directly queries gold identifiers: ['answer']",
            tool_reachability.path_problems(task),
        )

    def test_requires_later_arguments_to_use_prior_marker(self):
        task = {"tool_path": [
            {"tool": "search_concept", "arguments": {"query": "seed"}, "expect": ["seed"]},
            {
                "tool": "trace_dependencies",
                "arguments": {"name": "unrelated"},
                "expect": ["answer"],
                "uses_prior": "pkg/seed.py::seed",
            },
        ]}
        self.assertIn(
            "tool_path[1] arguments do not consume prior marker 'pkg/seed.py::seed'",
            tool_reachability.path_problems(task),
        )


if __name__ == "__main__":
    unittest.main()
